# Migrating VMs from vSphere with MTV: setup and demo runs

How to set up VM migration from VMware vSphere to OpenShift Virtualization with the Migration Toolkit for
Virtualization (MTV), using Everpure FlashArray copy offload (XCOPY), and how to run, and re-run, a migration with one
command. Everything runs in the toolbox on the jump box (`tb` = `docker compose run --rm -T -e
ANSIBLE_VAULT_PASSWORD_FILE=/work/.state/vault-pass toolbox`), always with `-e cluster=<name>`.

## What happens during a migration

| Step (MTV pipeline) | What it does | Typical time (30 GiB Linux VM) |
|---|---|---|
| Initialize | validates, powers off the source VM (cold migration) | ~10 s |
| DiskTransfer | the XCOPY populator creates a FADA volume per disk in the cluster's realm pod and has an ESXi clone host copy the VMDK into it with `vmkfstools`: the FlashArray copies internally (XCOPY), nothing crosses the network | ~1 min |
| ImageConversion | virt-v2v adapts the guest for KVM: virtio drivers, initramfs, bootloader/fstab, removes VMware Tools. The disk stays raw | ~3 min |
| VirtualMachineCreation | creates the VirtualMachine on the migrated disks | seconds |
| PostHook (plans with `portworx_hook`) | the Portworx hook copies each FADA disk into a Portworx volume (`pxd_storageclass`), rebinds the PVCs, restarts the VM | ~3 min |

Measured end to end in the lab: about 4.3 min without the hook, 6.9 min with it.

With the hook the VM ends on Portworx Enterprise (replicas, Stork, PX-DR); without it, on its FADA volumes (one array
volume per disk, array snapshots and replication). Both are live-migratable (RWX raw block).

## One-time setup

1. **MTV** as an addon (`addons:`, package `mtv-operator`, ForkliftController with `feature_copy_offload: "true"`):
   `tb ansible-playbook playbooks/addons.yml -e cluster=<name>`.
2. **Configuration** in `clusters/<name>.yaml` under `migration:` (see `cluster.example.yaml`): the vSphere source
   and its clone hosts, the array objects that get realm access, the migration datastore(s), the network map and the
   plans. Requirements (Portworx "Rapid migration of VMs using XCOPY"): SCSI transport (iSCSI/FC, no NVMe), the
   source datastore, the FADA volumes and the Portworx cloud drives on the same array, in the same realm and pod.
3. **Setup run**, `--check` first; each phase reruns with `changed=0`:
   ```bash
   tb ansible-playbook playbooks/migration.yml -e cluster=<name> --check
   tb ansible-playbook playbooks/migration.yml -e cluster=<name>
   ```
   | Tag | Result |
   |---|---|
   | `array` | ESXi hosts + host group have realm access; migration datastore volume(s) in the realm pod, connected |
   | `vsphere` | MTV role and SSO user on the datacenter; VMFS datastore(s) mounted by the clone hosts |
   | `mtv` | provider (no VDDK, SSH clone method) Ready, FlashArray secret, MTV's restricted SSH key on the clone hosts, StorageMap/NetworkMap Ready, Portworx hook resources, every plan created (ready, or "already migrated") |
4. **Source VMs** go on the migration datastore (Storage vMotion in vCenter). Delete their VMware snapshots first:
   with snapshots, ESXi falls back to a slow software copy.

## Run a migration

```bash
tb ansible-playbook playbooks/migration.yml -e cluster=<name> --tags run -e plan_name=<plan>
```
The play applies the plan, checks it is Ready, starts a Migration, waits for it (`-e run_timeout_minutes=60`),
then checks the VMs: Running, disks on the expected StorageClass (`pxd_storageclass` with the hook, the FADA class
without), live-migratable. It prints every pipeline step's duration.

## Run it again (demos)

A Plan migrates a VM once; MTV refuses a second run while the migrated VM exists (`VMAlreadyExists`, MAC conflicts).
Reset and run in one go:
```bash
tb ansible-playbook playbooks/migration.yml -e cluster=<name> --tags run -e plan_name=<plan> \
  -e reset=true -e confirm=<plan>
```
The reset deletes, in the plan's target namespace, only the VMs named in the plan **that MTV created** (labels
`migration` and `vmID`); their disk PVCs (owned by the VM) and FADA volumes go with them. Then the plan's Migrations and
the Plan itself, which the run re-creates. A VM with the same name that MTV did not create is never deleted: the run
stops and says so. The source VM is untouched (it stays powered off after a cold migration, which is fine for the
next one).

A good demo pair: two plans for the same source VM, one with the hook (ends on Portworx) and one with
`portworx_hook: false` (stays on FADA, about 2.5 minutes shorter). Run one, show the VM (console, live migration),
then reset into the other.

## What to show while it runs

- Console: Migration → Plans → the plan's pipeline; Virtualization → VirtualMachines in the target namespace.
- The FADA volume appearing in the realm pod on the array (`<realm>::<pod>::px_<id>-pvc-…`) during DiskTransfer, and
  the populator pod (`populate-…`) in the target namespace.
- With the hook: the `pxd-…` PVC on the Portworx class and the `…-posthook-…` pod in `openshift-mtv`.
- Afterwards: the VM's PVC StorageClass, `LiveMigratable=True`, a live migration between workers.

## When it fails

`oc -n openshift-mtv get plan <plan> -o yaml` (conditions and each condition's `suggestion`), the populator and
hook pods' logs, and the `ocp-upi-migration` skill's triage list. A failed hook leaves the VM on its FADA disks,
working.
