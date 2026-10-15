variable "cluster_json" {
  description = "Path to out/cluster.json rendered from cluster.yaml (set by the scripts)."
  type        = string
}

variable "iso_local_dir" {
  description = "Local directory holding <node>.iso and <node>.iso.sha256 (set by the scripts)."
  type        = string
}

variable "vsphere_user" {
  type      = string
  sensitive = true
}

variable "vsphere_password" {
  type      = string
  sensitive = true
}

variable "bootstrap_enabled" {
  description = "Keep the bootstrap VM. Set to false once bootstrap is complete."
  type        = bool
  default     = true
}

variable "attach_iso" {
  description = "Keep the install ISO in the CD-ROM. Set to false after the install to eject it (no reboot)."
  type        = bool
  default     = true
}
