---
name: ocp-upi-migration-demo
description: Run, or re-run, a VMware-to-OpenShift-Virtualization migration demo with this project's MTV automation - one plan from migration.plans, FlashArray XCOPY copy offload, with or without the Portworx hook - via playbooks/migration.yml --tags run in the toolbox, then show and explain the result. The entry point for short or spoken prompts such as "run the migration demo", "migrate the VM", "migrate mtv-ubuntu-01 again", "demo MTV", "do the FADA migration", "show the migration with Portworx", "reset and migrate", or "how fast is the migration". Use it whenever the user wants a migration executed, repeated or demonstrated; for setting up MTV, the provider, maps or datastores use ocp-upi-migration instead.
---

# Migration demo (one prompt)

Reference: `docs/migration-demo.md`. Toolbox prefix and rules as in `ocp-upi-deploy`; always `-e cluster=<name>`. The
setup (MTV addon, `playbooks/migration.yml` tags array, vsphere, mtv) must already be done: `ocp-upi-migration`.

## Pick the plan
- Read `migration.plans` in `clusters/<name>.yaml`. Map the prompt onto a plan: "with Portworx" / "PX" → a plan with
  the hook (default); "FADA" / "keep on the array" / "fastest" → a plan with `portworx_hook: false`. If it's unclear
  which one, ask once, listing the plans with their description.
- Spoken prompts: "M T V", "fada", "port works", "flash array" mean MTV, FADA, Portworx, FlashArray.

## Before starting (read-only, a few seconds)
1. `oc -n openshift-mtv get plan <plan>`: Ready, or "already migrated"?
2. In the plan's target namespace: does a VM with the plan's VM name exist? Is it MTV's (labels `migration`,
   `plan-name`)? Which plan made it, and which StorageClass are its disks on?
3. If it exists, the run needs a **reset**, which deletes that migrated VM and its disks. **Ask the user** before a
   reset every time, naming the VM, namespace and plan that created it; then use `-e reset=true -e confirm=<plan>`.
   Never delete a VM without MTV's labels (the play refuses anyway).

## Run (background: 5-15 min)
```bash
tb ansible-playbook playbooks/migration.yml -e cluster=<name> --tags run -e plan_name=<plan> \
  [-e reset=true -e confirm=<plan>] > .state/<name>-migration-run.log 2>&1
```
While it runs, give the user one line per pipeline step as it changes (Monitor on the plan's
`status.migration.vms[].pipeline`, the target namespace's PVCs, `populate-…` and `…-posthook-…` pods), and point out
what to show: the plan's pipeline in the console, the FADA volume appearing in the realm pod, the hook's `pxd-…` PVC.

## Report
- From the play's final message: each step's duration, the VM (node, IP), its disks with StorageClass (Portworx class
  with the hook, FADA class without), LiveMigratable.
- One or two sentences of explanation for a demo audience: XCOPY = the array copies the VMDK internally (the
  DiskTransfer time), virt-v2v converts the guest not the disk format (raw stays raw), the hook moves the disk onto
  Portworx (replicas, PX-DR) or the VM stays on FADA (array-native snapshots/replication).
- Offer the natural next step: a live migration of the VM between workers, or the other plan (reset into it).

## If it fails
The play prints the failing step and reason. Then `oc -n openshift-mtv get plan <plan> -o yaml` (conditions and their
`suggestion`), the populator/hook pods' logs, and the triage list in `ocp-upi-migration`.
