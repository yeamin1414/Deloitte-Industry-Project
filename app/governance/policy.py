"""Policy as code: continuous compliance posture.

Runtime checks mapped to the controls hospital procurement teams ask about.
The Terraform modules enforce the same rules at provisioning time, so drift
between infrastructure and application is visible in one place.
"""

from __future__ import annotations

from app.cloud.router import MultiCloudRouter
from app.config import Settings
from app.core.crypto import FieldCipher

AU_REGIONS = {
    "ap-southeast-2",
    "ap-southeast-4",
    "australiaeast",
    "australiasoutheast",
    "australiacentral",
    "australiacentral2",
    "hospital edge",
}
DEFAULT_SECRET = "change-me-in-production-please-32-bytes-min"


def _check(control: str, framework: str, ok: bool, detail: str, severity: str = "fail") -> dict:
    return {"control": control, "framework": framework, "status": "pass" if ok else severity, "detail": detail}


def evaluate(settings: Settings, router: MultiCloudRouter, cipher: FieldCipher, chain_valid: bool) -> dict:
    providers = list(router.providers.values())
    regions = {p.name: p.region for p in providers}
    outside = {n: r for n, r in regions.items() if r not in AU_REGIONS}
    escrow = cipher.escrow_status()
    production = settings.environment not in {"local", "test", "demo"}

    checks = [
        _check(
            "Data residency in Australia",
            "Privacy Act 1988 APP 8",
            not outside,
            f"Regions {regions}" if not outside else f"Outside Australia: {outside}",
        ),
        _check(
            "Immutable result provenance retention",
            "ISO 27001 A.5.33",
            settings.ledger_retention_days >= 2555,
            f"WORM retention {settings.ledger_retention_days} days (target 2555)",
        ),
        _check(
            "Envelope encryption key escrow across clouds",
            "ISO 27001 A.8.24",
            all(escrow.values()),
            f"Wrapped data key held by {escrow}",
            severity="warn",
        ),
        _check(
            "Multi cloud redundancy",
            "ISO 27001 A.8.14",
            len({p.kind for p in providers}) >= 2,
            f"Provider kinds {[p.kind for p in providers]}",
            severity="warn",
        ),
        _check(
            "Ledger hash chain integrity",
            "APP 11 security of personal information",
            chain_valid,
            "Chain verified" if chain_valid else "Chain broken, run repair from WORM replica",
        ),
        _check(
            "Edge buffer within tolerance",
            "ISO 27001 A.8.14",
            router.buffer_depth() < 1000,
            f"{router.buffer_depth()} operations waiting",
            severity="warn",
        ),
        _check(
            "Signing secret rotated from default",
            "ISO 27001 A.8.24",
            settings.jwt_secret != DEFAULT_SECRET,
            "Default development secret in use" if settings.jwt_secret == DEFAULT_SECRET else "Custom secret",
            severity="fail" if production else "warn",
        ),
    ]
    if any(p.kind == "aws" and not p.simulated for p in providers):
        checks.append(
            _check(
                "S3 Object Lock enabled",
                "ISO 27001 A.5.33",
                settings.aws_object_lock,
                "COMPLIANCE mode retention on every ledger object",
            )
        )
        checks.append(
            _check(
                "AWS KMS customer managed key",
                "ISO 27001 A.8.24",
                bool(settings.aws_kms_key_id) or settings.cloud_bootstrap,
                "KMS key configured" if settings.aws_kms_key_id else "No KMS key id",
            )
        )
    if any(p.kind == "azure" and not p.simulated for p in providers):
        uses_identity = not settings.azure_storage_connection_string
        checks.append(
            _check(
                "Azure Managed Identity instead of keys",
                "ISO 27001 A.5.17",
                uses_identity or not production,
                "DefaultAzureCredential" if uses_identity else "Connection string in use",
                severity="warn",
            )
        )
        checks.append(
            _check(
                "Azure Key Vault key configured",
                "ISO 27001 A.8.24",
                bool(settings.azure_keyvault_key_id) or not production,
                "Key Vault key id set" if settings.azure_keyvault_key_id else "Local key wrapper",
                severity="warn",
            )
        )

    summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")}
    return {"score": round(100 * summary["pass"] / len(checks), 1), "summary": summary, "checks": checks}
