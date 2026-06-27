terraform {
  required_version = ">= 1.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }

  backend "local" {}
}

# In PROD, applying this requires a lead's sign-off (PR + Slack) — TerraPilot
# refuses to apply until the change request is approved.
resource "local_file" "greeting" {
  content  = "Provisioned by TerraPilot in PROD. Applied only after lead sign-off.\n"
  filename = "${path.module}/greeting.txt"
}
