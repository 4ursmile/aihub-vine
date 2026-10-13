"""Kafka transport, the hub-served index with its CLI cache, and the backup jitter."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub.cli import api, gitx, paths, registry, telemetry
from aihub.core import kafkacfg, pkgindex as P
from aihub.server import backup as B, kafka_in
from aihub.server.config import Settings
from aihub.server.main import create_app

URL = "file:///tmp/x.git"


def app():
    return TestClient(create_app(Settings(seed_builtin=False, sync_enabled=False, data_dir=tempfile.mkdtemp())))


class KafkaConfig(unittest.TestCase):
    def test_off_without_a_bootstrap_server(self):
        self.assertEqual(kafkacfg.resolve(lambda k: ""), {})

    def test_topic_default_and_security_options(self):
        cfg = kafkacfg.resolve({"bootstrap": "a:9092, b:9093", "sasl_mechanism": "plain", "username": "u", "password": "p", "security_protocol": "sasl_ssl"}.get)
        self.assertEqual(cfg["topic"], "aihub.events")
        kw = kafkacfg.client_kwargs(cfg)
        self.assertEqual(kw["bootstrap_servers"], ["a:9092", "b:9093"])
        self.assertEqual((kw["security_protocol"], kw["sasl_mechanism"], kw["sasl_plain_password"]), ("SASL_SSL", "PLAIN", "p"))
        self.assertEqual(kafkacfg.hosts(cfg), [("a", 9092), ("b", 9093)])

    def test_client_config_never_carries_credentials(self):
        c = app()
        with mock.patch.dict(os.environ, {"AIHUB_KAFKA_BOOTSTRAP": "k:9092", "AIHUB_KAFKA_PASSWORD": "secret", "AIHUB_KAFKA_USERNAME": "svc"}):
            body = c.get("/api/v1/client-config").text
        self.assertIn("k:9092", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("svc", body)


class Consumer(unittest.TestCase):
    def test_messages_that_are_not_events_are_dropped(self):
        p = kafka_in.KafkaIngest.parse
        self.assertEqual(p(b'{"id":"1","kind":"use"}'), {"id": "1", "kind": "use"})
        for bad in (b"", None, b"not json", b"[1,2]", b'"x"', b"x" * (kafka_in.MAX_MESSAGE + 1)):
            self.assertIsNone(p(bad))

    def test_offsets_are_committed_only_after_the_events_are_stored(self):
        c = app()
        sy = c.app.state.sync
        order = []

        class Rec:
            def __init__(self, v):
                self.value = v

        class FakeConsumer:
            n = 0

            def poll(self, **k):
                self.n += 1
                if self.n > 1:
                    ki.stop.set()
                    return {}
                return {"tp": [Rec(json.dumps({"id": "k1", "kind": "use", "package": "demo", "ts": time.time()}).encode()), Rec(b"junk")]}

            def commit(self):
                order.append("commit")

        ki = kafka_in.KafkaIngest(sy)
        real = sy.ingest
        sy.ingest = lambda evs: order.append("stored") or real(evs)
        ki._consume(FakeConsumer())
        self.assertEqual(order, ["stored", "commit"])
        self.assertEqual(sy.repos.db.conn().execute("SELECT COUNT(*) FROM events WHERE ext_id='k1'").fetchone()[0], 1)

    def test_a_database_error_means_no_commit_so_kafka_redelivers(self):
        c = app()
        sy = c.app.state.sync
        committed = []

        class FakeConsumer:
            def poll(self, **k):
                return {"tp": [type("R", (), {"value": b'{"id":"z","kind":"use","package":"demo"}'})()]}

            def commit(self):
                committed.append(1)

        sy.ingest = mock.Mock(side_effect=RuntimeError("db down"))
        with self.assertRaises(RuntimeError):
            kafka_in.KafkaIngest(sy)._consume(FakeConsumer())
        self.assertEqual(committed, [])


class RealClientLibrary(unittest.TestCase):
    """The mocks above cannot catch a wrong option name (kafka-python 3 rejects unknown ones). These build the real clients; no broker is needed
    to get an answer, and a dead port must fail fast with a clean error, never hang a CLI command."""

    def setUp(self):
        try:
            import kafka  # noqa
        except ImportError:
            self.skipTest("kafka-python not installed")
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": tempfile.mkdtemp(), "AIHUB_KAFKA_BOOTSTRAP": "127.0.0.1:1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_producer_options_are_valid_and_a_dead_broker_fails_fast(self):
        t = time.time()
        with self.assertRaises(Exception) as cm:
            telemetry.send(['{"id":"1"}'], timeout=3)
        self.assertNotIsInstance(cm.exception, ValueError, "kafka-python rejected one of our producer options: %s" % cm.exception)
        self.assertLess(time.time() - t, 15)

    def test_consumer_options_are_valid_and_a_dead_broker_fails_fast(self):
        sy = app().app.state.sync
        t = time.time()
        with self.assertRaises(Exception) as cm:
            kafka_in.KafkaIngest(sy)._connect(sy.kafka_cfg())
        self.assertNotIsInstance(cm.exception, ValueError, "kafka-python rejected one of our consumer options: %s" % cm.exception)
        self.assertLess(time.time() - t, 25)


class CliSender(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_KAFKA_BOOTSTRAP": "localhost:1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_unreachable_broker_keeps_the_events_and_backs_off(self):
        telemetry.enqueue({"kind": "use", "package": "p"})
        with mock.patch.object(telemetry, "send", side_effect=RuntimeError("NoBrokersAvailable")):
            self.assertEqual(telemetry._send_once(), 0)
        self.assertTrue(telemetry._pending())
        self.assertGreater(telemetry._backoff(), 0)
        self.assertIn("NoBrokersAvailable", telemetry.last_status()["error"])

    def test_without_kafka_nothing_is_sent_and_the_spool_stays(self):
        with mock.patch.dict(os.environ, {"AIHUB_KAFKA_BOOTSTRAP": ""}), mock.patch("aihub.core.defaults.load", return_value={}):
            telemetry.enqueue({"kind": "use", "package": "p"})
            self.assertEqual(telemetry._send_once(), 0)
            self.assertTrue(telemetry._pending())

    def test_a_batch_is_one_flush_with_every_event_as_a_message(self):
        sent = []

        class Fut:
            def get(self, timeout=None):
                return None

        class Prod:
            def __init__(self, **kw):
                sent.append(kw)

            def send(self, topic, key=None, value=None):
                sent.append((topic, key, json.loads(value)["package"]))
                return Fut()

            def flush(self, timeout=None):
                sent.append("flush")

            def close(self, timeout=None):
                sent.append("close")

        import sys
        import types
        mod = types.ModuleType("kafka")
        mod.KafkaProducer = Prod
        with mock.patch.dict(sys.modules, {"kafka": mod}):
            telemetry.send([json.dumps({"package": "a"}), json.dumps({"package": "b"})])
        self.assertEqual(sent[0]["acks"], "all")
        self.assertEqual([x[2] for x in sent if isinstance(x, tuple)], ["a", "b"])
        self.assertEqual(sent[-2:], ["flush", "close"])
        self.assertEqual(sent[1][0], "aihub.events")


def seed(c):
    r = c.app.state.repos
    pid = r.package_upsert("alpha", "skill", "first", ["x"], "# readme", visibility="public")
    r.version_sync(pid, "1.0.0", {"package": {"name": "alpha", "version": "1.0.0"}, "requires": {"os": ["macos"]}, "repo": {"url": URL, "branch": "main", "ref": "a1"}})
    r.version_sync(pid, "1.1.0", {"package": {"name": "alpha", "version": "1.1.0"}, "requires": {}, "repo": {"url": URL, "branch": "main", "ref": "a2"}})
    hid = r.package_upsert("secret", "skill", "private", [], visibility="private")
    r.version_sync(hid, "1.0.0", {"package": {"name": "secret", "version": "1.0.0"}, "repo": {"url": URL}})
    return r


class HubIndex(unittest.TestCase):
    def test_served_from_the_database_with_versions_visibility_and_etag(self):
        c = app()
        seed(c)
        r = c.get("/api/v1/index")
        self.assertEqual(r.status_code, 200)
        pk = {p["name"]: p for p in r.json()["packages"]}
        self.assertEqual(list(pk), ["alpha"])                                              # the private package is not in an anonymous index
        a = pk["alpha"]
        self.assertEqual((a["latest_version"], [v["version"] for v in a["versions"]]), ("1.1.0", ["1.0.0", "1.1.0"]))
        self.assertEqual((a["repo"]["url"], a["versions"][0]["ref"], a["readme"]), (URL, "a1", "# readme"))
        self.assertEqual(c.get("/api/v1/index", headers={"If-None-Match": r.headers["ETag"]}).status_code, 304)
        c.app.state.repos.version_sync(c.app.state.repos.package("alpha")["id"], "1.2.0", {"package": {"name": "alpha", "version": "1.2.0"}, "repo": {"url": URL}})
        c.app.state.cache.invalidate("index")
        r2 = c.get("/api/v1/index", headers={"If-None-Match": r.headers["ETag"]})
        self.assertEqual(r2.status_code, 200)                                              # a new version changes the etag
        self.assertNotEqual(r2.headers["ETag"], r.headers["ETag"])

    def test_a_publish_event_is_in_the_index_before_git_has_it(self):
        c = app()
        sy = c.app.state.sync
        with mock.patch.object(sy_module(), "SCHEMES", sy_module().SCHEMES + ("file://",)):
            sy.ingest([{"id": "p1", "kind": "publish", "package": "brand-new", "version": "0.1.0", "git_url": URL, "commit": "c1", "type": "tool",
                        "description": "d", "ts": time.time()}])
        pk = {p["name"]: p for p in c.get("/api/v1/index").json()["packages"]}
        self.assertEqual(pk["brand-new"]["latest_version"], "0.1.0")
        self.assertEqual(pk["brand-new"]["versions"][0]["ref"], "c1")


def sy_module():
    from aihub.server import sync
    return sync


class FakeResp:
    def __init__(self, body, etag):
        self.body, self.headers = body, {"ETag": etag}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CliIndexCache(unittest.TestCase):
    """Hub first (ETag, 5 min), then git (5 min), never both on every command."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_INDEX_URL": "https://git.example/idx.git", "AIHUB_URL": "http://hub.test",
                                                "AIHUB_INDEX_TTL": ""})
        self.env.start()
        registry._cache.clear()
        self.body = json.dumps({"packages": [{"name": "alpha", "type": "skill", "latest_version": "1.0.0", "versions": [{"version": "1.0.0"}]}]}).encode()

    def tearDown(self):
        self.env.stop()
        registry._cache.clear()

    def test_within_the_ttl_nothing_is_asked(self):
        with mock.patch.object(api, "_req", return_value=FakeResp(self.body, '"e1"')) as rq, mock.patch.object(gitx, "fetch") as fetch:
            self.assertEqual(registry.get("alpha")["latest_version"], "1.0.0")
            registry._cache.clear()
            self.assertEqual(registry.get("alpha")["latest_version"], "1.0.0")
        self.assertEqual(rq.call_count, 1)                                                 # second command: served from ~/.aihub/index-hub.json
        fetch.assert_not_called()                                                          # and git was never touched

    def test_after_the_ttl_the_hub_is_asked_with_the_etag_and_304_keeps_the_cache(self):
        with mock.patch.object(api, "_req", return_value=FakeResp(self.body, '"e1"')):
            registry.get("alpha")
        c = paths.load("index-hub.json", {})
        c["at"] -= registry.CHECK + 1
        paths.save("index-hub.json", c)
        registry._cache.clear()
        seen = {}

        def req(method, path, **kw):
            seen.update(kw.get("headers") or {})
            raise api.ApiError("304 /index: Not Modified")
        with mock.patch.object(api, "_req", side_effect=req), mock.patch.object(gitx, "fetch") as fetch:
            self.assertEqual(registry.get("alpha")["latest_version"], "1.0.0")
        self.assertEqual(seen.get("If-None-Match"), '"e1"')
        fetch.assert_not_called()
        self.assertLess(time.time() - paths.load("index-hub.json", {})["at"], 5)           # the TTL restarted

    def test_hub_down_falls_back_to_git_and_does_not_retry_the_hub_every_command(self):
        repo = tempfile.mkdtemp()
        g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "skills", "one"))
        open(os.path.join(d, "aihub.toml"), "w").write('[package]\nname="one"\nversion="1.0.0"\ntype="skill"\ndescription="d"\n[[skills]]\nname="one"\npath="skills/one"\n')
        open(os.path.join(d, "skills", "one", "SKILL.md"), "w").write("---\nname: one\ndescription: Use when x\n---\nhi")
        P.add(os.path.join(repo, "index"), d, "https://example.invalid/one.git")
        g("add", "-A")
        g("commit", "-qm", "i")
        with mock.patch.dict(os.environ, {"AIHUB_INDEX_URL": "file://" + repo, "AIHUB_INDEX_PATH": "index"}), \
                mock.patch.object(api, "_req", side_effect=api.ApiError("cannot reach hub http://hub.test (refused)")) as rq:
            self.assertEqual(registry.get("one")["latest_version"], "1.0.0")              # served by git
            registry._cache.clear()
            registry.get("one")
            self.assertEqual(rq.call_count, 1)                                             # the hub is left alone for HUB_RETRY seconds

    def test_a_stale_cache_is_used_by_nobody_when_the_hub_changed(self):
        with mock.patch.object(api, "_req", return_value=FakeResp(self.body, '"e1"')):
            registry.get("alpha")
        registry._cache.clear()
        with mock.patch.dict(os.environ, {"AIHUB_URL": "http://other-hub.test"}), \
                mock.patch.object(api, "_req", return_value=FakeResp(json.dumps({"packages": []}).encode(), '"e2"')) as rq:
            with self.assertRaises(api.ApiError):
                registry.get("alpha")
        rq.assert_called_once()


class BackupJitter(unittest.TestCase):
    def test_a_scheduled_run_is_delayed_a_little_but_never_a_short_interval(self):
        self.assertEqual(B.jitter(60), 0)
        vals = {B.jitter(86400) for _ in range(50)}
        self.assertGreater(len(vals), 40)                                                  # not the same second every night
        self.assertTrue(all(0 <= v <= B.JITTER_MAX for v in vals))
        self.assertTrue(all(0 <= B.jitter(600) <= 60 for _ in range(50)))                  # at most 10% of a short wait


if __name__ == "__main__":
    unittest.main()
