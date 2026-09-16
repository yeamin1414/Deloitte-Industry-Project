output "app_url" {
  description = "Public URL of the KapConnect console."
  value       = "${var.certificate_arn == "" ? "http" : "https"}://${aws_lb.app.dns_name}"
}

output "ecr_repository_url" {
  description = "Push the container image here."
  value       = aws_ecr_repository.app.repository_url
}

output "ledger_bucket" {
  value = aws_s3_bucket.ledger.bucket
}

output "results_queue_url" {
  value = aws_sqs_queue.results.url
}

output "dead_letter_queue_url" {
  value = aws_sqs_queue.results_dlq.url
}

output "kms_key_arn" {
  value = aws_kms_key.data.arn
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}
