terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Recommended: store state remotely with locking.
  # backend "s3" {
  #   bucket       = "your-terraform-state-bucket"
  #   key          = "kapconnect/aws/terraform.tfstate"
  #   region       = "ap-southeast-2"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
