import pytest

from app.cloud.local import LocalProvider
from app.core.crypto import FieldCipher
from app.mpi.matcher import PatientIndex, jaro_winkler, score_pair, soundex
from app.storage.db import Database

BASE = {
    "mrn": "RN1",
    "medicare": "2123456781",
    "family_name": "Nguyen",
    "given_name": "Minh",
    "birth_date": "1980-03-07",
    "sex": "M",
    "postcode": "2065",
}


@pytest.fixture
def index(tmp_path):
    db = Database(":memory:")
    cipher = FieldCipher(db, [LocalProvider(tmp_path, "secret")])
    return PatientIndex(db, cipher)


def test_jaro_winkler_reference_values():
    assert jaro_winkler("MARTHA", "MARHTA") == pytest.approx(0.9611, abs=1e-3)
    assert jaro_winkler("DWAYNE", "DUANE") == pytest.approx(0.84, abs=1e-2)
    assert jaro_winkler("", "x") == 0.0


def test_soundex_reference_values():
    assert soundex("Robert") == "R163"
    assert soundex("Rupert") == "R163"
    assert soundex("Tymczak") == "T522"
    assert soundex("Ashcraft") == "A261"


def test_score_bands():
    same, _ = score_pair(BASE, dict(BASE))
    typo, _ = score_pair(BASE, {**BASE, "family_name": "Ngyuen", "medicare": ""})
    other, _ = score_pair(
        BASE,
        {
            **BASE,
            "family_name": "Smith",
            "given_name": "Jack",
            "medicare": "3999999991",
            "birth_date": "1955-10-10",
            "postcode": "2000",
        },
    )
    assert same > 30
    assert 10 <= typo
    assert other < 0


def test_same_site_mrn_is_deterministic(index):
    first = index.resolve(BASE, "RNSH")
    again = index.resolve({**BASE, "given_name": "M"}, "RNSH")
    assert first.decision == "new"
    assert again.decision == "existing" and again.patient_id == first.patient_id


def test_cross_site_match_goes_to_review_and_never_auto_merges(index):
    first = index.resolve(BASE, "RNSH")
    other_site = index.resolve({**BASE, "mrn": "WM9"}, "WMD")
    assert other_site.decision == "probable"
    assert other_site.patient_id != first.patient_id
    assert other_site.golden_id == other_site.patient_id  # not linked until a human decides

    index.decide(other_site.review_id, approve=True, actor="admin")
    assert index.get(other_site.patient_id)["golden_id"] == first.golden_id
    with pytest.raises(ValueError):
        index.decide(other_site.review_id, approve=True, actor="admin")


def test_demographics_are_encrypted_at_rest(index):
    outcome = index.resolve(BASE, "RNSH")
    raw = index.db.one("SELECT enc_payload FROM patients WHERE id = ?", (outcome.patient_id,))["enc_payload"]
    assert b"Nguyen" not in bytes(raw) and b"2123456781" not in bytes(raw)
    tokens = [r["token"] for r in index.db.all("SELECT token FROM patient_index")]
    assert all("Nguyen" not in t and "1980" not in t for t in tokens)
    assert index.get(outcome.patient_id)["family_name"] == "Nguyen"
