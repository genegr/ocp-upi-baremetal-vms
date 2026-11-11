---
name: ocp-upi-migration
description: Migrate VMs from VMware vSphere to OpenShift Virtualization on a cluster built with this project, with the Migration Toolkit for Virtualization (MTV) and Everpure/Pure FlashArray copy offload (XCOPY), plus the Portworx post-migration hook that moves the disks onto Portworx volumes - via playbooks/migration.yml (tags array, vsphere, mtv) in the toolbox. Use when the user wants MTV, a vSphere source provider, storage or network maps, a migration plan, XCOPY or copy offload, a migration datastore on the array, the vCenter account or ESXi SSH keys for MTV, or asks why a migration plan isn't ready or a migration is slow or failed, even if they only say "move the VMs from VMware".
---

# VM migration: MTV + FlashArray copy offload

Toolbox prefix and rules as in `ocp-upi-deploy`; always `-e cluster=<name>`. Reference: README "Optional: VM migration
from vSphere", `cluster.example.yaml` (`migration:`), `CLAUDE.md` gotchas (MTV, realms, SSO).

## Before a first run
1. **MTV installed** as an addon (`mtv-operator`, newest Red Hat channel, ForkliftController with
   `feature_copy_offload: "true"`): `playbooks/addons.yml`.
2. **Reachability from the nodes** (not just the jump box): vCenter 443, ESXi 443 and 22 on the clone hosts, DNS for
   their names. Test from a node (`/dev/tcp`, `getent`). No VDDK is needed (vSAN sources excepted).
3. **Copy offload fits?** Read-only: the source datastores must be VMFS on the **same FlashArray** over **SCSI**
   (`naa.624a9370…` LUNs over FC or iSCSI; `eui.` = NVMe won't do), and the cluster's Portworx on ISCSI or FC. The
   datastore must be in the cluster's **realm and pod**: in practice a new migration datastore (`copy_offload.datastores`)
   that VMs are moved onto with Storage vMotion first.
4. **Array objects**: the ESXi hosts and their host group as the array names them (`purefa_info hosts`, look for the
   ESXi WWNs/IQNs). They get realm access; their other connections are untouched.
5. **vCenter**: the SSO system domain (`govc sso.idp.ls`), an SSO admin in the vault (`admin_*_var`), the clone hosts
   (must see the new datastore's LUN).
6. **Ask before**: realm access and a new volume on a shared array, a new SSO user/role on a shared vCenter, and
   root SSH keys on production ESXi hosts. Each is a change outside the cluster.

## Run
`--check` first; every tag reruns with `changed=0`.

| Tag | Checks after it |
|---|---|
| `array` | resource accesses listed for hosts and host group; the volume connected to the group (LUN reported) |
| `vsphere` | the MTV user logs in and sees the datacenter; the datastore is mounted by the clone hosts |
| `mtv` | Provider Ready (SSHNotReady may still list hosts that aren't clone hosts: harmless), StorageMap/NetworkMap Ready, the forced-command check answers `SSH_OK`, each Plan Ready |

Start a migration only when the user asks: the console (Migration > Plans) or a `Migration` CR (`spec.plan` with the
plan's name, namespace and uid). Watch `status.migration.vms[].pipeline` of the Plan, the PVCs in the target
namespace (`prime-…` FADA volume of the populator, then `pxd-…` from the hook) and the hook pod in `openshift-mtv`.

## Triage
- Plan not Ready: read every condition with its `suggestion` field (SSH setup, static IPs on the pod network,
  datastore not mapped).
- Disk transfer fails or falls back slow: wrong transport (NVMe), cross-pod/realm source, VMware snapshots on the VM
  (delete them first), the clone host doesn't see the LUN, or `Disk.MaxLUN` too low for many disks.
- Hook fails: the VM is left on its FADA PVCs and works; logs of the `…-posthook-…` pod; rerun only the hook with
  Portworx's `portworx-post-migration-hook.yaml`. Check `PURE_CLUSTER_PREFIX` against a real FADA volume name
  (`<realm>::<pod>::px_<uid8>-pvc-…`).
- After the migration: VMI LiveMigratable, PVC on `pxd_storageclass`, the guest's IP (DHCP on the pod network), no
  leftover `prime-…`/FADA volume in the realm pod.
