"""Consumes the CLIs' usage events from Kafka and hands them to `Sync.ingest`, which writes the database (the web shows them at once).

At-least-once: offsets are committed only after a batch is stored, so a crash or a database error means redelivery, and the unique
`events.ext_id` makes redelivery harmless. Several servers can share the group (`aihub-server`): Kafka splits the partitions.
The thread never dies: any failure is recorded in `sync.status["kafka"]` (Admin > Sync) and retried with a growing delay.
Messages are untrusted input: oversize, non-JSON and non-object messages are dropped.
"""
import json
import logging
import threading

from ..core import kafkacfg

log = logging.getLogger("aihub")
GROUP = "aihub-server"
MAX_MESSAGE = 64 * 1024         # a real event is well under 4 KB (publish events carry a manifest summary)
POLL_MS = 1000
MAX_RECORDS = 500
RETRY_MAX = 60


class KafkaIngest:
    def __init__(self, sync):
        self.sync = sync
        self.stop = threading.Event()
        self.t = None
        self.consumer = None
        self.st = sync.status["kafka"] = {"enabled": False, "connected": False, "consumed": 0, "last_message": None, "error": None, "topic": ""}

    def start(self):
        cfg = self.sync.kafka_cfg()
        self.st.update(enabled=bool(cfg), topic=cfg.get("topic", ""))
        if not cfg:
            log.info("kafka: not configured (AIHUB_KAFKA_BOOTSTRAP); usage events are not collected")
            return
        log.info("kafka: consuming topic %s from %s as group %s", cfg["topic"], cfg["bootstrap"], GROUP)
        self.t = threading.Thread(target=self._loop, name="aihub-kafka", daemon=True)
        self.t.start()

    def close(self):
        self.stop.set()
        c = self.consumer
        if c is not None:
            try:
                c.wakeup() if hasattr(c, "wakeup") else None
            except Exception:
                pass

    def _connect(self, cfg):
        from kafka import KafkaConsumer
        return KafkaConsumer(cfg["topic"], group_id=GROUP, enable_auto_commit=False, auto_offset_reset="earliest",
                             max_poll_records=MAX_RECORDS, consumer_timeout_ms=POLL_MS, bootstrap_timeout_ms=10000,
                             **kafkacfg.client_kwargs(cfg))

    def _loop(self):
        fails = 0
        while not self.stop.is_set():
            try:
                cfg = self.sync.kafka_cfg()
                if not cfg:
                    self.st.update(enabled=False, connected=False)
                    self.stop.wait(30)                              # switched off in the settings: keep checking, an admin may turn it on
                    continue
                self.consumer = self._connect(cfg)
                self.st.update(enabled=True, connected=True, error=None, topic=cfg["topic"])
                fails = 0
                self._consume(self.consumer)
            except Exception as e:
                fails += 1
                self.st.update(connected=False, error=("%s: %s" % (type(e).__name__, e))[:300])
                log.warning("kafka consumer: %s (retry %d)", e, fails)
            finally:
                c, self.consumer = self.consumer, None
                if c is not None:
                    try:
                        c.close(autocommit=False)
                    except Exception:
                        pass
            if not self.stop.is_set():
                self.stop.wait(min(2 ** min(fails, 6), RETRY_MAX))

    def _consume(self, consumer):
        while not self.stop.is_set():
            batch = consumer.poll(timeout_ms=POLL_MS, max_records=MAX_RECORDS)
            events = []
            for records in batch.values():
                for rec in records:
                    ev = self.parse(rec.value)
                    if ev is not None:
                        events.append(ev)
            if not batch:
                continue
            try:
                self.sync.ingest(events)                            # raises on a database error: nothing is committed, Kafka redelivers
            finally:
                self.sync.repos.db.release()
            consumer.commit()
            import time
            self.st.update(consumed=self.st["consumed"] + len(events), last_message=time.time())

    @staticmethod
    def parse(raw):
        """One message value -> event dict, or None for anything that is not one of ours."""
        if not raw or len(raw) > MAX_MESSAGE:
            return None
        try:
            ev = json.loads(raw)
        except ValueError:
            return None
        return ev if isinstance(ev, dict) else None
