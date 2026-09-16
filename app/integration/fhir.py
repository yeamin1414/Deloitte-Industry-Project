"""HL7 v2 to FHIR R4 mapping.

Produces a FHIR R4 transaction Bundle ready for a FHIR server such as
AWS HealthLake or Azure Health Data Services.
"""

from __future__ import annotations

import uuid

from app.integration.hl7v2 import ORUMessage

MEDICARE_SYSTEM = "http://ns.electronichealth.net.au/id/medicare-number"
IHI_SYSTEM = "http://ns.electronichealth.net.au/id/hi/ihi/1.0"
LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

FLAG_MAP = {"H": "H", "HH": "HH", "L": "L", "LL": "LL", "N": "N", "A": "A", "AA": "AA"}
STATUS_MAP = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled"}


def _urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _range(text: str) -> list[dict]:
    if "-" not in text:
        return [{"text": text}] if text else []
    low, _, high = text.partition("-")
    try:
        return [{"low": {"value": float(low)}, "high": {"value": float(high)}}]
    except ValueError:
        return [{"text": text}]


def to_bundle(msg: ORUMessage, patient_id: str, result_ids: list[str], held: bool = False) -> dict:
    p = msg.patient
    patient_urn, device_urn, report_urn = _urn(), _urn(), _urn()

    identifiers = [{"system": f"urn:kapconnect:mrn:{msg.sending_facility or 'site'}", "value": p["mrn"]}]
    if p["medicare"]:
        identifiers.append({"system": MEDICARE_SYSTEM, "value": p["medicare"]})
    if p["ihi"]:
        identifiers.append({"system": IHI_SYSTEM, "value": p["ihi"]})

    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": identifiers,
        "name": [{"family": p["family_name"], "given": [p["given_name"]] if p["given_name"] else []}],
        "gender": {"M": "male", "F": "female", "O": "other"}.get(p["sex"], "unknown"),
        "birthDate": p["birth_date"],
    }

    prov = msg.provenance
    device = {
        "resourceType": "Device",
        "serialNumber": prov.get("instrument_serial"),
        "deviceName": [{"name": prov.get("instrument_model") or "KapD Sigma", "type": "model-name"}],
        "version": [{"value": prov.get("firmware_version")}],
    }

    observations, obs_urns = [], []
    for obs, rid in zip(msg.observations, result_ids, strict=False):
        urn = _urn()
        obs_urns.append(urn)
        resource = {
            "resourceType": "Observation",
            "id": rid,
            "status": "preliminary" if held else STATUS_MAP.get(obs.status, "final"),
            "category": [
                {
                    "coding": [
                        {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}
                    ]
                }
            ],
            "code": {"coding": [{"system": LOINC, "code": obs.code, "display": obs.name}]},
            "subject": {"reference": patient_urn},
            "device": {"reference": device_urn},
            "effectiveDateTime": msg.collected_at.isoformat() if msg.collected_at else None,
            "issued": obs.observed_at.isoformat() if obs.observed_at else None,
            "referenceRange": _range(obs.reference_range),
        }
        if obs.value is not None:
            resource["valueQuantity"] = {"value": obs.value, "unit": obs.units, "system": UCUM, "code": obs.units}
        else:
            resource["valueString"] = obs.raw_value
        if obs.abnormal_flag in FLAG_MAP:
            resource["interpretation"] = [{"coding": [{"system": INTERPRETATION, "code": FLAG_MAP[obs.abnormal_flag]}]}]
        observations.append((urn, resource))

    report = {
        "resourceType": "DiagnosticReport",
        "status": "preliminary" if held else "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB"}]}],
        "code": {"text": "Pathology result"},
        "subject": {"reference": patient_urn},
        "identifier": [{"system": "urn:kapconnect:order", "value": msg.order_id}],
        "result": [{"reference": u} for u in obs_urns],
    }

    provenance = {
        "resourceType": "Provenance",
        "target": [{"reference": u} for u in obs_urns],
        "recorded": (msg.message_datetime.isoformat() if msg.message_datetime else None),
        "agent": [
            {"type": {"text": "performer"}, "who": {"identifier": {"value": prov.get("operator_id")}}},
            {"type": {"text": "device"}, "who": {"reference": device_urn}},
        ],
        "entity": [
            {
                "role": "source",
                "what": {"identifier": {"system": "urn:kapconnect:reagent-batch", "value": prov.get("reagent_batch")}},
            }
        ],
    }

    def entry(urn: str, resource: dict) -> dict:
        return {"fullUrl": urn, "resource": resource, "request": {"method": "POST", "url": resource["resourceType"]}}

    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            entry(patient_urn, patient),
            entry(device_urn, device),
            *[entry(u, r) for u, r in observations],
            entry(report_urn, report),
            entry(_urn(), provenance),
        ],
    }
