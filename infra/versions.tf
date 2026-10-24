terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
    # KVS WebRTC のシグナリングチャネル(AWS::KinesisVideo::SignalingChannel)は hashicorp/aws にリソースがないため、
    # Cloud Control API の awscc プロバイダーで作る(webrtc.tf)
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.106"
    }
  }

  # state はローカル(infra/terraform.tfstate、gitignore 済み)
}

# awscc は default_tags を持たないので、タグはリソースごとに付ける(webrtc.tf の local.awscc_tags)
provider "awscc" {
  region = var.region
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
