"""AWS provider.

Services used
* Amazon S3 with Object Lock (COMPLIANCE mode) and SSE KMS for the immutable provenance ledger
* Amazon SQS with a redrive policy to a dead letter queue for result events
* AWS KMS for envelope encryption of patient identifiers
"""

from __future__ import annotations

import json
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.cloud.base import (
    CloudProvider,
    KeyProvider,
    MessageHandler,
    MessageQueue,
    ObjectStore,
    ProviderUnavailable,
    QueueMessage,
)

AWS_ERRORS = (BotoCoreError, ClientError)

# Fail fast so the router can fail over instead of hanging on a degraded region.
FAST_FAIL = Config(
    retries={"max_attempts": 2, "mode": "standard"},
    connect_timeout=2,
    read_timeout=5,
)


def _client(service: str, region: str, endpoint_url: str | None):
    config = FAST_FAIL
    if endpoint_url and service == "s3":
        # Emulators such as LocalStack need path style bucket addressing.
        config = FAST_FAIL.merge(Config(s3={"addressing_style": "path"}))
    return boto3.client(service, region_name=region, endpoint_url=endpoint_url, config=config)


class S3ObjectLockStore(ObjectStore):
    def __init__(
        self, bucket: str, region: str, endpoint_url: str | None, kms_key_id: str | None, object_lock: bool = True
    ):
        self.bucket = bucket
        self.region = region
        self.kms_key_id = kms_key_id
        self.object_lock = object_lock
        self.s3 = _client("s3", region, endpoint_url)

    def ensure_bucket(self) -> None:
        """Create the bucket for local emulation. Production buckets come from Terraform."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            return
        except ClientError:
            pass
        kwargs: dict = {"Bucket": self.bucket, "ObjectLockEnabledForBucket": self.object_lock}
        if self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.s3.create_bucket(**kwargs)

    def put_immutable(self, key: str, data: bytes, retain_until: datetime) -> str:
        try:
            if self.exists(key):
                if self.get(key) == data:
                    return f"s3://{self.bucket}/{key}"
                raise PermissionError(f"Object {key} is immutable and already exists")
            kwargs: dict = {
                "Bucket": self.bucket,
                "Key": key,
                "Body": data,
                "ContentType": "application/json",
            }
            if self.object_lock:
                kwargs["ObjectLockMode"] = "COMPLIANCE"
                kwargs["ObjectLockRetainUntilDate"] = retain_until
            if self.kms_key_id:
                kwargs["ServerSideEncryption"] = "aws:kms"
                kwargs["SSEKMSKeyId"] = self.kms_key_id
            self.s3.put_object(**kwargs)
            return f"s3://{self.bucket}/{key}"
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws s3 put failed: {exc}") from exc

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws s3 get failed: {exc}") from exc

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ProviderUnavailable(f"aws s3 head failed: {exc}") from exc
        except BotoCoreError as exc:
            raise ProviderUnavailable(f"aws s3 head failed: {exc}") from exc

    def retention_of(self, key: str) -> dict:
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        return {"Mode": head.get("ObjectLockMode"), "RetainUntilDate": head.get("ObjectLockRetainUntilDate")}


class SQSQueue(MessageQueue):
    def __init__(self, prefix: str, region: str, endpoint_url: str | None, max_attempts: int = 5):
        self.prefix = prefix
        self.max_attempts = max_attempts
        self.sqs = _client("sqs", region, endpoint_url)
        self._urls: dict[str, str] = {}

    def _name(self, topic: str) -> str:
        return f"{self.prefix}-{topic}"

    def _url(self, topic: str) -> str:
        if topic not in self._urls:
            name = self._name(topic)
            try:
                self._urls[topic] = self.sqs.get_queue_url(QueueName=name)["QueueUrl"]
            except ClientError:
                self._urls[topic] = self._create_with_dlq(name)
        return self._urls[topic]

    def _create_with_dlq(self, name: str) -> str:
        dlq_url = self.sqs.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
        dlq_arn = self.sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(self.max_attempts)})
        return self.sqs.create_queue(QueueName=name, Attributes={"RedrivePolicy": redrive})["QueueUrl"]

    def publish(self, topic: str, body: str, message_id: str) -> None:
        try:
            self.sqs.send_message(
                QueueUrl=self._url(topic),
                MessageBody=body,
                MessageAttributes={"message_id": {"DataType": "String", "StringValue": message_id}},
            )
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws sqs publish failed: {exc}") from exc

    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        try:
            url = self._url(topic)
            resp = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=min(max_messages, 10),
                WaitTimeSeconds=1,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
                MessageAttributeNames=["All"],
            )
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws sqs receive failed: {exc}") from exc

        processed = 0
        for raw in resp.get("Messages", []):
            attempts = int(raw.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            mid = raw.get("MessageAttributes", {}).get("message_id", {}).get("StringValue", raw["MessageId"])
            msg = QueueMessage(message_id=mid, body=raw["Body"], attempts=attempts)
            try:
                handler(msg)
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=raw["ReceiptHandle"])
                processed += 1
            except Exception:  # noqa: BLE001
                # Exponential backoff. SQS moves the message to the DLQ after maxReceiveCount.
                self.sqs.change_message_visibility(
                    QueueUrl=url,
                    ReceiptHandle=raw["ReceiptHandle"],
                    VisibilityTimeout=min(2 ** (attempts - 1), 60) if attempts > 1 else 0,
                )
        return processed

    def dead_letters(self, topic: str, limit: int = 20) -> list[QueueMessage]:
        try:
            url = self.sqs.get_queue_url(QueueName=f"{self._name(topic)}-dlq")["QueueUrl"]
            resp = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=min(limit, 10), VisibilityTimeout=0)
        except AWS_ERRORS:
            return []
        return [QueueMessage(message_id=m["MessageId"], body=m["Body"]) for m in resp.get("Messages", [])]


class KMSKeyProvider(KeyProvider):
    def __init__(self, key_id: str, region: str, endpoint_url: str | None):
        self.key_id = key_id
        self.kms = _client("kms", region, endpoint_url)

    def generate_data_key(self) -> tuple[bytes, bytes]:
        try:
            resp = self.kms.generate_data_key(KeyId=self.key_id, KeySpec="AES_256")
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws kms generate failed: {exc}") from exc
        return resp["Plaintext"], resp["CiphertextBlob"]

    def wrap(self, plaintext_key: bytes) -> bytes:
        try:
            return self.kms.encrypt(KeyId=self.key_id, Plaintext=plaintext_key)["CiphertextBlob"]
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws kms encrypt failed: {exc}") from exc

    def unwrap(self, wrapped_key: bytes) -> bytes:
        try:
            return self.kms.decrypt(CiphertextBlob=wrapped_key, KeyId=self.key_id)["Plaintext"]
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws kms decrypt failed: {exc}") from exc


class AWSProvider(CloudProvider):
    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        queue_prefix: str,
        kms_key_id: str | None,
        endpoint_url: str | None = None,
        object_lock: bool = True,
        max_attempts: int = 5,
        fallback_keys: KeyProvider | None = None,
        bootstrap: bool = False,
    ):
        self.name = "aws"
        self.kind = "aws"
        self.region = region
        self.objects = S3ObjectLockStore(bucket, region, endpoint_url, kms_key_id, object_lock)
        self.queue = SQSQueue(queue_prefix, region, endpoint_url, max_attempts)
        if kms_key_id:
            self.keys = KMSKeyProvider(kms_key_id, region, endpoint_url)
        elif fallback_keys is not None:
            self.keys = fallback_keys
        else:
            raise ValueError("AWS provider needs KAP_AWS_KMS_KEY_ID or a fallback key provider")
        if bootstrap:
            self.objects.ensure_bucket()

    def health_check(self) -> None:
        try:
            self.objects.s3.head_bucket(Bucket=self.objects.bucket)
        except AWS_ERRORS as exc:
            raise ProviderUnavailable(f"aws unhealthy: {exc}") from exc
