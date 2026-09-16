import pytest

from app.cloud.finops import CostMeter
from app.cloud.local import LocalProvider
from app.cloud.router import MultiCloudRouter
from app.config import Settings
from app.provenance.ledger import ProvenanceLedger
from app.screening.model import PreAnalyticalScreen
from app.storage.db import Database


@pytest.fixture
def ledger(tmp_path):
    db = Database(":memory:")
    providers = [
        LocalProvider(tmp_path, "k", name="aws", kind="aws", region="ap-southeast-2"),
        LocalProvider(tmp_path, "k", name="azure", kind="azure", region="australiaeast"),
    ]
    router = MultiCloudRouter(providers, db, CostMeter(db, Settings().pricing_file, 500))
    return ProvenanceLedger(db, router, b"signing-key", retention_days=30)


def _entry(i: int) -> dict:
    return {
        "result_id": f"res_{i}",
        "value": 5.0 + i,
        "reagent_batch": "LOT-1",
        "site": "RNSH",
        "instrument_serial": "SIG-001",
        "operator_id": "OP1",
    }


def test_chain_links_and_verifies(ledger):
    first = ledger.append(_entry(1))
    second = ledger.append(_entry(2))
    assert first["prev_hash"] == "0" * 64
    assert second["prev_hash"] == first["entry_hash"]
    report = ledger.verify()
    assert report["chain_valid"] and report["entries"] == 2
    assert {c["status"] for c in report["replica_checks"]} == {"match"}


def test_tampering_is_detected_and_repaired_from_worm_copy(ledger):
    for i in range(3):
        ledger.append(_entry(i))
    ledger.tamper_drill(2)
    report = ledger.verify()
    assert not report["chain_valid"]
    assert report["broken"][0]["seq"] == 2
    assert ledger.repair(2)["restored_from"] == "aws"
    assert ledger.verify()["chain_valid"]


def test_repair_falls_back_to_second_cloud(ledger):
    ledger.append(_entry(1))
    ledger.tamper_drill(1)
    ledger.router.set_outage("aws", True)
    assert ledger.repair(1)["restored_from"] == "azure"


def test_worm_objects_cannot_be_overwritten(ledger):
    record = ledger.append(_entry(1))
    store = ledger.router.providers["aws"].objects
    with pytest.raises(PermissionError):
        store.put_immutable(record["object_key"], b"forged", None)


@pytest.fixture(scope="module")
def screen():
    return PreAnalyticalScreen()


GOOD = {
    "haemolysis_index": 12,
    "lipaemia_index": 20,
    "icterus_index": 1,
    "sample_volume_ul": 700,
    "tube_code": "LIH",
    "expected_tube_code": "LIH",
    "clot_signal": 0.05,
    "minutes_since_collection": 40,
}


def test_model_quality_on_holdout(screen):
    assert screen.metrics["error_detection_recall"] >= 0.95
    assert screen.metrics["false_hold_rate"] <= 0.03
    assert screen.metrics["macro_f1"] >= 0.9


def test_clean_sample_released(screen):
    result = screen.screen(GOOD)
    assert result.label == "ok" and not result.hold


@pytest.mark.parametrize(
    "change,label",
    [
        ({"haemolysis_index": 320, "minutes_since_collection": 400}, "haemolysed"),
        ({"clot_signal": 0.85}, "clotted"),
        ({"sample_volume_ul": 110}, "insufficient_volume"),
    ],
)
def test_model_flags_common_errors(screen, change, label):
    result = screen.screen({**GOOD, **change})
    assert result.label == label and result.hold
    assert result.reasons


def test_guardrails_override_model(screen):
    wrong = screen.screen({**GOOD, "tube_code": "EDTA"})
    assert wrong.label == "wrong_tube" and wrong.source == "guardrail" and wrong.hold
    empty = screen.screen({**GOOD, "sample_volume_ul": 10})
    assert empty.source == "guardrail"


def test_missing_quality_data_is_not_screened(screen):
    assert screen.screen({}).label == "not_screened"
