---
name: ocp-upi-deploy
description: Install, reinstall or extend an OpenShift UPI cluster on virtual machines with this project (bare-metal flow, self-installing ISOs with static IPs, OpenTofu for the VMs, Ansible for everything, all in a toolbox container on a Docker jump box). Use whenever the user wants to deploy or rebuild a cluster, run or resume a phase (tools, infra, preflight, ignition, isos, vms, bootstrap, workers, complete), regenerate ignition or ISOs, recover a stuck install, change VM sizing, add nodes, extra NICs, users, a compact 3-node topology, or nested virtualization (OpenShift Virtualization).
---

# Deploy an OpenShift UPI cluster (bare metal on VMs)

Read `CLAUDE.md` first: the hard rules and verified gotchas.

## Where things run
- **Authoring machine**: edit files only, then `bash tools/sync.sh` (target in `.sync.env`: `SYNC_HOST`, `SYNC_DIR`).
  tar never deletes, so after removing or renaming a file, `rm` it on the jump box too.
- **Jump box** (Docker only), checkout at `<SYNC_DIR>`. Every tool runs in the toolbox:
  ```bash
  ssh <SYNC_HOST> 'cd <SYNC_DIR> && docker compose run --rm -T \
    -e ANSIBLE_VAULT_PASSWORD_FILE=/work/.state/vault-pass toolbox <cmd>'
  ```
  Jump-box-only files are `.state/` (vault-pass, ssh key, caches, per-cluster state, kubeconfigs) and
  `clusters/vault.yml`. Never overwrite or delete them; sync excludes them.
- **Always pass `-e cluster=<name>`** (`ls clusters/*.yaml`) and confirm which cluster a change is for.

## The flow
`<cmd>` = `ansible-playbook playbooks/deploy.yml -e cluster=<name> [--tags <phase>] [--check]`

| Phase (tag) | Does | Time |
|-------------|------|------|
| `tools` | pin release, openshift-install/oc into `.state/bin/<rel>/`, RHCOS ISO, project SSH key | 1–3 min (first time) |
| `infra` | DNS zone + HAProxy (see the `ocp-upi-infra` skill: shared CoreDNS needs consent) | 1 min |
| `preflight` | DNS, LB ports, node IPs free, platform objects, secrets | 20 s |
| `ignition` | install-config + manifests + ignition. **Bootstrap certs valid 24 h** | 10 s |
| `isos` | one ISO per node; rebuilt only if its inputs digest changed | ~10 s per node |
| `vms` | OpenTofu apply (ISO uploads, VMs, power on) + boot order | 5–8 min (vSphere) |
| `bootstrap` | waits for `kube-system/bootstrap` complete + `EtcdBootstrapMemberRemoved`, then destroys the bootstrap VM | ~20 min after power-on |
| `workers` | `ocp_csr_approve` until workers are Ready (compact: no-op) | ~25 min after power-on |
| `complete` | ClusterVersion/operators healthy, eject ISOs, users (if enabled) | 15–30 min |

A full `deploy.yml` takes about 45 minutes. Run it in the background with output to `.state/<name>-deploy.log`
(`nohup … > .state/x.log 2>&1 &` on the jump box), then grep `fatal|PLAY RECAP`. A step piped through a dying ssh
session can be lost.

Access: `.state/clusters/<name>/install/auth/{kubeconfig,kubeadmin-password}`. Run oc with
`playbooks/tools/oc.yml -e "oc_args='get co'"`. Never print the password or paste the kubeconfig; give the path.

## What "normal" looks like
- Every node answers ping on its static IP within a minute of power-on (the live ISO). SSH comes only after the installed boot.
- NIC names depend on the platform (vSphere vmx-21: `ens33`, …). That's harmless: keyfiles match by MAC.
- Workers reboot once (OS update) before their CSRs appear, about 20 min in.
- After bootstrap, an ingress pool with one node DOWN is normal.
- The installer warns if the machine network overlaps Docker's default bridge. That's harmless.

## Recovery
| Symptom | Fix |
|---------|-----|
| isos refuses: bootstrap ignition > 20 h old | fresh install: `--tags ignition -e ignition_force=true`, then `isos`, `vms`. Nodes already installed from old ignition must be recreated: `-e '{"vms_targets": ["<address>"]}'` won't replace; destroy the cluster or replace that VM via OpenTofu |
| A node reinstalls in a loop | the installed disk must boot before the CD-ROM: rerun `--tags vms` (post_apply sets the boot order) |
| Bootstrap never completes | on the bootstrap node: `journalctl -b -u bootkube.service -u release-image.service`; check DNS (`api-int`) and HAProxy 22623/6443 (`--tags preflight -e preflight_allow_alive=true`) |
| Node has no network / wrong IP | console screenshot: include `platforms/<p>/tasks/console.yml` with `-e console_node=<n>` (or the platform UI); fix the config, rebuild that node's ISO (`--tags isos -e '{"iso_nodes": ["<n>"]}'`), recreate the VM |
| Workers never Ready | rerun `--tags workers` (idempotent); MCS reachable at `api-int:22623`? |

## Changes after install
- **Memory/CPU/nested virtualization**: hot-add is off, so every change power-cycles the VM. Never apply it with
  `--tags vms` on a live cluster (it would reboot every changed node at once). Use `playbooks/rolling_update.yml`
  (`-e rolling_yes=true`, `-e '{"rolling_nodes": [...]}'`). Check the hypervisor capacity first.
- **Users**: `identity.htpasswd.enabled: true` + `clusters/<name>.htpasswd.yml` (roles: cluster-wide,
  `{role, namespace}`, `{role, namespace, kind: Role}`), then `playbooks/users.yml`. The user list and passwords are
  the user's to choose: never invent them. `htpasswd_remove_kubeadmin: true` is irreversible: confirm first.
- **Compact 3-node cluster** = no workers in the config: masters become schedulable and carry ingress. If the HAProxy
  in use predates the config (ingress → workers), run `playbooks/infra.yml --tags lb` first.
- **Extra NICs** come up disabled. Configure them with an NMState NNCP (`maxUnavailable: "100%"` when it only adds addresses).
- **Adding a worker**: add it to the config, rerun `infra` (DNS + ingress backends), `isos -e '{"iso_nodes": ["<n>"]}'`,
  `vms`, `workers`. `worker.ign` only points at the MCS, so there's no age limit.

Finish by running the `ocp-upi-status` skill and reporting the result in plain words.
