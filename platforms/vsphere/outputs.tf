output "nodes" {
  description = "Created VMs: address, role, BIOS UUID (used by the post-apply steps) and nested-virtualization flag."
  value = {
    for name, vm in vsphere_virtual_machine.node : name => {
      ip          = local.nodes[name].ip
      role        = local.nodes[name].role
      uuid        = vm.uuid
      nested_virt = vm.nested_hv_enabled
      iso         = var.attach_iso ? vsphere_file.iso[name].destination_file : null
    }
  }
}
