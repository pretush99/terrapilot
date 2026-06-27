terraform {
  required_version = ">= 1.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }

  # Local backend — keeps state in this directory. No cloud, no credentials.
  backend "local" {}
}

# A trivial, real resource so `terraform apply` actually does something
# observable on disk. Swap this for your real cloud resources in production.
resource "local_file" "greeting" {
  content  = "Provisioned by TerraPilot in DEV. Auto-applied, no human needed.\n"
  filename = "${path.module}/greeting.txt"
}
