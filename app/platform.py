"""KapConnect platform service.

Wires every component together and implements the end to end flow:
device HL7 message, validation, patient resolution, AI screening, provenance,
multi cloud event routing, idempotent delivery and critical value alerting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from app.alerts.critical import AlertService
from app.cloud.base import QueueMessage
from app.cloud.factory import build_providers
from app.cloud.finops import CostMeter
from app.cloud.router import MultiCloudRouter
from app.config import Settings
from app.core import metrics
from app.core.crypto import FieldCipher
from app.core.security import User, mask_patient
from app.governance import policy
from app.integration import fhir
from app.integration.hl7v2 import HL7ValidationError, build_ack, parse_oru
from app.mpi.matcher import PatientIndex
from app.provenance.ledger import ProvenanceLedger
from app.screening.model import PreAnalyticalScreen
from app.storage.db import Database, new_id, utcnow

log = logging.getLogger(__name__)
RESULTS_TOPIC = "results"


class Platform:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_path)
        self.providers = build_providers(settings)
        self.meter = CostMeter(
            self.db, settings.pricing_file, settings.monthly_budget_aud, settings.projected_monthly_tests
        )
        self.router = MultiCloudRouter(
            self.providers,
            self.db,
            self.meter,
            strategy=settings.routing_strategy,
            threshold=settings.breaker_failure_threshold,
            cooldown=settings.breaker_cooldown_seconds,
        )
        self.cipher = FieldCipher(self.db, self.providers)
        self.patients = PatientIndex(self.db, self.cipher)
        self.screen = PreAnalyticalScreen()
        signing_key = hmac.new(self.cipher._dek, b"ledger-signing", hashlib.sha256).digest()
        self.ledger = ProvenanceLedger(self.db, self.router, signing_key, settings.ledger_retention_days)
        self.alerts = AlertService(self.db, settings.escalation_seconds)
        self.started_at = datetime.now(UTC)

    # ingestion --------------------------------------------------------------
    def ingest_hl7(self, message: str, actor: User) -> dict:
        try:
            msg = parse_oru(message)
        except HL7ValidationError as exc:
            metrics.RESULTS.labels("rejected").inc()
            self.db.audit(actor.username, actor.role, "ingest.rejected", exc.control_id, {"errors": exc.errors})
            return {
                "accepted": False,
                "ack": build_ack(exc.control_id, "AR", "; ".join(exc.errors)),
                "errors": exc.errors,
            }

        existing = self.db.all(
            "SELECT id FROM results WHERE message_control_id >= ? AND message_control_id < ?",
            (f"{msg.control_id}:", f"{msg.control_id};"),
        )
        if existing:
            return {
                "accepted": True,
                "duplicate": True,
                "ack": build_ack(msg.control_id, "AA", "duplicate ignored"),
                "result_ids": [r["id"] for r in existing],
            }

        site = msg.provenance.get("site") or msg.sending_facility
        match = self.patients.resolve(msg.patient, site)
        screening = self.screen.screen(msg.sample_quality)
        if screening.label not in ("ok", "not_screened"):
            metrics.SCREEN_FLAGS.labels(screening.label).inc()
        status = "held" if screening.hold else "released"

        received_at = utcnow()
        result_ids = [new_id("res") for _ in msg.observations]
        bundle = fhir.to_bundle(msg, match.golden_id, result_ids, held=screening.hold)

        outputs = []
        for rid, obs in zip(result_ids, msg.observations, strict=True):
            row = {
                "id": rid,
                "message_control_id": f"{msg.control_id}:{obs.set_id}",
                "patient_id": match.patient_id,
                "order_id": msg.order_id,
                "test_code": obs.code,
                "test_name": obs.name,
                "value": obs.value,
                "units": obs.units,
                "reference_range": obs.reference_range,
                "abnormal_flag": obs.abnormal_flag,
                "status": status,
                "screening_label": screening.label,
                "screening_confidence": screening.confidence,
                "instrument_serial": msg.provenance.get("instrument_serial"),
                "firmware_version": msg.provenance.get("firmware_version"),
                "operator_id": msg.provenance.get("operator_id"),
                "reagent_batch": msg.provenance.get("reagent_batch"),
                "site": site,
                "collected_at": msg.collected_at.isoformat() if msg.collected_at else None,
                "resulted_at": obs.observed_at.isoformat() if obs.observed_at else None,
                "received_at": received_at,
                "fhir": json.dumps(bundle),
            }
            self.db.insert("results", row)

            rpr = self.ledger.append(
                {
                    "result_id": rid,
                    "message_control_id": row["message_control_id"],
                    "patient_ref": match.golden_id,
                    "test_code": obs.code,
                    "value": obs.value,
                    "units": obs.units,
                    "instrument_serial": row["instrument_serial"],
                    "firmware_version": row["firmware_version"],
                    "operator_id": row["operator_id"],
                    "reagent_batch": row["reagent_batch"],
                    "site": site,
                    "collected_at": row["collected_at"],
                    "resulted_at": row["resulted_at"],
                    "screening": screening.label,
                }
            )
            self.db.execute("UPDATE results SET rpr_hash = ? WHERE id = ?", (rpr["entry_hash"], rid))

            event = {
                "type": "result.ready",
                "result_id": rid,
                "status": status,
                "site": site,
                "patient_ref": match.golden_id,
                "test_code": obs.code,
            }
            route = self.router.publish(RESULTS_TOPIC, json.dumps(event), rid, site)
            self.db.execute("UPDATE results SET event_route = ? WHERE id = ?", (route.provider, rid))

            alert = None
            if status == "released":
                alert = self.alerts.raise_if_critical({**row, "patient_id": match.golden_id})
            metrics.RESULTS.labels(status).inc()
            outputs.append(
                {
                    "result_id": rid,
                    "test": obs.name,
                    "value": obs.value,
                    "units": obs.units,
                    "status": status,
                    "event_route": route.provider,
                    "failover": route.failover,
                    "rpr": {"seq": rpr["seq"], "hash": rpr["entry_hash"], "replicas": rpr["replicas"]},
                    "critical_alert": alert["id"] if alert else None,
                }
            )

        self.db.audit(
            actor.username,
            actor.role,
            "ingest.accepted",
            msg.control_id,
            {"results": result_ids, "match": match.decision, "screening": screening.label},
        )
        ack_text = "held for sample recollection" if screening.hold else "accepted"
        return {
            "accepted": True,
            "ack": build_ack(msg.control_id, "AA", ack_text),
            "patient": {
                "id": match.patient_id,
                "golden_id": match.golden_id,
                "decision": match.decision,
                "score": match.score,
                "review_id": match.review_id,
            },
            "screening": screening.as_dict(),
            "results": outputs,
        }

    # delivery (idempotent consumer) ---------------------------------------
    def handle_event(self, msg: QueueMessage) -> None:
        provider = msg.attributes.get("provider", "unknown")
        if self.db.one("SELECT 1 FROM processed_messages WHERE message_id = ?", (msg.message_id,)):
            return  # duplicate delivery, already processed
        event = json.loads(msg.body)  # malformed bodies raise and are retried then dead lettered
        if event.get("type") != "result.ready":
            raise ValueError(f"Unsupported event type {event.get('type')}")
        result = self.db.one("SELECT * FROM results WHERE id = ?", (event["result_id"],))
        if not result:
            raise LookupError(f"Result {event['result_id']} not found")
        now = datetime.now(UTC)
        with self.db.transaction() as db:
            db.execute(
                "INSERT INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
                (msg.message_id, now.isoformat()),
            )
            if result["status"] == "released":
                for destination in ("clinician_portal", "emr_fhir_endpoint"):
                    db.insert(
                        "deliveries",
                        {
                            "id": new_id("dlv"),
                            "result_id": result["id"],
                            "destination": destination,
                            "provider": provider,
                            "delivered_at": now.isoformat(),
                        },
                    )
                latency = (now - datetime.fromisoformat(result["received_at"])).total_seconds()
                db.execute(
                    "UPDATE results SET delivered_at = ?, turnaround_seconds = ? WHERE id = ?",
                    (now.isoformat(), latency, result["id"]),
                )
                if result["collected_at"]:
                    metrics.TURNAROUND.observe((now - datetime.fromisoformat(result["collected_at"])).total_seconds())

    def process_events(self, max_messages: int = 25) -> int:
        return self.router.consume(RESULTS_TOPIC, self.handle_event, max_messages)

    def tick(self) -> dict:
        """One iteration of background work."""
        processed = self.process_events()
        self.router.probe()
        drained = self.router.drain() if self.router.buffer_depth() else {"replayed": 0}
        escalated = self.alerts.escalate_overdue()
        return {"processed": processed, "replayed": drained["replayed"], "escalated": len(escalated)}

    # queries --------------------------------------------------------------
    def patient_view(self, patient_id: str, user: User) -> dict | None:
        patient = self.patients.get(patient_id)
        if not patient:
            return None
        return patient if user.can("phi:read") else mask_patient(patient)

    def list_results(self, user: User, limit: int = 50, status: str | None = None) -> list[dict]:
        sql = (
            "SELECT r.id, r.patient_id, p.golden_id, r.test_code, r.test_name, r.value, r.units, "
            "r.reference_range, r.abnormal_flag, r.status, r.screening_label, r.screening_confidence, "
            "r.instrument_serial, r.reagent_batch, r.site, r.collected_at, r.received_at, r.delivered_at, "
            "r.turnaround_seconds, r.event_route, r.rpr_hash FROM results r "
            "JOIN patients p ON p.id = r.patient_id"
        )
        params: tuple = ()
        if status:
            sql += " WHERE r.status = ?"
            params = (status,)
        sql += " ORDER BY r.received_at DESC LIMIT ?"
        rows = self.db.all(sql, (*params, limit))
        cache: dict[str, dict | None] = {}
        for row in rows:
            if row["patient_id"] not in cache:
                cache[row["patient_id"]] = self.patient_view(row["patient_id"], user)
            p = cache[row["patient_id"]] or {}
            row["patient_name"] = f"{p.get('family_name', '')}, {p.get('given_name', '')}".strip(", ")
            row["patient_mrn"] = p.get("mrn", "")
        return rows

    def patient_history(self, golden_id: str, user: User) -> dict:
        rows = self.db.all(
            "SELECT r.* FROM results r JOIN patients p ON p.id = r.patient_id WHERE p.golden_id = ? "
            "ORDER BY r.collected_at",
            (golden_id,),
        )
        for r in rows:
            r.pop("fhir", None)
        return {"golden_id": golden_id, "patient": self.patient_view(golden_id, user), "results": rows}

    def recall(self, batch: str, user: User) -> dict:
        start = time.perf_counter()
        rows = self.db.all(
            "SELECT r.id AS result_id, r.test_name, r.value, r.units, r.status, r.site, r.collected_at, "
            "r.delivered_at, r.instrument_serial, p.golden_id FROM results r "
            "JOIN patients p ON p.id = r.patient_id WHERE r.reagent_batch = ? ORDER BY r.collected_at",
            (batch,),
        )
        patients: dict[str, dict] = {}
        for r in rows:
            gid = r["golden_id"]
            if gid not in patients:
                view = self.patient_view(gid, user) or {}
                patients[gid] = {
                    "golden_id": gid,
                    "name": f"{view.get('family_name')}, {view.get('given_name')}",
                    "mrn": view.get("mrn"),
                    "results": [],
                }
            patients[gid]["results"].append(r)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        self.db.audit(user.username, user.role, "recall.run", batch, {"patients": len(patients)})
        return {
            "reagent_batch": batch,
            "affected_results": len(rows),
            "affected_patients": len(patients),
            "delivered_results": sum(1 for r in rows if r["delivered_at"]),
            "instruments": sorted({r["instrument_serial"] for r in rows}),
            "elapsed_ms": elapsed_ms,
            "patients": list(patients.values()),
        }

    def device_health(self) -> list[dict]:
        rows = self.db.all(
            "SELECT instrument_serial AS serial, site, MAX(received_at) AS last_seen, COUNT(*) AS results, "
            "SUM(CASE WHEN screening_label NOT IN ('ok','not_screened') THEN 1 ELSE 0 END) AS flags, "
            "MAX(firmware_version) AS firmware FROM results GROUP BY instrument_serial, site ORDER BY serial"
        )
        now = datetime.now(UTC)
        for r in rows:
            idle = (now - datetime.fromisoformat(r["last_seen"])).total_seconds()
            rate = (r["flags"] or 0) / max(r["results"], 1)
            r["flag_rate"] = round(rate, 3)
            if idle > 600:
                r["status"] = "offline"
            elif rate > 0.25 and r["results"] >= 4:
                r["status"] = "check calibration"
            else:
                r["status"] = "online"
        return rows

    def summary(self) -> dict:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        totals = (
            self.db.one(
                "SELECT COUNT(*) AS tests, "
                "SUM(CASE WHEN status='held' THEN 1 ELSE 0 END) AS held, "
                "AVG(turnaround_seconds) AS avg_latency, "
                "AVG((julianday(delivered_at) - julianday(collected_at)) * 86400) AS avg_tat "
                "FROM results WHERE received_at >= ?",
                (since,),
            )
            or {}
        )
        flags = self.db.all(
            "SELECT screening_label AS label, COUNT(*) AS n FROM results WHERE received_at >= ? "
            "GROUP BY screening_label",
            (since,),
        )
        hourly = self.db.all(
            "SELECT substr(received_at, 1, 13) AS hour, COUNT(*) AS n FROM results WHERE received_at >= ? "
            "GROUP BY hour ORDER BY hour",
            (since,),
        )
        open_alerts = self.db.all(
            "SELECT * FROM alerts WHERE status != 'acknowledged' ORDER BY created_at DESC LIMIT 20"
        )
        devices = self.device_health()
        pending_reviews = self.db.one("SELECT COUNT(*) AS n FROM match_reviews WHERE status = 'pending'")
        return {
            "tests_24h": totals.get("tests") or 0,
            "held_24h": totals.get("held") or 0,
            "avg_integration_latency_s": round(totals.get("avg_latency") or 0, 3),
            "avg_collection_tat_min": round((totals.get("avg_tat") or 0) / 60, 1),
            "open_alerts": open_alerts,
            "flags": flags,
            "hourly": hourly,
            "devices": devices,
            "active_devices": sum(1 for d in devices if d["status"] != "offline"),
            "pending_reviews": pending_reviews["n"] if pending_reviews else 0,
            "cloud": self.router.status(),
            "uptime_seconds": round((datetime.now(UTC) - self.started_at).total_seconds()),
        }

    def compliance(self) -> dict:
        chain_valid = self.ledger.verify(check_replicas=0)["chain_valid"]
        return policy.evaluate(self.settings, self.router, self.cipher, chain_valid)
