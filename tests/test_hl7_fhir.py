import pytest

from app.integration.fhir import MEDICARE_SYSTEM, to_bundle
from app.integration.hl7v2 import HL7ValidationError, build_ack, parse_oru, unescape

MESSAGE = "\r".join(
    [
        "MSH|^~\\&|KAPD_SIGMA|RNSH|KAPCONNECT|RNSH|20260822142200+1000||ORU^R01|MSG0001|P|2.4|||AL|NE|AUS",
        "PID|1||RN123456^^^RNSH^MR~2123456781^^^AUSHIC^MC||O\\S\\BRIEN^Sean||19800101|M|||1 St^^Sydney^NSW^2065^AUS",
        "PV1|1|I|4B^^^RNSH",
        "OBR|1|ORD1||2823-3^Potassium^LN|||20260822140000+1000|||||||||DR1^JONES^A|||||||20260822142000|||F",
        "OBX|1|NM|2823-3^Potassium^LN||6.9|mmol/L|3.5-5.2|HH|||F|||20260822142000+1000",
        "ZRP|SIG-001|4.2.1|OP77|LOT-A1|RNSH|KapD Sigma",
        "ZSQ|12|20|1|600|LIH|LIH|0.05|30",
    ]
)


def test_parse_valid_oru():
    msg = parse_oru(MESSAGE)
    assert msg.control_id == "MSG0001"
    assert msg.patient["medicare"] == "2123456781"
    assert msg.patient["mrn"] == "RN123456"
    assert msg.patient["family_name"] == "O^BRIEN"  # escape sequence decoded
    assert msg.patient["birth_date"] == "1980-01-01"
    assert msg.ordering_provider == "DR1"
    assert msg.location == "4B"
    obs = msg.observations[0]
    assert obs.value == 6.9 and obs.abnormal_flag == "HH" and obs.units == "mmol/L"
    assert msg.collected_at.utcoffset().total_seconds() == 36000
    assert msg.provenance["reagent_batch"] == "LOT-A1"
    assert msg.sample_quality["tube_code"] == "LIH"


def test_newline_separators_are_accepted():
    assert parse_oru(MESSAGE.replace("\r", "\n")).control_id == "MSG0001"


@pytest.mark.parametrize("drop", ["PID", "OBX", "ZRP"])
def test_missing_required_segment_is_rejected(drop):
    broken = "\r".join(s for s in MESSAGE.split("\r") if not s.startswith(drop))
    with pytest.raises(HL7ValidationError) as err:
        parse_oru(broken)
    assert any(drop in e for e in err.value.errors)
    assert err.value.control_id == "MSG0001"


def test_wrong_message_type_rejected():
    with pytest.raises(HL7ValidationError):
        parse_oru(MESSAGE.replace("ORU^R01", "ADT^A01"))


def test_unescape_and_ack():
    assert unescape("A\\F\\B\\T\\C", "^~\\&") == "A|B&C"
    ack = build_ack("MSG0001", "AE", "bad|text")
    assert "MSA|AE|MSG0001|bad text" in ack


def test_fhir_bundle_contains_provenance_and_loinc():
    msg = parse_oru(MESSAGE)
    bundle = to_bundle(msg, "pat_1", ["res_1"])
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types == ["Patient", "Device", "Observation", "DiagnosticReport", "Provenance"]
    obs = bundle["entry"][2]["resource"]
    assert obs["code"]["coding"][0] == {"system": "http://loinc.org", "code": "2823-3", "display": "Potassium"}
    assert obs["valueQuantity"]["value"] == 6.9
    assert obs["interpretation"][0]["coding"][0]["code"] == "HH"
    assert obs["referenceRange"] == [{"low": {"value": 3.5}, "high": {"value": 5.2}}]
    patient = bundle["entry"][0]["resource"]
    assert {"system": MEDICARE_SYSTEM, "value": "2123456781"} in patient["identifier"]
    held = to_bundle(msg, "pat_1", ["res_1"], held=True)
    assert held["entry"][2]["resource"]["status"] == "preliminary"
