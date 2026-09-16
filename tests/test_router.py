import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.cloud.finops import CostMeter
from app.cloud.local import LocalProvider
from app.cloud.router import EDGE_BUFFER, MultiCloudRouter
from app.config import Settings
from app.storage.db import Database


@pytest.fixture
def router(tmp_path):
    db = Database(":memory:")
    providers = [
        LocalProvider(tmp_path, "k", name="aws", kind="aws", region="ap-southeast-2"),
        LocalProvider(tmp_path, "k", name="azure", kind="azure", region="australiaeast"),
    ]
    meter = CostMeter(db, Settings().pricing_file, 500)
    return MultiCloudRouter(providers, db, meter, threshold=2, cooldown=0.2)


RETAIN = datetime.now(UTC) + timedelta(days=1)


def test_primary_serves_when_healthy(router):
    result = router.publish("results", "{}", "m1")
    assert result.provider == "aws" and not result.failover


def test_failover_to_secondary_during_outage(router):
    router.set_outage("aws", True)
    result = router.publish("results", "{}", "m1")
    assert result.provider == "azure" and result.failover
    assert router.status()["active_route"] == "azure"
    assert router.breakers["aws"].state == "open"


def test_edge_buffer_when_every_cloud_is_down_then_replay(router):
    router.set_outage("aws", True)
    router.set_outage("azure", True)
    result = router.publish("results", '{"a":1}', "m1")
    archived = router.archive("rpr/1.json", b"{}", RETAIN)
    assert result.provider == EDGE_BUFFER
    assert archived.replicas == {"aws": "pending", "azure": "pending"}
    assert router.buffer_depth() == 3

    assert router.drain()["replayed"] == 0  # still down, nothing lost
    router.set_outage("aws", False)
    router.set_outage("azure", False)
    outcome = router.drain()
    assert outcome == {"replayed": 3, "remaining": 0}
    assert router.providers["aws"].objects.exists("rpr/1.json")
    assert router.providers["azure"].objects.exists("rpr/1.json")
    received = []
    router.consume("results", lambda m: received.append(json.loads(m.body)))
    assert received == [{"a": 1}]


def test_archive_replicates_to_every_cloud(router):
    result = router.archive("rpr/2.json", b'{"x":1}', RETAIN)
    assert set(result.replicas) == {"aws", "azure"}
    assert all(v.startswith("file://") for v in result.replicas.values())


def test_partial_outage_leaves_pending_replica_that_heals(router):
    router.set_outage("azure", True)
    result = router.archive("rpr/3.json", b"{}", RETAIN)
    assert result.replicas["azure"] == "pending" and result.failover
    healed = []
    router.on_replicated = lambda key, provider, loc: healed.append((key, provider))
    router.set_outage("azure", False)
    router.drain()
    assert healed == [("rpr/3.json", "azure")]


def test_circuit_breaker_half_opens_after_cooldown(router):
    breaker = router.breakers["aws"]
    breaker.failure("x")
    breaker.failure("x")
    assert breaker.state == "open" and not breaker.allow()
    time.sleep(0.25)
    assert breaker.state == "half_open" and breaker.allow()
    breaker.success(1.0)
    assert breaker.state == "closed"


def test_cost_strategy_prefers_cheaper_messaging(router):
    router.order = ["azure", "aws"]  # azure is primary by priority
    assert router.publish("results", "{}", "m1").provider == "azure"
    router.strategy = "cost"
    assert router.publish("results", "{}", "m2").provider == "aws"  # cheaper per request in pricing.json


def test_poison_messages_are_dead_lettered(router):
    router.publish("results", "not json", "bad")
    for _ in range(6):
        router.consume("results", lambda m: json.loads(m.body))
    letters = router.dead_letters("results")
    assert [m["message_id"] for m in letters["aws"]] == ["bad"]


def test_costs_are_metered_per_provider(router):
    router.publish("results", "{}", "m1", site="RNSH")
    router.archive("rpr/4.json", b"{}", RETAIN, site="RNSH")
    summary = router.meter.summary()
    providers = {r["provider"] for r in summary["by_provider"]}
    assert providers == {"aws", "azure"}
    assert summary["cheapest_messaging_provider"] == "aws"
