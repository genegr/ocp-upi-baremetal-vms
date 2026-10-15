# OpenShift UPI "bare metal" on virtual machines

Install OpenShift with the **user-provisioned bare-metal flow** (`platform: none`) on VMs, treating them exactly like
physical servers. This suits labs, demos and anywhere you want the bare-metal install experience without the hardware:

- **OpenTofu** creates the VMs on a virtualization platform. **vSphere** is implemented. The platform layer is
  pluggable (`platforms/`), so others such as Nutanix or Proxmox can be added without touching the rest.
- Every node boots its own **self-installing RHCOS ISO**. The ISO carries the node's **static IP** (NetworkManager
  keyfile, matched by MAC) and its **ignition**, and writes RHCOS to disk. There is no PXE, DHCP or HTTP server.
- From there on it is plain bare metal: bootstrap, CSR approval, install complete.
- **Ansible** drives everything: tools, DNS records and HAProxy on a Docker host, ignition, ISOs, OpenTofu, the
  install phases and users. It uses real modules, not shell scripts.
- **Everything runs in one pinned toolbox container** on a Docker host (the "jump box"). Nothing else needs to be
  installed there.
- Optional per cluster: compact 3-node topology, extra NICs, nested virtualization for OpenShift Virtualization,
  HTPasswd users with roles.

```
 jump box (Docker only)
 └─ toolbox container ── Ansible ─┬─ OpenTofu ──────────► hypervisor: bootstrap, master0-2, worker0-N
                                  │                         each boots <node>.iso: static IP + ignition → disk
                                  ├─ SSH ──────────────► Docker host: CoreDNS zone + HAProxy (api, api-int, *.apps)
                                  └─ Kubernetes API ───► bootstrap wait, CSR approval, install complete, users
```

## Requirements

- A Linux host with Docker (Compose v2, buildx) that reaches the platform's API, the Docker host for DNS/HAProxy,
  the machine network and the internet: mirror.openshift.com, quay.io, GitHub, PyPI, Ansible Galaxy, the Fedora
  mirrors.
- A Docker host for DNS and HAProxy that owns the DNS server address and both VIPs (`api_vip`, `ingress_vip`) as local
  addresses. It can be the same host.
- A Red Hat [pull secret](https://console.redhat.com/openshift/install/pull-secret) and platform credentials.

The toolbox image (`docker/`) pins everything:
- Fedora 44 with coreos-installer and Python;
- OpenTofu;
- ansible-core and the collections in `docker/requirements.yml`;
- the Python libraries in `docker/requirements.txt`.

`openshift-install` and `oc` are per release, so the `tools` phase downloads them, checksum-verified, into
`.state/bin/<release>/`.

## Set up the jump box

```bash
git clone <this repo> ocp-upi && cd ocp-upi
docker buildx bake -f compose.yaml --allow=network.host toolbox      # host networking: see compose.yaml

mkdir -p .state/home && chmod 700 .state
docker compose run --rm toolbox python3 -c "import secrets; print(secrets.token_urlsafe(32))" > .state/vault-pass
chmod 600 .state/vault-pass

cp clusters/vault.yml.example clusters/vault.yml            # vSphere user/password, pull secret
docker compose run --rm toolbox ansible-vault encrypt clusters/vault.yml --vault-password-file .state/vault-pass

cp cluster.example.yaml clusters/<name>.yaml                 # the cluster, documented inline
```

Every command below runs as `docker compose run --rm -e ANSIBLE_VAULT_PASSWORD_FILE=/work/.state/vault-pass toolbox
<command>`. To keep it short, set an alias:

```bash
alias tb='docker compose run --rm -e ANSIBLE_VAULT_PASSWORD_FILE=/work/.state/vault-pass toolbox'
```

The `tools` phase creates the project SSH key `.state/ssh/id_ed25519`. Add its public key
(`.state/ssh/id_ed25519.pub`) to the DNS/HAProxy host's `~/.ssh/authorized_keys` once. It's also authorized for
`core@` on every node it installs.

## Configure

`clusters/<name>.yaml` holds everything about a cluster. The `cluster_model` filter validates it and derives the rest:
- node FQDNs and hostnames;
- one MAC per NIC;
- the HAProxy backends (bootstrap as backup plus masters for the API and MCS; workers, or masters, for ingress);
- the DNS records.

Several clusters can live side by side. Each has its own config and its own `.state/clusters/<name>/`: pinned
release, OpenTofu state, install dir, ISOs.

**DNS modes** (`infra.dns.mode`):
- `managed`: a dedicated CoreDNS container for the cluster zone.
- `shared`: add the cluster zone to an existing CoreDNS. It writes the zone file and a marked Corefile block, and
  restarts that container only when something changed.

## Deploy

```bash
tb ansible-playbook playbooks/deploy.yml -e cluster=<name>
```

Phases, as tags. Every phase is idempotent, so re-running one is the normal way to converge or resume.

| Tag | Does |
|-----|------|
| `tools` | pins the release, fetches openshift-install/oc and the RHCOS ISO, creates the SSH key |
| `infra` | DNS zone + HAProxy on the Docker host (shared CoreDNS: run `playbooks/infra.yml --check --diff` first) |
| `preflight` | DNS answers, LB ports, node IPs free, platform objects, secrets |
| `ignition` | install-config + manifests + ignition (bootstrap certs valid 24 h; `-e ignition_force=true` starts over) |
| `isos` | one ISO per node (rebuilt only when its inputs change) |
| `vms` | OpenTofu: upload ISOs, create and power on the VMs, boot order |
| `bootstrap` | waits for bootstrap complete and the bootstrap etcd member's removal, then deletes the bootstrap VM |
| `workers` | approves the workers' CSRs until they're Ready (only CSRs of the listed nodes) |
| `complete` | waits for install complete, ejects the ISOs, sets up HTPasswd users if enabled |

`--check` turns every phase into a dry run; OpenTofu then only plans. A full install takes about 35 minutes from VM power-on.

Other playbooks:

| Playbook | Does |
|----------|------|
| `playbooks/status.yml` | read-only health report (DNS, HAProxy pools, VMs, cluster); fails on any FAIL |
| `playbooks/infra.yml` | DNS + HAProxy only (`--tags dns`/`lb`, `-e infra_state=absent`) |
| `playbooks/users.yml` | HTPasswd users and roles from `clusters/<name>.htpasswd.yml` |
| `playbooks/rolling_update.yml` | memory/CPU/nested-virt changes, one node at a time (drain, apply, reboot, uncordon) |
| `playbooks/destroy.yml` | VMs + ISOs (asks for the cluster name; `-e destroy_infra=true` also removes DNS/HAProxy) |
| `playbooks/tools/oc.yml` | `-e "oc_args='get nodes'"`: oc of the cluster's release against the cluster |

## Access the cluster

| What | Where (on the jump box) |
|------|-------------------------|
| Admin kubeconfig | `.state/clusters/<name>/install/auth/kubeconfig` |
| `kubeadmin` password | `.state/clusters/<name>/install/auth/kubeadmin-password` |
| Web console | `https://console-openshift-console.apps.<cluster>.<base_domain>` |
| API | `https://api.<cluster>.<base_domain>:6443` |

```bash
tb ansible-playbook playbooks/tools/oc.yml -e cluster=<name> -e "oc_args='get co'"
tb .state/bin/<release>/oc --kubeconfig .state/clusters/<name>/install/auth/kubeconfig get nodes
```

Keep the admin kubeconfig safe: it's a cluster-admin credential that can't be revoked short of rotating the
cluster's CA.

## Users (HTPasswd identity provider)

Set `identity.htpasswd.enabled: true` and write `clusters/<name>.htpasswd.yml`, starting from
`clusters/htpasswd.example.yml`. The `complete` phase then:
- writes the bcrypt htpasswd secret (hashes are reused while the password matches, so a rerun changes nothing);
- adds the `htpasswd` identity provider and leaves any other identity providers alone;
- binds roles: cluster-wide, per namespace, or a namespace's own Role. Every role and namespace is checked before
  anything is written, and bindings removed from the file are removed from the cluster;
- prunes users dropped from the list;
- logs each user in to verify.

On an existing cluster, run `playbooks/users.yml`. Encrypt passwords with `ansible-vault encrypt_string`.

## Compact 3-node cluster

Leave out the workers. `masters_schedulable: auto` (the default) makes the masters schedulable, HAProxy's ingress
pool points at them, and the `workers` phase has nothing to do. Size the masters for both jobs, e.g. 16 vCPU / 32 GB.
The bootstrap VM is still needed for the first 20–30 minutes.

## Optional: nested virtualization (OpenShift Virtualization)

Only needed if the cluster will run VMs with OpenShift Virtualization. Set `roles.worker.nested_virt: true` (or per
node; on masters only in a compact cluster) and give those nodes 64 GB or more. Changing it later power-cycles the
VM, so apply it with `playbooks/rolling_update.yml`.

## Resizing nodes on a running cluster

Change `memory_mb`/`cpus` (per role or per node), then run `tb ansible-playbook playbooks/rolling_update.yml -e
cluster=<name>`. Hot-add is off, so every change power-cycles the VM. Each node in turn:
1. Skipped if a targeted OpenTofu plan shows nothing to change.
2. Drained (`kubernetes.core.k8s_drain`).
3. The change is applied to that VM only.
4. Ready again with a new `bootID`, then uncordoned.
5. Every cluster operator settled for a minute before the next node.

`-e rolling_yes=true` skips the per-node prompt; `-e '{"rolling_nodes": ["worker0"]}'` limits it to some nodes.

## Optional: extra NICs

`roles.worker.extra_networks` (or per node) adds NICs on more platform networks, e.g. storage. They come up with
IPv4/IPv6 disabled; configure them afterwards (e.g. a kubernetes-nmstate `NodeNetworkConfigurationPolicy`).

## How a node installs

1. The VM boots with a blank disk. The boot order (disk, then CD-ROM) skips the empty disk, so the ISO boots.
2. The live system applies the keyfile (static IP, matched by MAC) and runs `coreos-installer` to `install_disk` with
   the embedded ignition, copying the network config to the installed system. Then it reboots.
3. The disk now boots first, so the ISO never reinstalls. The `complete` phase ejects the media.

Masters and workers embed the installer's small ignition, which pulls the rest from the machine-config server at
`api-int:22623` through HAProxy. Each node's ignition also writes `/etc/hostname`, so no PTR records are needed.

## Authoring from another machine

Edit on your workstation, then copy the tree to the jump box with `tools/sync.sh` (settings in `.sync.env`). It never
copies `.state/` or `clusters/vault.yml`. tar doesn't delete, so remove deleted files on the jump box too.

## Layout

```
compose.yaml, docker/          toolbox image (everything pinned), host networking
ansible.cfg, inventory/        localhost; the infra host is added at run time from the cluster config
playbooks/                     deploy, infra, users, rolling_update, status, destroy, tools/oc
roles/                         cluster_model tools infra preflight ignition isos vms bootstrap csr install_complete
                               htpasswd rolling_update status
plugins/filter/                cluster_model (validate + derive), htpasswd (bcrypt), k8s (conditions)
plugins/modules/               ocp_csr_approve (no upstream module approves CSRs)
platforms/<name>/              OpenTofu module + Ansible task files per hypervisor (contract: platforms/README.md)
clusters/                      configs, user lists, vault (gitignored; examples tracked)
tests/                         filter unit tests (python3 tests/filters_test.py, in the toolbox)
tools/sync.sh                  optional: workstation → jump box
.state/                        runtime state on the jump box (gitignored)
```
