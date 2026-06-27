terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "example-terraform-state-dev"
    key    = "dynamodb/example-table/terraform.tfstate"
    region = "us-east-1"
  }
}
