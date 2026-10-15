---
name: ocp-upi-status
description: Read-only health check of an OpenShift UPI cluster built with this project and everything it depends on (DNS records, HAProxy pools, the VMs and their nested-virtualization flag, nodes, cluster operators, pending CSRs), via playbooks/status.yml in the toolbox container. Use whenever the user asks whether a cluster is up, healthy or finished installing, wants a sanity check before or after a change, asks why the API, console or *.apps routes don't answer, or reports any cluster problem that needs first-line triage, even if they just say "the cluster" or "the lab".
---

# Cluster status

From the toolbox on the jump box (prefix in the `ocp-upi-deploy` skill), one cluster per run (`ls clusters/*.yaml`):

```bash
ansible-playbook playbooks/status.yml -e cluster=<name>
```

It checks, in order:
1. every DNS record against `dns_servers[0]` (dig lookup);
2. the VIP ports and the HAProxy pools from the stats CSV;
3. each VM's power and nested-virtualization flag (platform `vm_state.yml`);
4. ClusterVersion, nodes, cluster operators and pending CSRs.

The `Report` task lists `ok`/`WARN`/`FAIL` lines and ends with `RESULT: HEALTHY` (maybe "with warnings"). Any FAIL
fails the playbook.

Report in plain words: say whether the cluster is usable, then list only the WARN and FAIL lines with what they mean.

## Reading the results
- An ingress pool with fewer nodes UP than exist is normal (fewer routers than nodes). It's only FAIL when no backend is UP.
- A missing bootstrap VM is expected after the bootstrap phase.
- ClusterVersion not Available / Progressing during the first ~30 min after bootstrap is an install in progress.
- `nested_virt is X, config says Y`: the VM drifted from the config. `playbooks/rolling_update.yml` reconciles it
  node by node (power cycle).
- Pending CSRs on a running cluster usually mean a new or rebuilt node. Check they belong to expected nodes, then
  run `deploy.yml --tags workers`, which only approves listed nodes.

## Deeper triage
- `playbooks/tools/oc.yml -e cluster=<name> -e "oc_args='get co'"` (`get nodes -o wide`, `adm top nodes`, …).
- Node console without SSH: include `platforms/<platform>/tasks/console.yml` with `-e console_node=<n>`. The
  screenshot lands in `.state/clusters/<name>/`; read the image.
- Node shell from the toolbox: `ssh -i .state/ssh/id_ed25519 -o UserKnownHostsFile=/dev/null core@<node-ip>`. The
  project key is authorized on nodes installed by this framework.
