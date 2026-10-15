terraform {
  required_version = ">= 1.8.0"
  # Per-cluster state: the vms role initializes with -backend-config=path=.state/clusters/<cluster>/terraform.tfstate.
  backend "local" {}
  required_providers {
    vsphere = {
      source  = "vmware/vsphere"
      version = "2.17.1"
    }
  }
}

provider "vsphere" {
  vsphere_server       = local.vs.server
  user                 = var.vsphere_user
  password             = var.vsphere_password
  allow_unverified_ssl = local.vs.insecure
}
