terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket = "example-terraform-state-prod"
    key    = "rds/example-db/terraform.tfstate"
    region = "us-east-1"
  }
}
