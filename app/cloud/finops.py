"""FinOps metering.

Every cloud call is metered per provider, per operation and per hospital site.
This answers the questions finance teams actually ask: what does one pathology
test cost to move through the cloud, which site drives spend, and are we on
track against budget.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.storage.db import Database, utcnow


class CostMeter:
    def __init__(
        self, db: Database, pricing_file: Path, monthly_budget_aud: float, projected_monthly_tests: int = 1_500_000
    ):
        self.db = db
        raw = json.loads(Path(pricing_file).read_text())
        self.prices: dict[str, dict[str, float]] = raw["providers"]
        self.usd_to_aud: float = raw.get("usd_to_aud", 1.5)
        self.monthly_budget_aud = monthly_budget_aud
        self.projected_monthly_tests = projected_monthly_tests

    def unit_price(self, provider: str, operation: str) -> float:
        return self.prices.get(provider, {}).get(operation, 0.0)

    def record(self, provider: str, operation: str, units: float = 1.0, site: str | None = None) -> None:
        self.db.insert(
            "cost_events",
            {
                "ts": utcnow(),
                "provider": provider,
                "operation": operation,
                "units": units,
                "cost_usd": units * self.unit_price(provider, operation),
                "site": site,
            },
        )

    def cheapest(self, providers: list[str], operation: str) -> list[str]:
        return sorted(providers, key=lambda p: self.unit_price(p, operation))

    def summary(self) -> dict:
        by_provider = self.db.all(
            "SELECT provider, COUNT(*) AS calls, SUM(cost_usd) AS cost_usd FROM cost_events GROUP BY provider"
        )
        by_operation = self.db.all(
            "SELECT provider, operation, SUM(units) AS units, SUM(cost_usd) AS cost_usd "
            "FROM cost_events GROUP BY provider, operation ORDER BY provider, operation"
        )
        by_site = self.db.all(
            "SELECT COALESCE(site, 'shared') AS site, SUM(cost_usd) AS cost_usd FROM cost_events GROUP BY site"
        )
        total_usd = (self.db.one("SELECT SUM(cost_usd) AS total FROM cost_events") or {}).get("total") or 0.0
        tests = (self.db.one("SELECT COUNT(*) AS n FROM results") or {"n": 0})["n"]
        cost_per_test_aud = total_usd * self.usd_to_aud / tests if tests else 0.0
        # Unit economics scale linearly, so project from cost per test at the planned network volume.
        projected_aud = cost_per_test_aud * self.projected_monthly_tests

        messaging_rank = self.cheapest([p for p in self.prices if p != "local"], "message_publish")
        return {
            "currency": "AUD",
            "total_aud": round(total_usd * self.usd_to_aud, 6),
            "tests_processed": tests,
            "cost_per_test_aud": round(cost_per_test_aud, 8),
            "projected_monthly_tests": self.projected_monthly_tests,
            "projected_monthly_aud": round(projected_aud, 2),
            "monthly_budget_aud": self.monthly_budget_aud,
            "budget_used_pct": round(100 * projected_aud / self.monthly_budget_aud, 2)
            if self.monthly_budget_aud
            else 0.0,
            "by_provider": [{**r, "cost_aud": round((r["cost_usd"] or 0) * self.usd_to_aud, 6)} for r in by_provider],
            "by_operation": by_operation,
            "by_site": [{**r, "cost_aud": round((r["cost_usd"] or 0) * self.usd_to_aud, 6)} for r in by_site],
            "cheapest_messaging_provider": messaging_rank[0] if messaging_rank else None,
            "generated_at": datetime.now(UTC).isoformat(),
        }
