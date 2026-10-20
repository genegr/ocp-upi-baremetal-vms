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
| `smoke` | `-e storage_smoke_test=true`: PVC + Pod + volume found in `<realm>::<pod>`, then cleanup |

## Triage
- `playbooks/status.yml` reports policies, StorageCluster phase, the CSI driver per node, the StorageClass and realm
  usage against its quota.
- PVC Pending: `oc get storagecluster -n portworx`, the CSI Pods' logs in `portworx`, and `px-pure-secret` (realm
  user token, realm name).
- Volume created but the Pod can't attach: the data path (addresses, MTU, portal reachability), multipathd, and a
  unique hostnqn per node (identical NQNs make every node one host on the array).
- Never use the array-admin token in the cluster, and never reuse another cluster's realm or user.
