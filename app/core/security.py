"""Authentication and zero trust style authorisation.

* Short lived JWT access tokens (swap for Amazon Cognito or Microsoft Entra ID in production)
* Permission based RBAC checked on every request
* PHI minimisation: roles without phi:read receive masked identifiers
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "device": {"ingest"},
    "lab_technician": {"ingest", "results:read", "phi:read", "recall:run", "screening:read", "ledger:read"},
    "clinician": {"results:read", "phi:read", "alerts:ack", "recall:run"},
    "hospital_admin": {
        "results:read",
        "reviews:decide",
        "recall:run",
        "finops:read",
        "audit:read",
        "compliance:read",
        "ledger:read",
        "screening:read",
    },
    "it_operations": {
        "results:read",
        "cloud:operate",
        "finops:read",
        "audit:read",
        "compliance:read",
        "ledger:read",
        "ledger:operate",
    },
}

_SALT = b"kapconnect-demo-users"


def _hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), _SALT, 120_000).hex()


@dataclass(frozen=True)
class User:
    username: str
    role: str
    display_name: str
    site: str

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        return permission in self.permissions


# Demo identities. Real deployments federate with Cognito or Entra ID and drop this table.
DEMO_PASSWORD_HASH = _hash("kapconnect")
DEMO_USERS = {
    "tech.lee": User("tech.lee", "lab_technician", "Sam Lee", "RNSH"),
    "dr.jones": User("dr.jones", "clinician", "Dr Anna Jones", "RNSH"),
    "admin.patel": User("admin.patel", "hospital_admin", "Priya Patel", "RNSH"),
    "ops.nguyen": User("ops.nguyen", "it_operations", "Minh Nguyen", "RNSH"),
    "sigma.device": User("sigma.device", "device", "Sigma edge adapter", "RNSH"),
}


def authenticate(username: str, password: str) -> User | None:
    user = DEMO_USERS.get(username)
    if user and hmac.compare_digest(_hash(password), DEMO_PASSWORD_HASH):
        return user
    return None


def issue_token(user: User, secret: str, ttl_minutes: int) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": user.username,
        "role": user.role,
        "site": user.site,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "iss": "kapconnect",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> User:
    claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="kapconnect")
    user = DEMO_USERS.get(claims["sub"])
    if user is None or user.role != claims.get("role"):
        raise jwt.InvalidTokenError("Unknown subject")
    return user


def mask_text(value: str | None, keep: int = 1) -> str:
    if not value:
        return ""
    return value[:keep] + "*" * max(len(value) - keep, 3)


def mask_patient(patient: dict) -> dict:
    return {
        **patient,
        "family_name": mask_text(patient.get("family_name")),
        "given_name": mask_text(patient.get("given_name")),
        "birth_date": (patient.get("birth_date") or "")[:4] + "-**-**",
        "medicare": "*" * 9 + (patient.get("medicare") or "")[-1:],
        "ihi": mask_text(patient.get("ihi"), 0),
        "mrn": mask_text(patient.get("mrn"), 2),
        "address": "",
        "masked": True,
    }
