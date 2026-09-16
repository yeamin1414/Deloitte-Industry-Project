"""Result Provenance Record (RPR) ledger.

Each result gets an RPR capturing who, what, where and when. Entries form a
SHA 256 hash chain and are signed with HMAC, then replicated to write once
storage in every cloud (S3 Object Lock and Azure immutable Blob). The
operational database copy can therefore be verified, and repaired, against
copies that nobody (including administrators) can alter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from app.cloud.router import MultiCloudRouter
from app.storage.db import Database, utcnow

GENESIS = "0" * 64


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class ProvenanceLedger:
    def __init__(self, db: Database, router: MultiCloudRouter, signing_key: bytes, retention_days: int):
        self.db = db
        self.router = router
        self.signing_key = signing_key
        self.retention_days = retention_days
        router.on_replicated = self._mark_replicated

    def _sign(self, entry_hash: str) -> str:
        return hmac.new(self.signing_key, entry_hash.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def object_key(site: str, seq: int, entry_hash: str) -> str:
        day = datetime.now(UTC).strftime("%Y/%m/%d")
        return f"rpr/{site or 'unknown'}/{day}/{seq:012d}_{entry_hash[:16]}.json"

    def append(self, entry: dict) -> dict:
        entry = {**entry, "recorded_at": utcnow()}
        with self.db.transaction() as db:
            last = db.one("SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1")
            prev_hash = last["entry_hash"] if last else GENESIS
            entry_hash = hashlib.sha256(prev_hash.encode() + canonical(entry)).hexdigest()
            signature = self._sign(entry_hash)
            cur = db.execute(
                "INSERT INTO ledger (result_id, entry, prev_hash, entry_hash, signature, replicas, created_at) "
                "VALUES (?, ?, ?, ?, ?, '{}', ?)",
                (entry["result_id"], json.dumps(entry, sort_keys=True), prev_hash, entry_hash, signature, utcnow()),
            )
            seq = cur.lastrowid

        record = {"seq": seq, "entry": entry, "prev_hash": prev_hash, "entry_hash": entry_hash, "signature": signature}
        key = self.object_key(entry.get("site", ""), seq, entry_hash)
        retain_until = datetime.now(UTC) + timedelta(days=self.retention_days)
        route = self.router.archive(key, canonical(record), retain_until, site=entry.get("site"))
        replicas = {name: {"key": key, "location": loc} for name, loc in route.replicas.items()}
        self.db.execute("UPDATE ledger SET replicas = ? WHERE seq = ?", (json.dumps(replicas), seq))
        return {**record, "object_key": key, "replicas": replicas}

    def _mark_replicated(self, key: str, provider: str, location: str) -> None:
        seq = int(key.rsplit("/", 1)[1].split("_")[0])
        row = self.db.one("SELECT replicas FROM ledger WHERE seq = ?", (seq,))
        if not row:
            return
        replicas = json.loads(row["replicas"])
        replicas[provider] = {"key": key, "location": location}
        self.db.execute("UPDATE ledger SET replicas = ? WHERE seq = ?", (json.dumps(replicas), seq))

    def get(self, seq: int) -> dict | None:
        row = self.db.one("SELECT * FROM ledger WHERE seq = ?", (seq,))
        if not row:
            return None
        return {**row, "entry": json.loads(row["entry"]), "replicas": json.loads(row["replicas"])}

    def for_result(self, result_id: str) -> dict | None:
        row = self.db.one("SELECT seq FROM ledger WHERE result_id = ?", (result_id,))
        return self.get(row["seq"]) if row else None

    def verify(self, check_replicas: int = 5) -> dict:
        """Walk the full chain, then compare the newest entries with their WORM replicas."""
        prev = GENESIS
        broken: list[dict] = []
        rows = self.db.all("SELECT seq, entry, prev_hash, entry_hash, signature FROM ledger ORDER BY seq")
        for row in rows:
            entry = json.loads(row["entry"])
            expected = hashlib.sha256(prev.encode() + canonical(entry)).hexdigest()
            problems = []
            if row["prev_hash"] != prev:
                problems.append("prev_hash does not link to previous entry")
            if expected != row["entry_hash"]:
                problems.append("entry content does not match its hash")
            if not hmac.compare_digest(self._sign(row["entry_hash"]), row["signature"]):
                problems.append("signature invalid")
            if problems:
                broken.append({"seq": row["seq"], "problems": problems})
            prev = row["entry_hash"]

        replica_checks = []
        for row in self.db.all(
            "SELECT seq, entry_hash, replicas FROM ledger ORDER BY seq DESC LIMIT ?", (check_replicas,)
        ):
            for provider, info in json.loads(row["replicas"]).items():
                if info.get("location") == "pending":
                    replica_checks.append({"seq": row["seq"], "provider": provider, "status": "pending"})
                    continue
                try:
                    obj = json.loads(self.router.read_from(provider, info["key"]))
                    ok = obj["entry_hash"] == row["entry_hash"]
                    replica_checks.append(
                        {"seq": row["seq"], "provider": provider, "status": "match" if ok else "mismatch"}
                    )
                except Exception as exc:  # noqa: BLE001
                    replica_checks.append(
                        {"seq": row["seq"], "provider": provider, "status": f"unreachable: {str(exc)[:80]}"}
                    )
        return {
            "entries": len(rows),
            "chain_valid": not broken,
            "broken": broken,
            "head_hash": prev,
            "replica_checks": replica_checks,
            "verified_at": utcnow(),
        }

    def tamper_drill(self, seq: int) -> dict:
        """Simulate an insider editing a result in the operational database."""
        row = self.get(seq)
        if not row:
            raise KeyError(seq)
        entry = row["entry"]
        entry["value"] = (entry.get("value") or 0) * 0.5
        self.db.execute("UPDATE ledger SET entry = ? WHERE seq = ?", (json.dumps(entry, sort_keys=True), seq))
        return {"seq": seq, "altered_value": entry["value"]}

    def repair(self, seq: int) -> dict:
        """Restore an entry from the first reachable immutable replica."""
        row = self.get(seq)
        if not row:
            raise KeyError(seq)
        for provider, info in row["replicas"].items():
            if info.get("location") == "pending":
                continue
            try:
                obj = json.loads(self.router.read_from(provider, info["key"]))
            except Exception:  # noqa: BLE001
                continue
            if obj["entry_hash"] == row["entry_hash"]:
                self.db.execute(
                    "UPDATE ledger SET entry = ? WHERE seq = ?", (json.dumps(obj["entry"], sort_keys=True), seq)
                )
                return {"seq": seq, "restored_from": provider}
        raise LookupError("No immutable replica available to restore from")
