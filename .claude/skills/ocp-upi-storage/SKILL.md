---
name: ocp-upi-storage
description: Add or check persistent storage on an OpenShift UPI cluster built with this project - Portworx CSI (PX-CSI) on an Everpure/Pure Storage FlashArray, with a dedicated FlashArray realm, pod, policy and realm-scoped user per cluster, over NVMe/TCP or iSCSI data NICs configured with NMState - via playbooks/storage.yml in the toolbox. Use when the user wants PVCs, a StorageClass, Portworx, PX-CSI, FlashArray, a realm, NVMe/TCP or iSCSI on a cluster, asks why volumes don't provision or attach, or wants the storage NICs or multipath set up, even if they only say "storage for the cluster".
---

# Persistent storage: PX-CSI on FlashArray

Everything runs from the toolbox on the jump box (prefix in the `ocp-upi-deploy` skill), always with
`-e cluster=<name>`. The README section "Optional: persistent storage" is the reference.

## Before a first run
1. **Data path.** Find the array's data portals (`purefa_info` subset `network`; for NVMe/TCP, the `nvme-tcp`
   service and port 4420) and the platform networks (port groups) that reach them. If the array also has FC ports
   with NVMe-FC, PX-CSI can pick the wrong transport: check before choosing NVMe/TCP.
2. **Free addresses.** Never guess. Probe from a node already on those networks (ARP duplicate-address check). With
   `host: same`, each node's storage address reuses its machine-network host number.
3. **Config.** Set `roles.<role>.extra_networks`, `storage.networks`, `storage.flasharray` (endpoint, quota) and
   `storage.portworx.enabled`. The filter validates collisions, NIC indexes and names (`tests/filters_test.py`).
4. **Secret.** `vault_flasharray_api_token` (array admin) in `clusters/vault.yml`: decrypt, add and re-encrypt in the
   toolbox, passing the value over stdin, never on a command line. It is used only to create the realm objects.

## Run
`--check` first, then phase by phase. Every phase must rerun with `changed=0`.

| Tag | Notes |
|---|---|
| `nics` | `-e rolling_yes=true`; long: background + `.state/<name>.log`. NICs hot-plug, so no reboot |
| `network` | NMState; afterwards check each node has its addresses and reaches the portals |
| `node` | MachineConfig, so the MCO reboots each node in turn (~10-15 min per node; background it). Ends with a per-node check: unique NQN, multipath and udev files, `nvme_tcp` |
| `flasharray` | creates realm, pod, policy and user on a **shared array**; touches nothing else. A rerun must not create a new token |
| `portworx` | pinned operator CSV (Manual approval) + StorageCluster + StorageClass |
| `smoke` | `-e storage_smoke_test=true`: PVC + Pod; the realm pod must grow by the PVC's size; cleanup always runs |
| `teardown` | only with `--tags teardown`, irreversible: **ask the user every time**, `--check` first and show the plan it prints. It uninstalls Portworx (UninstallAndDelete), then removes hosts, realm(s) with pods and volumes, policy and user. Older layouts are passed via `cleanup_realms` / `cleanup_hosts` / `cleanup_users` / `cleanup_policies` |

Transport: `storage.portworx.san_type` picks the node MachineConfig (`99-<role>-pure-nvme-tcp` or `-iscsi`); a
switch removes the other one in the same run (one rolling reboot) and needs the storage NICs on the new VLANs (`nics`,
`network`). iSCSI gets `PURE_ISCSI_ALLOWED_CIDRS` from `storage.networks`, so Portworx ignores unreachable portals.

Modes: `storage.portworx.mode: csi` (PX-CSI only) or `enterprise`. In enterprise mode Portworx builds a Kube Datastore
per FlashArray automatically (`pxctl kubedatastore list`) and the operator adds `px-rwx-*-kubevirt-<array>` classes
pinned to it (`kube_datastore_affinity`). Enterprise means Portworx Enterprise on FlashArray
cloud drives in the realm pod, plus the PX-CSI integration for FADA volumes. The integration is one-way and needs
`vault_px_activation_code`. Day-2 operators such as OpenShift Virtualization and ACM go in `addons:` and are applied
with `playbooks/addons.yml`.

## Triage
- `playbooks/status.yml` reports policies, StorageCluster phase, the CSI driver per node, the StorageClass and realm
  usage against its quota.
- PVC Pending: `oc get storagecluster -n portworx`, the CSI Pods' logs in `portworx`, and `px-pure-secret` (realm
  user token, realm name).
- Volume created but the Pod can't attach: the data path (addresses, MTU, portal reachability), multipathd, and a
  unique hostnqn per node (identical NQNs make every node one host on the array).
- Portworx Enterprise nodes stuck Initializing / "Storage less node detected": look for the first provision attempt in
  the node journal (`journalctl -t portworx`). "failed to execute nvme cmd" with a portal-less target means the array
  also serves NVMe-FC: switch `san_type` to `ISCSI` (see CLAUDE.md).
- Never use the array-admin token in the cluster, and never reuse another cluster's realm or user.
- Array mode (`storage.flasharray.realm: false`): array-level pod `<cluster>-px-csi` and hosts, user in the built-in
  `storage_admin` policy. Needed when an array-wide consumer (OpenStack Cinder's Pure driver) shares the nodes'
  initiators: "Unable to connect or find connection to host" after "Re-using existing purity host '<realm>::…'" is
  the realm conflict. Switching a cluster = teardown of the realm (all its PVs) and a fresh `flasharray`/`portworx`.
  No migration copy offload in array mode.
