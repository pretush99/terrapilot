terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket = "example-terraform-state-dev"
    key    = "iam/example-role/terraform.tfstate"
    region = "us-east-1"
  }
}
