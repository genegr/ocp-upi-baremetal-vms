# Platforms

OpenShift is installed with the **bare-metal UPI flow** (`platform: none`), so the hypervisor only supplies
"hardware": VMs with fixed MACs that boot an ISO. Nearly the whole project is hypervisor-neutral:
- the cluster model;
- ignition and the per-node ISOs;
- DNS and HAProxy;
- bootstrap, CSR approval and users.

Only VM creation and a few checks are platform-specific. They live here, one directory per platform:

| Platform | Status | OpenTofu provider | Ansible collection |
|----------|--------|-------------------|--------------------|
| `vsphere` | implemented, tested | `vmware/vsphere` | `community.vmware` |
| `nutanix` | not implemented | `nutanix/nutanix` | `nutanix.ncp` |
| `proxmox` | not implemented | `bpg/proxmox` | `community.general` (`proxmox_*`) |

A cluster config selects one with `platform: <name>`, and that platform's settings go in a section of the same name.

## What a platform provides

A directory `platforms/<name>/` with an **OpenTofu module**, a few **Ansible task files** (`tasks/`) and
`vm_address.j2`, plus an entry in `PLATFORMS` in `plugins/filter/cluster_model.py`. Add the provider's pinned
Python libraries and collection to `docker/requirements.{txt,yml}`, so the toolbox image carries them.

### 1. `plugins/filter/cluster_model.py` → `PLATFORMS["<name>"]`

| Key | Meaning |
|-----|---------|
| `defaults` | defaults for the `<name>:` section of the cluster config |
| `required` | keys that must be set in that section; one must be `network`, the primary NIC's network name |
| `mac_prefix`, `max_fourth_octet` | optional MAC constraints (vSphere: `00:50:56:`, `0x3f`) |

### 2. OpenTofu module (`*.tf`)

**Inputs.** The `vms` role passes these through `.state/clusters/<cluster>/tofu.tfvars.json`. Credentials come as
`TF_VAR_*` from `tasks/credentials.yml`, through the task's `environment`, never as `-var`.

| Variable | Type | Meaning |
|----------|------|---------|
| `cluster_json` | string | path to `.state/clusters/<cluster>/cluster.json`; read it with `jsondecode(file(...))` |
| `iso_local_dir` | string | holds `<node>.iso` and `<node>.iso.sha256` |
| `bootstrap_enabled` | bool | create the bootstrap VM (`false` after bootstrap: destroy it) |
| `attach_iso` | bool | ISO in the CD-ROM (`false` after the install: eject it, ideally without a reboot) |

**State:** declare an empty `backend "local" {}`. The module directory is shared by every cluster, so the `vms` role
gives each cluster its own `TF_DATA_DIR` and state file under `.state/clusters/<cluster>/`. Name every platform object
after the cluster (folder, ISO directory, VM names within it) so that two clusters never collide.

**For every node** in `cluster.json` `.nodes[]`, skipping the bootstrap VM when `bootstrap_enabled` is false, the module must:
- create a VM with `cpus`, `memory_mb` and one disk of `disk_gb`. RHCOS must see the disk as `cluster.install_disk`.
- create one NIC per `nics[]` entry, in order, on the platform network named `network`, with exactly the static
  `mac` given. The keyfiles in the ISO match NICs by MAC, so this matters.
- upload `<node>.iso` and attach it as a CD-ROM. Put the sha in the ISO's name (see the vSphere module), so a
  rebuilt ISO is uploaded again, and `ignore_changes` its local source path.
- expose hardware virtualization to the guest when `nested_virt` is true. It's only ever true for nodes that run
  workloads, and only for OpenShift Virtualization.
- power the VM on.

**Output:** `nodes`, a map of node name to `{ip, role, uuid, nested_virt, ...}`. `tasks/post_apply.yml` can use it.

**Platform equivalents:**

| Requirement | vSphere | Nutanix (AHV) | Proxmox |
|-------------|---------|---------------|---------|
| nested virtualization | `nested_hv_enabled` | `enable_cpu_passthrough` | CPU type `host` |
| install disk (`install_disk`) | pvscsi → `/dev/sda` | SCSI → `/dev/sda` | virtio-blk → `/dev/vda` |
| installed disk boots before the ISO | boot order via `vmware_guest_boot_manager` (post_apply) | boot priority in the VM spec | `boot: order=scsi0;ide2` in the VM resource |

### 3. Ansible task files (`tasks/`), included by the roles with the model `c` in scope

| File | Contract |
|------|----------|
| `credentials.yml` | read credentials (vault vars or environment), assert they're set, set `platform_env` (the `TF_VAR_*` dict) |
| `preflight.yml` | check every platform object the module references exists (info modules); append messages to `preflight_failures` |
| `post_apply.yml` | anything the provider can't do, run after a successful apply (`tofu_result` in scope), e.g. vSphere boot order |
| `vm_state.yml` | set `vm_state: {<node>: {power: on\|off, nested: bool, boot_time: str}}` for all nodes, or for the names in `vm_state_nodes`; nodes without a VM are absent. `boot_time` (last power-on, `""` if unknown) tells `rolling_update` whether a change really power-cycled the VM |
| `console.yml` | optional: screenshot of `console_node` into the cluster's state dir |

`vm_address.j2` renders the OpenTofu address of a node's VM from `node`. `rolling_update` uses it for targeted applies.

Document the platform's credential variables in `clusters/vault.yml.example`, and its config section in
`cluster.example.yaml`.

## Testing a new platform

All in the toolbox (`docker compose run --rm toolbox ...`), with `-e cluster=<name>`:
1. `python3 tests/filters_test.py`, plus a config with `platform: <name>` loading cleanly (`--tags preflight`).
2. `playbooks/deploy.yml --tags preflight`: platform objects found.
3. A full `playbooks/deploy.yml`. Watch the first boot of the bootstrap VM: it must install once, then boot from disk.
4. `playbooks/status.yml` reports power and nested state through `vm_state.yml`.
5. `playbooks/rolling_update.yml` on one worker after changing its `memory_mb`.
6. Every playbook run twice reports no changes the second time.
