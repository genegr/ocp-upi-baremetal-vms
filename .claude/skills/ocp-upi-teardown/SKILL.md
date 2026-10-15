---
name: ocp-upi-teardown
description: Safely destroy an OpenShift UPI cluster built with this project (its VMs and install ISOs on the virtualization platform, optionally its DNS zone and HAProxy), via playbooks/destroy.yml in the toolbox container. Use whenever the user wants to delete, tear down, wipe or start over with a cluster, free its IPs, or clean up after a failed install.
---

# Tear down a cluster

Destroying a cluster is irreversible: every workload and persistent volume on it is lost. Before running anything:

1. Confirm with the user **which cluster** (`cluster.name`, platform folder) and that they mean destroy, not
   reinstall in place. Get an explicit yes in this conversation.
2. If the cluster is reachable, mention anything that outlives the VMs and won't be cleaned up here: storage on
   external arrays (CSI volumes, hosts and connections created by a storage operator), DNS records outside this
   project, and copies of the kubeconfig.

Then, from the toolbox on the jump box (prefix in the `ocp-upi-deploy` skill):

```bash
ansible-playbook playbooks/destroy.yml -e cluster=<name>                       # asks you to type the name
ansible-playbook playbooks/destroy.yml -e cluster=<name> -e confirm=<name>     # non-interactive (only if asked)
... -e destroy_infra=true                                                       # also remove its DNS zone + HAProxy
```

Notes:
- `destroy_infra=true` in shared-DNS mode edits a CoreDNS other environments use (see the `ocp-upi-infra` skill:
  dry-run first, explicit approval). It doesn't un-comment old records in `legacy_zone_file`; restore them from the
  `*~` backups on the Docker host if they're wanted back.
- `.state/clusters/<name>/` is kept, including `install/` with the kubeadmin password and logs. A new install starts
  with `--tags ignition -e ignition_force=true`, which keeps the old install dir as `install.<timestamp>`.
- Afterwards, verify: `playbooks/status.yml` reports the VMs as not found, the node IPs no longer answer ping, and a
  `deploy.yml --tags vms --check` plan would create everything from scratch.
