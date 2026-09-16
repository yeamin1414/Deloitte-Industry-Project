"""Critical value alerting with timed escalation.

Limits are configurable examples in the style of laboratory critical value
policies. Each site must confirm limits with its own pathology governance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core import metrics
from app.storage.db import Database, new_id, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticalRule:
    loinc: str
    analyte: str
    low: float | None
    high: float | None
    units: str


RULES = {
    r.loinc: r
    for r in [
        CriticalRule("2823-3", "Potassium", 2.5, 6.2, "mmol/L"),
        CriticalRule("2951-2", "Sodium", 120, 160, "mmol/L"),
        CriticalRule("2345-7", "Glucose", 2.5, 25.0, "mmol/L"),
        CriticalRule("718-7", "Haemoglobin", 70, None, "g/L"),
        CriticalRule("89579-7", "Troponin I (high sensitivity)", None, 100, "ng/L"),
        CriticalRule("2160-0", "Creatinine", None, 600, "umol/L"),
    ]
}


def evaluate(loinc: str, value: float | None) -> str | None:
    rule = RULES.get(loinc)
    if rule is None or value is None:
        return None
    if rule.low is not None and value < rule.low:
        return f"{rule.analyte} {value:g} {rule.units} below critical limit {rule.low:g}"
    if rule.high is not None and value > rule.high:
        return f"{rule.analyte} {value:g} {rule.units} above critical limit {rule.high:g}"
    return None


class AlertService:
    def __init__(self, db: Database, escalation_seconds: int):
        self.db = db
        self.escalation_seconds = escalation_seconds

    def raise_if_critical(self, result: dict) -> dict | None:
        rule_text = evaluate(result["test_code"], result.get("value"))
        if not rule_text:
            return None
        alert = {
            "id": new_id("alr"),
            "result_id": result["id"],
            "patient_id": result["patient_id"],
            "analyte": result["test_name"],
            "value": result.get("value"),
            "units": result.get("units"),
            "rule": rule_text,
            "site": result.get("site"),
            "status": "open",
            "created_at": utcnow(),
        }
        self.db.insert("alerts", alert)
        metrics.CRITICAL_ALERTS.labels(result["test_name"]).inc()
        log.warning("critical value alert %s: %s", alert["id"], rule_text)
        return alert

    def acknowledge(self, alert_id: str, actor: str) -> dict:
        alert = self.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if not alert:
            raise KeyError(alert_id)
        if alert["status"] == "acknowledged":
            return alert
        self.db.execute(
            "UPDATE alerts SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
            (utcnow(), actor, alert_id),
        )
        return self.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))

    def escalate_overdue(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(seconds=self.escalation_seconds)).isoformat()
        overdue = self.db.all("SELECT * FROM alerts WHERE status = 'open' AND created_at <= ?", (cutoff,))
        for alert in overdue:
            self.db.execute(
                "UPDATE alerts SET status = 'escalated', escalated_at = ? WHERE id = ?", (utcnow(), alert["id"])
            )
            # Production: page the on call registrar through Amazon SNS or Azure Communication Services.
            log.warning("escalated alert %s to on call clinician", alert["id"])
        return overdue
