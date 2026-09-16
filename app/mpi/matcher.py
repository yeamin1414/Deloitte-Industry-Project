"""Universal Patient Index (UPI).

Probabilistic record linkage using the Fellegi Sunter model with Jaro Winkler
string similarity. Demographics stay encrypted at rest; candidate lookup uses
HMAC blind index tokens so the database never sees plaintext search keys.

Policy: records are never merged automatically.
* probable  score >= 22  administrator confirmation required
* possible  score >= 10  human review queue
* below 10               treated as a new patient
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from app.core.crypto import FieldCipher
from app.storage.db import Database, new_id, utcnow

PROBABLE = 22.0
POSSIBLE = 10.0


def jaro_winkler(a: str, b: str, prefix_scale: float = 0.1) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    window = max(len(a), len(b)) // 2 - 1
    a_match = [False] * len(a)
    b_match = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(i + window + 1, len(b))
        for j in range(lo, hi):
            if not b_match[j] and b[j] == ch:
                a_match[i] = b_match[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    a_chars = [c for c, m in zip(a, a_match, strict=True) if m]
    b_chars = [c for c, m in zip(b, b_match, strict=True) if m]
    transpositions = sum(x != y for x, y in zip(a_chars, b_chars, strict=True)) / 2
    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4], strict=False):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1 - jaro)


def soundex(name: str) -> str:
    name = "".join(c for c in name.upper() if c.isalpha())
    if not name:
        return ""
    codes = {
        **dict.fromkeys("BFPV", "1"),
        **dict.fromkeys("CGJKQSXZ", "2"),
        **dict.fromkeys("DT", "3"),
        "L": "4",
        **dict.fromkeys("MN", "5"),
        "R": "6",
    }
    out, last = name[0], codes.get(name[0], "")
    for c in name[1:]:
        code = codes.get(c, "")
        if code and code != last:
            out += code
        if c not in "HW":
            last = code
    return (out + "000")[:4]


def _w(m: float, u: float) -> tuple[float, float]:
    return math.log2(m / u), math.log2((1 - m) / (1 - u))


WEIGHTS = {
    "medicare": _w(0.95, 0.0001),
    "birth_date": _w(0.97, 0.003),
    "family_name": _w(0.90, 0.01),
    "given_name": _w(0.88, 0.02),
    "postcode": _w(0.85, 0.05),
    "sex": _w(0.98, 0.5),
}


@dataclass
class MatchOutcome:
    patient_id: str
    golden_id: str
    decision: str  # existing, new, possible, probable
    score: float | None = None
    candidate_id: str | None = None
    review_id: str | None = None


def score_pair(a: dict, b: dict) -> tuple[float, dict]:
    evidence: dict[str, float] = {}

    def add(fieldname: str, agree: bool | None, partial: float | None = None):
        if agree is None:
            return
        w_agree, w_disagree = WEIGHTS[fieldname]
        if partial is not None:
            evidence[fieldname] = round(partial, 2)
        else:
            evidence[fieldname] = round(w_agree if agree else w_disagree, 2)

    if a.get("medicare") and b.get("medicare"):
        add("medicare", a["medicare"][:10] == b["medicare"][:10])
    if a.get("birth_date") and b.get("birth_date"):
        if a["birth_date"] == b["birth_date"]:
            add("birth_date", True)
        else:
            ya, ma, da = a["birth_date"].split("-")
            yb, mb, db_ = b["birth_date"].split("-")
            swapped = ya == yb and ma == db_ and da == mb  # day and month transposed
            add("birth_date", False, 3.0 if swapped else None)
    for name_field in ("family_name", "given_name"):
        sim = jaro_winkler(a.get(name_field, ""), b.get(name_field, ""))
        w_agree, w_disagree = WEIGHTS[name_field]
        if sim >= 0.92:
            add(name_field, True)
        elif sim >= 0.80:
            add(name_field, False, w_agree * (sim - 0.80) / 0.12)
        else:
            add(name_field, False)
    if a.get("postcode") and b.get("postcode"):
        add("postcode", a["postcode"] == b["postcode"])
    if a.get("sex") and b.get("sex"):
        add("sex", a["sex"] == b["sex"])
    return round(sum(evidence.values()), 2), evidence


class PatientIndex:
    def __init__(self, db: Database, cipher: FieldCipher):
        self.db = db
        self.cipher = cipher

    def _tokens(self, p: dict, site: str) -> list[str]:
        tokens = []
        if p.get("mrn"):
            tokens.append(self.cipher.blind_index(f"mrn|{site}|{p['mrn']}"))
        if p.get("medicare"):
            tokens.append(self.cipher.blind_index(f"medicare|{p['medicare'][:10]}"))
        if p.get("birth_date"):
            tokens.append(self.cipher.blind_index(f"dob|{p['birth_date']}"))
            tokens.append(self.cipher.blind_index(f"sdx|{soundex(p.get('family_name', ''))}|{p['birth_date'][:4]}"))
        return tokens

    def _create(self, p: dict, site: str, golden_id: str | None = None) -> str:
        pid = new_id("pat")
        self.db.insert(
            "patients",
            {
                "id": pid,
                "golden_id": golden_id or pid,
                "enc_payload": self.cipher.encrypt_json({**p, "site": site}),
                "site": site,
                "created_at": utcnow(),
            },
        )
        for token in self._tokens(p, site):
            self.db.execute("INSERT OR IGNORE INTO patient_index (token, patient_id) VALUES (?, ?)", (token, pid))
        return pid

    def get(self, patient_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM patients WHERE id = ?", (patient_id,))
        if not row:
            return None
        return {
            "id": row["id"],
            "golden_id": row["golden_id"],
            "site": row["site"],
            **self.cipher.decrypt_json(row["enc_payload"]),
        }

    def resolve(self, p: dict, site: str) -> MatchOutcome:
        mrn_token = self.cipher.blind_index(f"mrn|{site}|{p['mrn']}") if p.get("mrn") else None
        if mrn_token:
            hit = self.db.one(
                "SELECT pt.id, pt.golden_id FROM patient_index pi JOIN patients pt ON pt.id = pi.patient_id "
                "WHERE pi.token = ? LIMIT 1",
                (mrn_token,),
            )
            if hit:
                return MatchOutcome(hit["id"], hit["golden_id"], "existing")

        tokens = [t for t in self._tokens(p, site) if t != mrn_token]
        candidates: set[str] = set()
        if tokens:
            marks = ",".join("?" * len(tokens))
            rows = self.db.all(f"SELECT DISTINCT patient_id FROM patient_index WHERE token IN ({marks})", tuple(tokens))
            candidates = {r["patient_id"] for r in rows}

        best: tuple[float, dict, dict] | None = None
        for cid in candidates:
            cand = self.get(cid)
            if not cand:
                continue
            score, evidence = score_pair(p, cand)
            if best is None or score > best[0]:
                best = (score, evidence, cand)

        new_pid = self._create(p, site)
        if best is None or best[0] < POSSIBLE:
            return MatchOutcome(new_pid, new_pid, "new", best[0] if best else None)

        score, evidence, cand = best
        band = "probable" if score >= PROBABLE else "possible"
        review_id = new_id("rev")
        self.db.insert(
            "match_reviews",
            {
                "id": review_id,
                "incoming_patient_id": new_pid,
                "candidate_patient_id": cand["id"],
                "score": score,
                "band": band,
                "evidence": json.dumps(evidence),
                "status": "pending",
                "created_at": utcnow(),
            },
        )
        return MatchOutcome(new_pid, new_pid, band, score, cand["id"], review_id)

    def decide(self, review_id: str, approve: bool, actor: str) -> dict:
        review = self.db.one("SELECT * FROM match_reviews WHERE id = ?", (review_id,))
        if not review:
            raise KeyError(review_id)
        if review["status"] != "pending":
            raise ValueError(f"Review already {review['status']}")
        with self.db.transaction() as db:
            if approve:
                target = db.one("SELECT golden_id FROM patients WHERE id = ?", (review["candidate_patient_id"],))
                old = db.one("SELECT golden_id FROM patients WHERE id = ?", (review["incoming_patient_id"],))
                db.execute(
                    "UPDATE patients SET golden_id = ? WHERE golden_id = ?", (target["golden_id"], old["golden_id"])
                )
            db.execute(
                "UPDATE match_reviews SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                ("linked" if approve else "rejected", actor, utcnow(), review_id),
            )
        return self.db.one("SELECT * FROM match_reviews WHERE id = ?", (review_id,))
