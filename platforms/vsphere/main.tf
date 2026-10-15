locals {
  c  = jsondecode(file(var.cluster_json))
  vs = local.c.vsphere

  nodes = {
    for n in local.c.nodes : n.name => n
    if n.role != "bootstrap" || var.bootstrap_enabled
  }
  networks = toset(flatten([for n in local.c.nodes : [for nic in n.nics : nic.network]]))
  folder   = local.vs.create_folder ? vsphere_folder.cluster[0].path : local.vs.folder
}

data "vsphere_datacenter" "dc" {
  name = local.vs.datacenter
}

data "vsphere_compute_cluster" "cluster" {
  name          = local.vs.cluster
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_host" "pinned" {
  count         = local.vs.esxi_host != "" ? 1 : 0
  name          = local.vs.esxi_host
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_resource_pool" "pool" {
  count         = local.vs.resource_pool != "" ? 1 : 0
  name          = local.vs.resource_pool
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "vm" {
  name          = local.vs.datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "iso" {
  name          = local.vs.iso_datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "net" {
  for_each      = local.networks
  name          = each.value
  datacenter_id = data.vsphere_datacenter.dc.id
}

resource "vsphere_folder" "cluster" {
  count         = local.vs.create_folder ? 1 : 0
  path          = local.vs.folder
  type          = "vm"
  datacenter_id = data.vsphere_datacenter.dc.id
}

# One self-installing ISO per node. The sha in the file name makes a rebuilt ISO a new upload.
resource "vsphere_file" "iso" {
  for_each           = local.nodes
  datacenter         = local.vs.datacenter
  datastore          = local.vs.iso_datastore
  source_file        = "${var.iso_local_dir}/${each.key}.iso"
  destination_file   = "${local.vs.iso_dir}/${each.key}-${substr(trimspace(file("${var.iso_local_dir}/${each.key}.iso.sha256")), 0, 8)}.iso"
  create_directories = true

  # The ISO's identity is its content (the sha in destination_file), not where it sits locally.
  lifecycle {
    ignore_changes = [source_file]
  }
}

resource "vsphere_virtual_machine" "node" {
  for_each = local.nodes

  name             = each.key
  folder           = local.folder
  resource_pool_id = local.vs.resource_pool != "" ? data.vsphere_resource_pool.pool[0].id : data.vsphere_compute_cluster.cluster.resource_pool_id
  host_system_id   = local.vs.esxi_host != "" ? data.vsphere_host.pinned[0].id : null
  datastore_id     = data.vsphere_datastore.vm.id

  guest_id                = local.vs.guest_id
  firmware                = local.vs.firmware
  efi_secure_boot_enabled = false
  num_cpus                = each.value.cpus
  memory                  = each.value.memory_mb
  scsi_type               = "pvscsi"
  enable_disk_uuid        = true

  # KubeVirt on workers: expose VT-x/EPT (and vPMC) to the guest. Toggling it power-cycles the VM.
  nested_hv_enabled                = each.value.nested_virt
  cpu_performance_counters_enabled = each.value.nested_virt

  # RHCOS has open-vm-tools, but nothing to wait for while the ISO installs.
  wait_for_guest_net_timeout = 0
  wait_for_guest_ip_timeout  = 0

  dynamic "network_interface" {
    for_each = each.value.nics
    content {
      network_id     = data.vsphere_network.net[network_interface.value.network].id
      adapter_type   = "vmxnet3"
      use_static_mac = true
      mac_address    = network_interface.value.mac
    }
  }

  disk {
    label            = "disk0"
    size             = each.value.disk_gb
    thin_provisioned = local.vs.thin_disks
  }

  # The CD-ROM stays; only its media changes, which vSphere does without a reboot.
  cdrom {
    datastore_id  = var.attach_iso ? data.vsphere_datastore.iso.id : null
    path          = var.attach_iso ? vsphere_file.iso[each.key].destination_file : null
    client_device = var.attach_iso ? null : true
  }
}

# Boot order (disk, then CD-ROM) is set by Ansible after the apply: platforms/vsphere/tasks/post_apply.yml.
# The provider has no boot-order setting.
