"""End to end against a REAL Kafka broker: CLI spool -> flusher -> producer -> broker -> server consumer -> database -> /index.

Needs a broker: set AIHUB_TEST_KAFKA=host:9092 (anonymous PLAINTEXT is enough: the CLI is never asked for a login). Without it the
tests skip. kafka-python's KafkaAdminClient creates a unique topic per run and deletes it afterwards, so nothing else on the broker is
touched and the consumer group is unique too.
Run:  AIHUB_TEST_KAFKA=localhost:9092 python -m unittest tests.test_kafka_e2e -v
"""
import json
import os
import tempfile
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

BROKER = os.environ.get("AIHUB_TEST_KAFKA", "")


def until(fn, secs=30):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


@unittest.skipUnless(BROKER, "set AIHUB_TEST_KAFKA=host:9092 to run against a real broker")
class RealBroker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kafka.admin import KafkaAdminClient, NewTopic
        cls.topic = "aihub.e2e.%s" % uuid.uuid4().hex[:8]
        cls.admin = KafkaAdminClient(bootstrap_servers=BROKER, request_timeout_ms=15000)
        cls.admin.create_topics([NewTopic(cls.topic, num_partitions=1, replication_factor=1)])
        until(lambda: cls.topic in cls.admin.list_topics())

    @classmethod
    def tearDownClass(cls):
        try:
            cls.admin.delete_topics([cls.topic])
        finally:
            cls.admin.close()

    def setUp(self):
        from aihub.server import kafka_in
        from aihub.server.config import Settings
        from aihub.server.main import create_app
        self.home = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_KAFKA_BOOTSTRAP": BROKER, "AIHUB_KAFKA_TOPIC": self.topic,
                                           "AIHUB_TELEMETRY_INTERVAL": "5"})
        env.start()
        self.addCleanup(env.stop)
        # the Kafka producer must never prompt (git logins are a separate, kept feature): fail the test instead
        for target in ("builtins.input", "getpass.getpass"):
            p = mock.patch(target, side_effect=AssertionError("the Kafka producer must never ask for a login"))
            p.start()
            self.addCleanup(p.stop)
        g = mock.patch.object(kafka_in, "GROUP", "aihub-e2e-%s" % uuid.uuid4().hex[:8])
        g.start()
        self.addCleanup(g.stop)
        self.c = TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))
        self.sy, self.r = self.c.app.state.sync, self.c.app.state.repos
        with mock.patch.object(type(self.sy), "kafka_cfg", lambda s: {"bootstrap": BROKER, "topic": self.topic, "security_protocol": "",
                                                                       "sasl_mechanism": "", "username": "", "password": "", "ssl_cafile": ""}):
            self.ingest = kafka_in.KafkaIngest(self.sy)
            self.sy.kafka = self.ingest
            self.ingest.start()
        self.addCleanup(self.ingest.close)

    def count(self, where):
        return self.r.db.conn().execute("SELECT COUNT(*) FROM events WHERE " + where).fetchone()[0]

    def test_cli_events_reach_the_database_and_a_publish_reaches_the_index_cache(self):
        from aihub.cli import telemetry
        tag = uuid.uuid4().hex[:6]
        self.assertTrue(telemetry.enabled())
        self.assertEqual((telemetry.kafka_config()["username"], telemetry.kafka_config()["password"]), ("", ""))   # anonymous: nothing to fill in
        for i in range(25):
            telemetry.enqueue({"kind": "use", "package": "e2e-" + tag, "component": "skill:x", "id": "%s-%d" % (tag, i)})
        self.assertEqual(telemetry.flush(force=True), 25)                                  # the real producer, acks=all
        self.assertTrue(until(lambda: self.count("package='e2e-%s'" % tag) == 25), "the consumer did not store the 25 events")
        self.assertTrue(until(lambda: self.ingest.st["connected"]))
        self.assertIsNone(self.ingest.st["error"])

        first = self.c.get("/api/v1/index")                                                 # cached after the first build
        self.assertEqual(first.status_code, 200)
        etag = first.headers["ETag"]
        more = [{"kind": "use", "package": "e2e-" + tag, "component": "skill:y", "id": "%s-more-%d" % (tag, i)} for i in range(5)]
        for e in more:
            telemetry.enqueue(e)
        telemetry.flush(force=True)
        self.assertTrue(until(lambda: self.count("package='e2e-%s'" % tag) == 30))
        self.assertEqual(self.c.get("/api/v1/index", headers={"If-None-Match": etag}).status_code, 304)   # usage did not touch the index

        ev = {"kind": "publish", "package": "pub-" + tag, "version": "1.0.0", "git_url": "file:///tmp/x.git", "commit": "abc",
              "type": "tool", "description": "e2e"}
        with mock.patch.object(telemetry, "kick", lambda: None):
            self.assertTrue(telemetry.deliver(ev))                                          # publish goes over Kafka only
        self.assertTrue(until(lambda: self.r.package("pub-" + tag)), "the publish event never became a package")
        r = until(lambda: (lambda x: x if x.status_code == 200 else None)(self.c.get("/api/v1/index", headers={"If-None-Match": etag})))
        self.assertIn("pub-" + tag, [p["name"] for p in r.json()["packages"]])              # the CLI sees it before git has it
        self.assertEqual([i["package"] for i in self.sy._queue_read()], ["pub-" + tag])     # and git gets ONE batched commit later

    def test_redelivery_after_a_restart_does_not_duplicate_rows(self):
        from aihub.cli import telemetry
        tag = uuid.uuid4().hex[:6]
        for i in range(8):
            telemetry.enqueue({"kind": "use", "package": "re-" + tag, "id": "%s-%d" % (tag, i)})
        telemetry.flush(force=True)
        self.assertTrue(until(lambda: self.count("package='re-%s'" % tag) == 8))
        from kafka import KafkaConsumer
        c = KafkaConsumer(self.topic, group_id="replay-" + tag, auto_offset_reset="earliest", bootstrap_servers=BROKER, consumer_timeout_ms=5000)
        msgs = [json.loads(m.value) for m in c if json.loads(m.value).get("package") == "re-" + tag]
        c.close()
        self.assertEqual(len(msgs), 8)
        self.assertEqual(self.sy.ingest(msgs), 0)                                           # the whole topic again: nothing new
        self.assertEqual(self.count("package='re-%s'" % tag), 8)

    def test_a_broker_that_is_down_keeps_events_spooled(self):
        from aihub.cli import telemetry
        with mock.patch.dict(os.environ, {"AIHUB_KAFKA_BOOTSTRAP": "127.0.0.1:1"}):
            telemetry.enqueue({"kind": "use", "package": "down-x", "id": uuid.uuid4().hex})
            self.assertEqual(telemetry.flush(force=True, timeout=3), 0)
            self.assertTrue(telemetry._pending())
            self.assertFalse(telemetry.last_status()["ok"])


if __name__ == "__main__":
    unittest.main()
