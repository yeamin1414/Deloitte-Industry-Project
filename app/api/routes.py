from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from app.api.deps import current_user, get_platform, require
from app.core.security import User, authenticate, issue_token
from app.platform import RESULTS_TOPIC, Platform

api = APIRouter(prefix="/api/v1")


class OutageRequest(BaseModel):
    down: bool = Field(description="true starts an outage drill, false ends it")


class StrategyRequest(BaseModel):
    strategy: str = Field(pattern="^(priority|cost)$")


class DecisionRequest(BaseModel):
    approve: bool


class ScreeningRequest(BaseModel):
    haemolysis_index: float = 10
    lipaemia_index: float = 20
    icterus_index: float = 2
    sample_volume_ul: float = 600
    tube_code: str = "LIH"
    expected_tube_code: str = "LIH"
    clot_signal: float = 0.05
    minutes_since_collection: float = 45


class SeedRequest(BaseModel):
    count: int = Field(default=25, ge=1, le=500)
    batch: str | None = None


# auth -----------------------------------------------------------------------
@api.post("/auth/token", tags=["auth"])
def login(form: OAuth2PasswordRequestForm = Depends(), platform: Platform = Depends(get_platform)):
    user = authenticate(form.username, form.password)
    if not user:
        raise HTTPException(401, "Incorrect username or password")
    platform.db.audit(user.username, user.role, "auth.login")
    token = issue_token(user, platform.settings.jwt_secret, platform.settings.jwt_ttl_minutes)
    return {"access_token": token, "token_type": "bearer", "role": user.role, "display_name": user.display_name}


@api.get("/auth/me", tags=["auth"])
def me(user: User = Depends(current_user)):
    return {
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name,
        "site": user.site,
        "permissions": sorted(user.permissions),
    }


# ingestion ------------------------------------------------------------------
@api.post("/ingest/hl7", tags=["ingestion"], summary="Ingest an HL7 v2 ORU^R01 message (ER7 text body)")
async def ingest_hl7(
    request: Request,
    fmt: str = Query("json", alias="format", pattern="^(json|ack)$"),
    user: User = Depends(require("ingest")),
    platform: Platform = Depends(get_platform),
):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        raw = json.loads(raw).get("message", "")
    outcome = platform.ingest_hl7(raw, user)
    if fmt == "ack":
        return PlainTextResponse(
            outcome["ack"], media_type="application/hl7-v2", status_code=200 if outcome["accepted"] else 422
        )
    if not outcome["accepted"]:
        raise HTTPException(422, detail={"errors": outcome["errors"], "ack": outcome["ack"]})
    return outcome


# results and patients --------------------------------------------------------
@api.get("/results", tags=["results"])
def results(
    limit: int = Query(50, le=500),
    status: str | None = None,
    user: User = Depends(require("results:read")),
    platform: Platform = Depends(get_platform),
):
    return platform.list_results(user, limit, status)


@api.get("/results/{result_id}", tags=["results"])
def result_detail(
    result_id: str, user: User = Depends(require("results:read")), platform: Platform = Depends(get_platform)
):
    row = platform.db.one("SELECT * FROM results WHERE id = ?", (result_id,))
    if not row:
        raise HTTPException(404, "Result not found")
    bundle = json.loads(row.pop("fhir") or "{}")
    if not user.can("phi:read"):
        bundle = {"resourceType": "Bundle", "redacted": True}
    deliveries = platform.db.all("SELECT * FROM deliveries WHERE result_id = ?", (result_id,))
    return {
        "result": row,
        "provenance": platform.ledger.for_result(result_id),
        "deliveries": deliveries,
        "fhir_bundle": bundle,
    }


@api.get("/patients/reviews", tags=["patient index"])
def reviews(
    status: str = "pending", user: User = Depends(require("reviews:decide")), platform: Platform = Depends(get_platform)
):
    rows = platform.db.all("SELECT * FROM match_reviews WHERE status = ? ORDER BY score DESC", (status,))
    # Reviewers need full demographics to judge a match, so identity is shown here and the access is audited.
    for r in rows:
        r["evidence"] = json.loads(r["evidence"])
        r["incoming"] = platform.patients.get(r["incoming_patient_id"])
        r["candidate"] = platform.patients.get(r["candidate_patient_id"])
    platform.db.audit(user.username, user.role, "patient.review_queue_viewed", status, {"records": len(rows)})
    return rows


@api.post("/patients/reviews/{review_id}/decision", tags=["patient index"])
def decide(
    review_id: str,
    body: DecisionRequest,
    user: User = Depends(require("reviews:decide")),
    platform: Platform = Depends(get_platform),
):
    try:
        outcome = platform.patients.decide(review_id, body.approve, user.username)
    except KeyError as exc:
        raise HTTPException(404, "Review not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    platform.db.audit(user.username, user.role, "patient.link" if body.approve else "patient.reject", review_id)
    return outcome


@api.get("/patients/{golden_id}/history", tags=["patient index"])
def history(golden_id: str, user: User = Depends(require("results:read")), platform: Platform = Depends(get_platform)):
    return platform.patient_history(golden_id, user)


# alerts -----------------------------------------------------------------------
@api.get("/alerts", tags=["alerts"])
def alerts(
    status: str | None = None, user: User = Depends(require("results:read")), platform: Platform = Depends(get_platform)
):
    if status:
        return platform.db.all("SELECT * FROM alerts WHERE status = ? ORDER BY created_at DESC", (status,))
    return platform.db.all("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 100")


@api.post("/alerts/{alert_id}/acknowledge", tags=["alerts"])
def acknowledge(alert_id: str, user: User = Depends(require("alerts:ack")), platform: Platform = Depends(get_platform)):
    try:
        alert = platform.alerts.acknowledge(alert_id, user.username)
    except KeyError as exc:
        raise HTTPException(404, "Alert not found") from exc
    platform.db.audit(user.username, user.role, "alert.acknowledge", alert_id)
    return alert


# recall and provenance -------------------------------------------------------
@api.get("/recall/{reagent_batch}", tags=["provenance"])
def recall(reagent_batch: str, user: User = Depends(require("recall:run")), platform: Platform = Depends(get_platform)):
    return platform.recall(reagent_batch, user)


@api.get("/ledger/verify", tags=["provenance"])
def verify(user: User = Depends(require("ledger:read")), platform: Platform = Depends(get_platform)):
    return platform.ledger.verify()


@api.get("/ledger/{seq}", tags=["provenance"])
def ledger_entry(seq: int, user: User = Depends(require("ledger:read")), platform: Platform = Depends(get_platform)):
    entry = platform.ledger.get(seq)
    if not entry:
        raise HTTPException(404, "Ledger entry not found")
    return entry


@api.post(
    "/ledger/{seq}/tamper-drill",
    tags=["provenance"],
    summary="Simulate an insider editing a result in the operational database",
)
def tamper(seq: int, user: User = Depends(require("ledger:operate")), platform: Platform = Depends(get_platform)):
    try:
        outcome = platform.ledger.tamper_drill(seq)
    except KeyError as exc:
        raise HTTPException(404, "Ledger entry not found") from exc
    platform.db.audit(user.username, user.role, "ledger.tamper_drill", str(seq))
    return outcome


@api.post("/ledger/{seq}/repair", tags=["provenance"], summary="Restore an entry from immutable cloud storage")
def repair(seq: int, user: User = Depends(require("ledger:operate")), platform: Platform = Depends(get_platform)):
    try:
        outcome = platform.ledger.repair(seq)
    except KeyError as exc:
        raise HTTPException(404, "Ledger entry not found") from exc
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc
    platform.db.audit(user.username, user.role, "ledger.repair", str(seq), outcome)
    return outcome


# multi cloud operations --------------------------------------------------------
@api.get("/cloud/status", tags=["cloud operations"])
def cloud_status(user: User = Depends(current_user), platform: Platform = Depends(get_platform)):
    return platform.router.status()


@api.post(
    "/cloud/providers/{name}/outage",
    tags=["cloud operations"],
    summary="Start or end a regional outage drill for one cloud",
)
def outage(
    name: str,
    body: OutageRequest,
    user: User = Depends(require("cloud:operate")),
    platform: Platform = Depends(get_platform),
):
    try:
        platform.router.set_outage(name, body.down)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown provider {name}") from exc
    platform.db.audit(user.username, user.role, "cloud.outage_drill", name, {"down": body.down})
    return platform.router.status()


@api.post("/cloud/strategy", tags=["cloud operations"])
def strategy(
    body: StrategyRequest, user: User = Depends(require("cloud:operate")), platform: Platform = Depends(get_platform)
):
    platform.router.strategy = body.strategy
    platform.db.audit(user.username, user.role, "cloud.strategy", body.strategy)
    return platform.router.status()


@api.post("/cloud/drain", tags=["cloud operations"], summary="Replay the edge buffer now")
def drain(user: User = Depends(require("cloud:operate")), platform: Platform = Depends(get_platform)):
    platform.router.probe()
    return platform.router.drain()


@api.get("/cloud/dead-letters", tags=["cloud operations"])
def dead_letters(user: User = Depends(require("cloud:operate")), platform: Platform = Depends(get_platform)):
    return platform.router.dead_letters(RESULTS_TOPIC)


@api.post(
    "/cloud/drills/poison-message",
    tags=["cloud operations"],
    summary="Publish a malformed event to prove retry and dead letter handling",
)
def poison(user: User = Depends(require("cloud:operate")), platform: Platform = Depends(get_platform)):
    mid = f"poison_{uuid.uuid4().hex[:8]}"
    route = platform.router.publish(RESULTS_TOPIC, "{not valid json", mid)
    return {"message_id": mid, "provider": route.provider}


# finops, compliance, screening ------------------------------------------------
@api.get("/finops/costs", tags=["finops"])
def costs(user: User = Depends(require("finops:read")), platform: Platform = Depends(get_platform)):
    return platform.meter.summary()


@api.get("/compliance/posture", tags=["governance"])
def posture(user: User = Depends(require("compliance:read")), platform: Platform = Depends(get_platform)):
    return platform.compliance()


@api.get("/audit", tags=["governance"])
def audit(
    limit: int = Query(100, le=1000),
    user: User = Depends(require("audit:read")),
    platform: Platform = Depends(get_platform),
):
    return platform.db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


@api.get("/screening/model", tags=["ai screening"])
def model_card(user: User = Depends(require("screening:read")), platform: Platform = Depends(get_platform)):
    return platform.screen.metrics


@api.post("/screening/predict", tags=["ai screening"])
def predict(
    body: ScreeningRequest = Body(...),
    user: User = Depends(require("screening:read")),
    platform: Platform = Depends(get_platform),
):
    return platform.screen.screen(body.model_dump()).as_dict()


# dashboard and demo -----------------------------------------------------------
@api.get("/dashboard/summary", tags=["dashboard"])
def dashboard(user: User = Depends(current_user), platform: Platform = Depends(get_platform)):
    data = platform.summary()
    if not user.can("phi:read"):
        for alert in data["open_alerts"]:
            alert["patient_id"] = alert["patient_id"][:8] + "****"
    return data


@api.post("/demo/traffic", tags=["dashboard"], summary="Generate simulated device traffic")
def demo_traffic(body: SeedRequest, user: User = Depends(current_user), platform: Platform = Depends(get_platform)):
    from app.core.security import DEMO_USERS
    from simulator.device_simulator import DeviceSimulator

    sim = DeviceSimulator(seed=None)
    device = DEMO_USERS["sigma.device"]
    outcomes = [platform.ingest_hl7(sim.message(batch=body.batch), device) for _ in range(body.count)]
    platform.tick()
    return {
        "generated": body.count,
        "accepted": sum(o["accepted"] for o in outcomes),
        "held": sum(1 for o in outcomes if o.get("screening", {}).get("hold")),
        "critical_alerts": sum(1 for o in outcomes for r in o.get("results", []) if r["critical_alert"]),
        "requested_by": user.username,
    }
