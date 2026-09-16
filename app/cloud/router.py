"""Multi cloud router.

Responsibilities
* Health aware routing with a circuit breaker per provider
* Priority or cost aware provider selection for messaging
* Active active replication of the immutable ledger to every cloud
* Store and forward: when every cloud is down, operations are written to an
  on premises edge buffer (transactional outbox) and replayed on reconnect
* Chaos controls that simulate a regional outage for resilience drills
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.cloud.base import CloudProvider, MessageHandler, ProviderUnavailable
from app.cloud.finops import CostMeter
from app.core import metrics
from app.storage.db import Database, new_id, utcnow

log = logging.getLogger(__name__)

EDGE_BUFFER = "edge_buffer"


@dataclass
class CircuitBreaker:
    threshold: int
    cooldown: float
    failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None
    ok_calls: int = 0
    failed_calls: int = 0
    last_latency_ms: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self, latency_ms: float) -> None:
        self.failures = 0
        self.opened_at = None
        self.ok_calls += 1
        self.last_latency_ms = round(latency_ms, 2)

    def failure(self, error: str) -> None:
        self.failures += 1
        self.failed_calls += 1
        self.last_error = error[:300]
        if self.failures >= self.threshold or self.state == "half_open":
            self.opened_at = time.monotonic()


@dataclass
class RouteResult:
    provider: str
    failover: bool = False
    replicas: dict[str, str] = field(default_factory=dict)


class MultiCloudRouter:
    def __init__(
        self,
        providers: list[CloudProvider],
        db: Database,
        meter: CostMeter,
        *,
        strategy: str = "priority",
        threshold: int = 3,
        cooldown: float = 20.0,
    ):
        if not providers:
            raise ValueError("At least one cloud provider is required")
        self.providers = {p.name: p for p in providers}
        self.order = [p.name for p in providers]
        self.db = db
        self.meter = meter
        self.strategy = strategy
        self.breakers = {name: CircuitBreaker(threshold, cooldown) for name in self.order}
        self.chaos: set[str] = set()
        self.on_replicated: Callable[[str, str, str], None] | None = None
        self._lock = threading.Lock()

    # core call wrapper ------------------------------------------------------
    def _call(self, name: str, operation: str, fn: Callable, site: str | None = None, units: float = 1.0):
        provider = self.providers[name]
        breaker = self.breakers[name]
        start = time.perf_counter()
        try:
            if name in self.chaos:
                raise ProviderUnavailable(f"simulated regional outage for {name}")
            result = fn(provider)
        except ProviderUnavailable as exc:
            with self._lock:
                breaker.failure(str(exc))
            metrics.CLOUD_CALLS.labels(name, operation, "error").inc()
            metrics.BREAKER_STATE.labels(name).set(1 if breaker.state == "open" else 0)
            raise
        elapsed = time.perf_counter() - start
        with self._lock:
            breaker.success(elapsed * 1000)
        metrics.CLOUD_CALLS.labels(name, operation, "ok").inc()
        metrics.CLOUD_LATENCY.labels(name, operation).observe(elapsed)
        metrics.BREAKER_STATE.labels(name).set(0)
        self.meter.record(provider.kind, operation, units, site)
        return result

    def _ranked(self, operation: str) -> list[str]:
        names = list(self.order)
        if self.strategy == "cost":
            kinds = {n: self.providers[n].kind for n in names}
            ranked_kinds = self.meter.cheapest(list(dict.fromkeys(kinds.values())), operation)
            names.sort(key=lambda n: ranked_kinds.index(kinds[n]))  # stable: priority breaks ties
        return names

    def _candidates(self, operation: str) -> list[str]:
        return [n for n in self._ranked(operation) if self.breakers[n].allow()]

    # messaging -------------------------------------------------------------
    def publish(self, topic: str, body: str, message_id: str, site: str | None = None) -> RouteResult:
        ideal = self._ranked("message_publish")[0]
        for name in self._candidates("message_publish"):
            try:
                self._call(name, "message_publish", lambda p: p.queue.publish(topic, body, message_id), site)
                failover = name != ideal
                if failover:
                    metrics.FAILOVERS.labels("message_publish").inc()
                return RouteResult(provider=name, failover=failover)
            except ProviderUnavailable:
                continue
        self._buffer("publish", "any", topic, body.encode(), message_id)
        return RouteResult(provider=EDGE_BUFFER, failover=True)

    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        total = 0
        for name in self.order:
            if not self.breakers[name].allow():
                continue

            def tagged(msg, provider=name):
                msg.attributes["provider"] = provider
                handler(msg)

            try:
                count = self._call(name, "message_receive", lambda p, h=tagged: p.queue.consume(topic, h, max_messages))
                total += count
            except ProviderUnavailable:
                continue
        return total

    def dead_letters(self, topic: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for name, provider in self.providers.items():
            if name in self.chaos:
                continue
            out[name] = [m.__dict__ for m in provider.queue.dead_letters(topic)]
        return out

    # immutable ledger replication ----------------------------------------
    def archive(self, key: str, data: bytes, retain_until: datetime, site: str | None = None) -> RouteResult:
        replicas: dict[str, str] = {}
        for name in self.order:
            if not self.breakers[name].allow():
                self._buffer("archive", name, key, data, retain_until.isoformat())
                replicas[name] = "pending"
                continue
            try:
                location = self._call(
                    name, "object_put", lambda p: p.objects.put_immutable(key, data, retain_until), site
                )
                replicas[name] = location
            except ProviderUnavailable:
                self._buffer("archive", name, key, data, retain_until.isoformat())
                replicas[name] = "pending"
        confirmed = [n for n, v in replicas.items() if v != "pending"]
        return RouteResult(
            provider=confirmed[0] if confirmed else EDGE_BUFFER,
            failover=len(confirmed) < len(self.order),
            replicas=replicas,
        )

    def read_from(self, name: str, key: str) -> bytes:
        return self._call(name, "object_get", lambda p: p.objects.get(key))

    def fetch(self, key: str) -> tuple[str, bytes]:
        for name in self.order:
            if not self.breakers[name].allow():
                continue
            try:
                return name, self._call(name, "object_get", lambda p: p.objects.get(key))
            except (ProviderUnavailable, FileNotFoundError):
                continue
            except Exception:  # noqa: BLE001 object missing in this cloud, try the next one
                continue
        raise ProviderUnavailable(f"No provider could return {key}")

    # edge buffer (transactional outbox) ----------------------------------
    def _buffer(self, kind: str, target: str, topic: str, payload: bytes, extra: str) -> None:
        self.db.insert(
            "outbox",
            {
                "id": new_id("obx"),
                "kind": kind,
                "target": target,
                "topic": topic,
                "payload": payload,
                "meta": extra,
                "attempts": 0,
                "created_at": utcnow(),
            },
        )
        metrics.OUTBOX_DEPTH.set(self.buffer_depth())

    def buffer_depth(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM outbox")
        return row["n"] if row else 0

    def drain(self, limit: int = 200) -> dict[str, int]:
        """Replay buffered operations. Safe to call repeatedly; writes are idempotent."""
        replayed = 0
        rows = self.db.all("SELECT * FROM outbox ORDER BY created_at LIMIT ?", (limit,))
        for row in rows:
            try:
                if row["kind"] == "publish":
                    result = self.publish_direct(row["topic"], bytes(row["payload"]).decode(), row["meta"])
                    if result is None:
                        raise ProviderUnavailable("no provider available")
                else:
                    name = row["target"]
                    if not self.breakers[name].allow():
                        raise ProviderUnavailable(f"{name} still unavailable")
                    retain_until = datetime.fromisoformat(row["meta"])
                    location = self._call(
                        name,
                        "object_put",
                        lambda p, r=row, t=retain_until: p.objects.put_immutable(r["topic"], bytes(r["payload"]), t),
                    )
                    if self.on_replicated:
                        self.on_replicated(row["topic"], name, location)
                self.db.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
                replayed += 1
            except ProviderUnavailable:
                self.db.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        metrics.OUTBOX_DEPTH.set(self.buffer_depth())
        return {"replayed": replayed, "remaining": self.buffer_depth()}

    def publish_direct(self, topic: str, body: str, message_id: str) -> str | None:
        for name in self._candidates("message_publish"):
            try:
                self._call(name, "message_publish", lambda p: p.queue.publish(topic, body, message_id))
                return name
            except ProviderUnavailable:
                continue
        return None

    # health and chaos -------------------------------------------------------
    def probe(self) -> None:
        """Active health checks. Closes breakers once a provider recovers."""
        for name in self.providers:
            breaker = self.breakers[name]
            if breaker.state == "closed" and breaker.failures == 0:
                continue
            try:
                self._call(name, "health", lambda p: p.health_check())
            except ProviderUnavailable:
                pass

    def set_outage(self, name: str, down: bool) -> None:
        if name not in self.providers:
            raise KeyError(name)
        if down:
            self.chaos.add(name)
            # Mirror what a real outage does: the breaker trips after repeated failures.
            for _ in range(self.breakers[name].threshold):
                self.breakers[name].failure("simulated regional outage")
        else:
            self.chaos.discard(name)
            breaker = self.breakers[name]
            if breaker.opened_at is not None:
                breaker.opened_at = time.monotonic() - breaker.cooldown  # move to half open now
            self.probe()

    def status(self) -> dict:
        providers = []
        for i, name in enumerate(self.order):
            p = self.providers[name]
            b = self.breakers[name]
            providers.append(
                {
                    "name": name,
                    "kind": p.kind,
                    "simulated": p.simulated,
                    "region": p.region,
                    "priority": i + 1,
                    "state": b.state,
                    "outage_drill": name in self.chaos,
                    "consecutive_failures": b.failures,
                    "ok_calls": b.ok_calls,
                    "failed_calls": b.failed_calls,
                    "last_latency_ms": b.last_latency_ms,
                    "last_error": b.last_error,
                }
            )
        active = next((n for n in self._candidates("message_publish")), EDGE_BUFFER)
        return {
            "strategy": self.strategy,
            "active_route": active,
            "edge_buffer_depth": self.buffer_depth(),
            "providers": providers,
        }
