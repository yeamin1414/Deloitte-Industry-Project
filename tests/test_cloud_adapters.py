"""Tests for the real cloud SDK adapters.

AWS runs against moto, an in process AWS emulator, so it always runs.
Azure runs against Azurite when AZURITE_CONNECTION_STRING is set (CI starts it as a service).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from moto import mock_aws

from app.cloud.local import LocalKeyProvider

RETAIN = datetime.now(UTC) + timedelta(days=1)


@pytest.fixture
def aws_env(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-2",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        yield


@pytest.fixture
def aws(aws_env):
    import boto3

    from app.cloud.aws import AWSProvider

    key_id = boto3.client("kms", region_name="ap-southeast-2").create_key()["KeyMetadata"]["KeyId"]
    return AWSProvider(
        region="ap-southeast-2",
        bucket="kapconnect-test-ledger",
        queue_prefix="kap",
        kms_key_id=key_id,
        bootstrap=True,
        max_attempts=2,
    )


def test_s3_object_lock_blocks_overwrite_and_delete(aws):
    store = aws.objects
    location = store.put_immutable("rpr/1.json", b'{"a":1}', RETAIN)
    assert location == "s3://kapconnect-test-ledger/rpr/1.json"
    assert store.retention_of("rpr/1.json")["Mode"] == "COMPLIANCE"
    assert store.put_immutable("rpr/1.json", b'{"a":1}', RETAIN) == location  # idempotent retry
    with pytest.raises(PermissionError):
        store.put_immutable("rpr/1.json", b'{"a":2}', RETAIN)
    version = store.s3.head_object(Bucket=store.bucket, Key="rpr/1.json")["VersionId"]
    with pytest.raises(Exception, match="AccessDenied"):
        store.s3.delete_object(Bucket=store.bucket, Key="rpr/1.json", VersionId=version)


def test_kms_envelope_encryption(aws):
    dek, wrapped = aws.keys.generate_data_key()
    assert len(dek) == 32 and aws.keys.unwrap(wrapped) == dek
    assert aws.keys.unwrap(aws.keys.wrap(dek)) == dek


def test_sqs_delivery_and_dead_letter_redrive(aws):
    aws.queue.publish("results", "hello", "m1")
    seen = []
    assert aws.queue.consume("results", lambda m: seen.append((m.body, m.message_id))) == 1
    assert seen == [("hello", "m1")]

    aws.queue.publish("results", "poison", "m2")

    def fail(_):
        raise ValueError("cannot process")

    for _ in range(4):
        aws.queue.consume("results", fail)
        time.sleep(1.1)
    assert [m.body for m in aws.queue.dead_letters("results")] == ["poison"]


def test_full_platform_on_aws_sdk_with_simulated_azure(aws_env, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from simulator.device_simulator import DeviceSimulator

    settings = Settings(
        data_dir=tmp_path,
        cloud_providers="aws,azure_sim",
        cloud_bootstrap=True,
        aws_ledger_bucket="kapconnect-it-ledger",
        enable_background_workers=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/token", data={"username": "sigma.device", "password": "kapconnect"}).json()[
            "access_token"
        ]
        body = client.post(
            "/api/v1/ingest/hl7",
            content=DeviceSimulator(seed=3).message(),
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        replica = body["results"][0]["rpr"]["replicas"]["aws"]["location"]
        assert replica.startswith("s3://kapconnect-it-ledger/rpr/")
        assert app.state.platform.cipher.escrow_status() == {"aws": True, "azure": True}
        assert app.state.platform.tick()["processed"] == 1


AZURITE = os.getenv("AZURITE_CONNECTION_STRING")


@pytest.fixture
def azure():
    if not AZURITE:
        pytest.skip("Set AZURITE_CONNECTION_STRING to run Azure adapter tests")
    from app.cloud.azure import AzureProvider

    return AzureProvider(
        region="australiaeast",
        container=f"ledger{uuid.uuid4().hex[:8]}",
        storage_connection_string=AZURITE,
        storage_account_url=None,
        messaging="storagequeue",
        servicebus_connection_string=None,
        servicebus_namespace=None,
        keyvault_key_id=None,
        queue_prefix=f"kap{uuid.uuid4().hex[:6]}",
        max_attempts=2,
        fallback_keys=LocalKeyProvider("test"),
        bootstrap=True,
    )


def test_azure_blob_rejects_overwrite(azure):
    azure.health_check()
    url = azure.objects.put_immutable("rpr/1.json", b'{"a":1}', RETAIN)
    assert url.endswith("/rpr/1.json")
    assert azure.objects.put_immutable("rpr/1.json", b'{"a":1}', RETAIN) == url
    with pytest.raises(PermissionError):
        azure.objects.put_immutable("rpr/1.json", b'{"a":2}', RETAIN)
    assert azure.objects.get("rpr/1.json") == b'{"a":1}'


def test_azure_queue_poison_handling(azure):
    azure.queue.publish("results", "ok", "m1")
    seen = []
    assert azure.queue.consume("results", lambda m: seen.append(m.body)) == 1
    azure.queue.publish("results", "poison", "m2")

    def fail(_):
        raise ValueError("cannot process")

    for _ in range(4):
        azure.queue.consume("results", fail)
        time.sleep(1.2)
    assert [m.body for m in azure.queue.dead_letters("results")] == ["poison"]
