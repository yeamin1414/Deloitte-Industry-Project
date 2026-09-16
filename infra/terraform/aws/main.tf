data "aws_caller_identity" "current" {}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id

  tags = {
    Project     = var.project
    Environment = var.environment
    DataClass   = "health"
    CostCentre  = "pathology-integration"
    ManagedBy   = "terraform"
  }
}

# Encryption ------------------------------------------------------------------

data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchAlarmsToEncryptedSns"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }

  statement {
    sid       = "CloudWatchLogsEncryption"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_kms_key" "data" {
  description             = "KapConnect envelope encryption and ledger encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# Immutable provenance ledger (S3 Object Lock) --------------------------------

resource "aws_s3_bucket" "ledger" {
  bucket              = "${local.name}-rpr-ledger-${local.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.ledger_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.ledger]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "ledger" {
  bucket                  = aws_s3_bucket.ledger.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.ledger.arn, "${aws_s3_bucket.ledger.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.ledger]
}

# Locked objects keep their retention when they move to a cheaper tier.
resource "aws_s3_bucket_lifecycle_configuration" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  rule {
    id     = "tier-provenance-records"
    status = "Enabled"

    filter {
      prefix = "rpr/"
    }

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
  }

  depends_on = [aws_s3_bucket_versioning.ledger]
}

# Messaging (SQS with dead letter queue) --------------------------------------

resource "aws_sqs_queue" "results_dlq" {
  name                      = "${local.name}-results-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.data.id
}

resource "aws_sqs_queue" "results" {
  name                       = "${local.name}-results"
  visibility_timeout_seconds = 30
  receive_wait_time_seconds  = 1
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.data.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.results_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "results_dlq" {
  queue_url = aws_sqs_queue.results_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.results.arn]
  })
}

# Secrets ---------------------------------------------------------------------

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "jwt" {
  name                    = "${local.name}/jwt-secret"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "jwt" {
  secret_id     = aws_secretsmanager_secret.jwt.id
  secret_string = random_password.jwt.result
}
