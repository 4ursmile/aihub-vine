"""Test-suite guard: no test may reach a real Kafka broker.

The bundled defaults.json has no Kafka bootstrap server, so an unconfigured test spools its events locally and sends nothing. Tests
that need a broker set AIHUB_KAFKA_BOOTSTRAP themselves (tests/test_kafka_e2e.py starts its own).
"""
import os

os.environ.pop("AIHUB_KAFKA_BOOTSTRAP", None)
