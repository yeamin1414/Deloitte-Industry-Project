from datetime import UTC, datetime, timedelta

from tests.conftest import ingest


def test_health_and_docs(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    assert "kapconnect_cloud_calls_total" in client.get("/metrics").text
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/").status_code == 200


def test_login_rejects_bad_password(client):
    resp = client.post("/api/v1/auth/token", data={"username": "dr.jones", "password": "nope"})
    assert resp.status_code == 401


def test_requests_without_token_are_rejected(client):
    assert client.get("/api/v1/results").status_code == 401


def test_rbac_blocks_clinician_from_cloud_operations(client, auth):
    resp = client.post("/api/v1/cloud/providers/aws/outage", json={"down": True}, headers=auth("dr.jones"))
    assert resp.status_code == 403
    resp = client.post("/api/v1/ingest/hl7", content="MSH", headers=auth("dr.jones"))
    assert resp.status_code == 403


def test_ingest_end_to_end(client, auth, sim, platform):
    resp = ingest(client, auth, sim.message(issue="none", critical=False))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] and "MSA|AA|" in body["ack"]
    result = body["results"][0]
    assert result["status"] == "released"
    assert result["event_route"] == "aws"
    assert set(result["rpr"]["replicas"]) == {"aws", "azure"}

    platform.tick()
    detail = client.get(f"/api/v1/results/{result['result_id']}", headers=auth("dr.jones")).json()
    assert detail["result"]["delivered_at"] is not None
    assert {d["destination"] for d in detail["deliveries"]} == {"clinician_portal", "emr_fhir_endpoint"}
    assert detail["fhir_bundle"]["resourceType"] == "Bundle"
    assert detail["provenance"]["entry"]["instrument_serial"]


def test_duplicate_device_retry_is_idempotent(client, auth, sim):
    message = sim.message()
    first = ingest(client, auth, message).json()
    second = ingest(client, auth, message).json()
    assert second["duplicate"] is True
    assert second["result_ids"] == [r["result_id"] for r in first["results"]]


def test_invalid_message_returns_hl7_reject(client, auth):
    resp = client.post(
        "/api/v1/ingest/hl7?format=ack",
        content="MSH|^~\\&|X|Y|Z|W|20260101||ORU^R01|C1|P|2.4",
        headers={**auth("sigma.device"), "Content-Type": "application/hl7-v2"},
    )
    assert resp.status_code == 422
    assert "MSA|AR|C1" in resp.text


def test_bad_sample_is_held_and_not_alerted(client, auth, sim):
    body = ingest(client, auth, sim.message(issue="wrong_tube", critical=True)).json()
    assert body["screening"]["hold"] is True
    assert body["results"][0]["status"] == "held"
    assert body["results"][0]["critical_alert"] is None
    assert "held for sample recollection" in body["ack"]


def test_critical_value_alert_acknowledge_and_escalation(client, auth, sim, platform):
    msg = sim.message(issue="none", critical=True)
    while "89579-7" not in msg and "718-7" not in msg and "2823-3" not in msg:
        msg = sim.message(issue="none", critical=True)
    body = ingest(client, auth, msg).json()
    alert_id = body["results"][0]["critical_alert"]
    assert alert_id

    later = datetime.now(UTC) + timedelta(minutes=16)
    escalated = platform.alerts.escalate_overdue(now=later)
    assert [a["id"] for a in escalated] == [alert_id]

    resp = client.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers=auth("dr.jones"))
    assert resp.json()["status"] == "acknowledged"
    assert resp.json()["acknowledged_by"] == "dr.jones"


def test_it_operations_never_sees_patient_identity(client, auth, sim):
    ingest(client, auth, sim.message())
    clinician = client.get("/api/v1/results", headers=auth("dr.jones")).json()[0]
    ops = client.get("/api/v1/results", headers=auth("ops.nguyen")).json()[0]
    assert "*" not in clinician["patient_name"]
    assert "*" in ops["patient_name"]
    assert clinician["patient_mrn"] != ops["patient_mrn"]


def test_outage_drill_failover_and_recovery(client, auth, sim, platform):
    ops = auth("ops.nguyen")
    status = client.post("/api/v1/cloud/providers/aws/outage", json={"down": True}, headers=ops).json()
    assert status["active_route"] == "azure"
    result = ingest(client, auth, sim.message()).json()["results"][0]
    assert result["event_route"] == "azure" and result["failover"]
    assert result["rpr"]["replicas"]["aws"]["location"] == "pending"

    client.post("/api/v1/cloud/providers/aws/outage", json={"down": False}, headers=ops)
    assert client.post("/api/v1/cloud/drain", headers=ops).json()["remaining"] == 0
    entry = client.get(f"/api/v1/ledger/{result['rpr']['seq']}", headers=ops).json()
    assert entry["replicas"]["aws"]["location"].startswith("file://")


def test_total_outage_buffers_at_edge_then_delivers(client, auth, sim, platform):
    ops = auth("ops.nguyen")
    for name in ("aws", "azure"):
        client.post(f"/api/v1/cloud/providers/{name}/outage", json={"down": True}, headers=ops)
    result = ingest(client, auth, sim.message(issue="none")).json()["results"][0]
    assert result["event_route"] == "edge_buffer"
    assert client.get("/readyz").json()["cloud"]["edge_buffer_depth"] == 3

    for name in ("aws", "azure"):
        client.post(f"/api/v1/cloud/providers/{name}/outage", json={"down": False}, headers=ops)
    platform.tick()
    platform.tick()
    detail = client.get(f"/api/v1/results/{result['result_id']}", headers=auth("dr.jones")).json()
    assert detail["result"]["delivered_at"] is not None


def test_poison_message_drill_lands_in_dead_letter_queue(client, auth, platform):
    ops = auth("ops.nguyen")
    mid = client.post("/api/v1/cloud/drills/poison-message", headers=ops).json()["message_id"]
    for _ in range(platform.settings.max_delivery_attempts + 1):
        platform.process_events()
    letters = client.get("/api/v1/cloud/dead-letters", headers=ops).json()
    assert mid in [m["message_id"] for m in letters["aws"]]


def test_recall_finds_every_affected_patient_quickly(client, auth, sim):
    for _ in range(30):
        ingest(client, auth, sim.message(batch="LOT-RECALL-01"))
    for _ in range(10):
        ingest(client, auth, sim.message(batch="LOT-SAFE-02"))
    report = client.get("/api/v1/recall/LOT-RECALL-01", headers=auth("tech.lee")).json()
    assert report["affected_results"] == 30
    assert 0 < report["affected_patients"] <= 30
    assert report["elapsed_ms"] < 30_000


def test_tamper_drill_and_repair_via_api(client, auth, sim):
    ops = auth("ops.nguyen")
    seq = ingest(client, auth, sim.message()).json()["results"][0]["rpr"]["seq"]
    client.post(f"/api/v1/ledger/{seq}/tamper-drill", headers=ops)
    assert client.get("/api/v1/ledger/verify", headers=ops).json()["chain_valid"] is False
    assert client.post(f"/api/v1/ledger/{seq}/repair", headers=ops).status_code == 200
    assert client.get("/api/v1/ledger/verify", headers=ops).json()["chain_valid"] is True


def test_review_queue_decision_links_patients(client, auth, sim, platform):
    base = sim.message()
    other_site = base.replace("|RNSH|", "|XSITE|").replace("|RPA|", "|XSITE|").replace("|WMD|", "|XSITE|")
    other_site = other_site.replace("MSH|^~\\&|", "MSH|^~\\&|").split("\r")
    other_site[0] = other_site[0].replace(other_site[0].split("|")[9], "SECOND01")
    ingest(client, auth, base)
    body = ingest(client, auth, "\r".join(other_site)).json()
    review_id = body["patient"]["review_id"]
    assert body["patient"]["decision"] in {"probable", "possible"}
    admin = auth("admin.patel")
    queue = client.get("/api/v1/patients/reviews", headers=admin).json()
    assert review_id in [r["id"] for r in queue]
    decision = client.post(f"/api/v1/patients/reviews/{review_id}/decision", json={"approve": True}, headers=admin)
    assert decision.json()["status"] == "linked"


def test_finops_compliance_audit_and_dashboard(client, auth, sim):
    client.post("/api/v1/demo/traffic", json={"count": 20}, headers=auth("admin.patel"))
    admin = auth("admin.patel")
    costs = client.get("/api/v1/finops/costs", headers=admin).json()
    assert costs["tests_processed"] == 20 and costs["cost_per_test_aud"] > 0
    posture = client.get("/api/v1/compliance/posture", headers=admin).json()
    assert posture["summary"]["fail"] == 0
    assert any(c["control"] == "Data residency in Australia" and c["status"] == "pass" for c in posture["checks"])
    audit = client.get("/api/v1/audit", headers=admin).json()
    assert any(a["action"] == "auth.login" for a in audit)
    summary = client.get("/api/v1/dashboard/summary", headers=auth("tech.lee")).json()
    assert summary["tests_24h"] == 20 and summary["devices"]
    model = client.get("/api/v1/screening/model", headers=admin).json()
    assert model["error_detection_recall"] > 0.9


def test_review_queue_access_is_audited(client, auth, sim):
    for _ in range(40):
        ingest(client, auth, sim.message())
    admin = auth("admin.patel")
    client.get("/api/v1/patients/reviews", headers=admin)
    audit = client.get("/api/v1/audit", headers=admin).json()
    assert any(a["action"] == "patient.review_queue_viewed" for a in audit)
