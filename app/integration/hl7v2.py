"""HL7 v2.4 ORU^R01 parsing aligned to AS 4700.2 pathology messaging.

KapD devices add two Z segments, a standard HL7 extension mechanism:
* ZRP  result provenance: instrument serial, firmware, operator, reagent batch, site
* ZSQ  sample quality signals used by the AI pre analytical screen
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
MEDICARE_AUTHORITY = "AUSHIC"


class HL7ValidationError(ValueError):
    def __init__(self, errors: list[str], control_id: str = ""):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.control_id = control_id


@dataclass
class Observation:
    set_id: str
    code: str
    name: str
    coding_system: str
    value: float | None
    raw_value: str
    units: str
    reference_range: str
    abnormal_flag: str
    status: str
    observed_at: datetime | None


@dataclass
class ORUMessage:
    control_id: str
    sending_app: str
    sending_facility: str
    message_datetime: datetime | None
    version: str
    patient: dict
    order_id: str
    ordering_provider: str
    collected_at: datetime | None
    location: str
    observations: list[Observation]
    provenance: dict
    sample_quality: dict = field(default_factory=dict)


def unescape(value: str, enc: str) -> str:
    comp, rep, esc, sub = enc[0], enc[1], enc[2], enc[3]
    if esc not in value:
        return value
    table = {"F": "|", "S": comp, "R": rep, "E": esc, "T": sub}
    out, i = [], 0
    while i < len(value):
        if value[i] == esc:
            end = value.find(esc, i + 1)
            if end > i and value[i + 1 : end] in table:
                out.append(table[value[i + 1 : end]])
                i = end + 1
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def parse_ts(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    tz = SYDNEY
    for sign in ("+", "-"):
        if sign in value[8:]:
            idx = value.rindex(sign)
            offset = value[idx:]
            value = value[:idx]
            hours, minutes = int(offset[1:3]), int(offset[3:5])
            delta = timedelta(hours=hours, minutes=minutes)
            tz = timezone(delta if sign == "+" else -delta)
            break
    value = value.split(".")[0]
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(value))
    if not fmt:
        return None
    return datetime.strptime(value, fmt).replace(tzinfo=tz)


def to_ts(dt: datetime) -> str:
    return dt.astimezone(SYDNEY).strftime("%Y%m%d%H%M%S%z")


class Segment:
    def __init__(self, raw: str, field_sep: str, enc: str):
        self.enc = enc
        self.name = raw[:3]
        parts = raw.split(field_sep)
        if self.name == "MSH":
            # MSH-1 is the field separator itself, MSH-2 the encoding characters
            self.fields = ["MSH", field_sep, *parts[1:]]
        else:
            self.fields = parts

    def get(self, idx: int, comp: int | None = None, rep: int = 0) -> str:
        if idx >= len(self.fields):
            return ""
        value = self.fields[idx]
        if self.name == "MSH" and idx in (1, 2):
            return value
        reps = value.split(self.enc[1])
        value = reps[rep] if rep < len(reps) else ""
        if comp is not None:
            comps = value.split(self.enc[0])
            value = comps[comp - 1] if comp - 1 < len(comps) else ""
        return unescape(value, self.enc)

    def repetitions(self, idx: int) -> list[str]:
        if idx >= len(self.fields):
            return []
        return self.fields[idx].split(self.enc[1])


def _segments(message: str) -> list[Segment]:
    text = message.replace("\r\n", "\r").replace("\n", "\r").strip("\r ")
    if not text.startswith("MSH"):
        raise HL7ValidationError(["Message must start with an MSH segment"])
    field_sep = text[3]
    enc = text[4:8]
    if len(enc) < 4:
        raise HL7ValidationError(["MSH-2 must contain four encoding characters"])
    return [Segment(line, field_sep, enc) for line in text.split("\r") if line.strip()]


def _patient(pid: Segment) -> dict:
    mrn, medicare, ihi = "", "", ""
    for rep in pid.repetitions(3):
        comps = rep.split(pid.enc[0])
        ident = comps[0] if comps else ""
        authority = comps[3] if len(comps) > 3 else ""
        id_type = comps[4] if len(comps) > 4 else ""
        if id_type == "MC" or (authority == MEDICARE_AUTHORITY and id_type != "NI"):
            medicare = ident
        elif id_type == "NI":
            ihi = ident
        elif not mrn:
            mrn = ident
    birth = parse_ts(pid.get(7))
    return {
        "mrn": mrn,
        "medicare": medicare.replace(" ", ""),
        "ihi": ihi,
        "family_name": pid.get(5, 1).strip(),
        "given_name": pid.get(5, 2).strip(),
        "birth_date": birth.date().isoformat() if birth else "",
        "sex": pid.get(8),
        "address": pid.get(11, 1),
        "suburb": pid.get(11, 3),
        "postcode": pid.get(11, 5),
    }


def _num(value: str) -> float | None:
    try:
        return float(value.lstrip("<>="))
    except ValueError:
        return None


def parse_oru(message: str) -> ORUMessage:
    segs = _segments(message)
    by_name: dict[str, list[Segment]] = {}
    for s in segs:
        by_name.setdefault(s.name, []).append(s)

    msh = by_name["MSH"][0]
    control_id = msh.get(10)
    errors: list[str] = []
    if msh.get(9, 1) != "ORU" or msh.get(9, 2) != "R01":
        errors.append(f"Unsupported message type {msh.get(9)}; expected ORU^R01")
    if not control_id:
        errors.append("MSH-10 message control id is required")
    for required in ("PID", "OBR", "OBX"):
        if required not in by_name:
            errors.append(f"Missing required segment {required}")
    if errors:
        raise HL7ValidationError(errors, control_id)

    pid = by_name["PID"][0]
    obr = by_name["OBR"][0]
    patient = _patient(pid)
    if not patient["family_name"] or not patient["birth_date"]:
        errors.append("PID-5 family name and PID-7 birth date are required")

    observations = []
    for obx in by_name["OBX"]:
        raw_value = obx.get(5)
        value = _num(raw_value) if obx.get(2) in ("NM", "SN") else None
        observations.append(
            Observation(
                set_id=obx.get(1),
                code=obx.get(3, 1),
                name=obx.get(3, 2),
                coding_system=obx.get(3, 3) or "LN",
                value=value,
                raw_value=raw_value,
                units=obx.get(6, 1),
                reference_range=obx.get(7),
                abnormal_flag=obx.get(8),
                status=obx.get(11) or "F",
                observed_at=parse_ts(obx.get(14)),
            )
        )
        if not obx.get(3, 1):
            errors.append(f"OBX-{obx.get(1)} is missing an observation identifier")

    provenance = {}
    if "ZRP" in by_name:
        z = by_name["ZRP"][0]
        provenance = {
            "instrument_serial": z.get(1),
            "firmware_version": z.get(2),
            "operator_id": z.get(3),
            "reagent_batch": z.get(4),
            "site": z.get(5),
            "instrument_model": z.get(6),
        }
    else:
        errors.append("ZRP provenance segment is required for KapD devices")

    quality = {}
    if "ZSQ" in by_name:
        z = by_name["ZSQ"][0]
        quality = {
            "haemolysis_index": _num(z.get(1)) or 0.0,
            "lipaemia_index": _num(z.get(2)) or 0.0,
            "icterus_index": _num(z.get(3)) or 0.0,
            "sample_volume_ul": _num(z.get(4)) or 0.0,
            "tube_code": z.get(5),
            "expected_tube_code": z.get(6),
            "clot_signal": _num(z.get(7)) or 0.0,
            "minutes_since_collection": _num(z.get(8)) or 0.0,
        }

    if errors:
        raise HL7ValidationError(errors, control_id)

    pv1 = by_name.get("PV1", [None])[0]
    return ORUMessage(
        control_id=control_id,
        sending_app=msh.get(3, 1),
        sending_facility=msh.get(4, 1),
        message_datetime=parse_ts(msh.get(7)),
        version=msh.get(12),
        patient=patient,
        order_id=obr.get(2, 1) or obr.get(3, 1),
        ordering_provider=obr.get(16, 1),
        collected_at=parse_ts(obr.get(7)),
        location=pv1.get(3, 1) if pv1 else "",
        observations=observations,
        provenance=provenance,
        sample_quality=quality,
    )


def build_ack(control_id: str, code: str = "AA", text: str = "", sending: str = "KAPCONNECT") -> str:
    now = datetime.now(SYDNEY).strftime("%Y%m%d%H%M%S")
    safe_text = text.replace("|", " ").replace("^", " ")[:200]
    return "\r".join(
        [
            f"MSH|^~\\&|{sending}|KAPD|DEVICE|KAPD|{now}||ACK^R01|ACK{control_id}|P|2.4",
            f"MSA|{code}|{control_id}|{safe_text}",
        ]
    )
