# CLAUDE.md

OpenShift UPI "bare metal" on virtual machines: OpenTofu creates VMs on a virtualization platform, each VM boots a
self-installing RHCOS ISO carrying its static IP and ignition, and from there it's the plain bare-metal flow. Ansible
drives every phase, from a pinned toolbox container on a Docker jump box. Human docs: `README.md`.

## Hard rules
- **Everything runs in the toolbox container** (`docker compose run --rm toolbox …`) on the jump box. Never install
  tools on the jump box or in its home directory. An authoring workstation only edits files and syncs them over
  (`tools/sync.sh`). A new tool means a new pinned entry in `docker/Dockerfile` or `docker/requirements.{txt,yml}`.
- **No shell scripts.** Use Ansible modules or OpenTofu resources. Sanctioned `command` uses, each guarded with
  `creates:` or `changed_when: false`:
  - `openshift-install` (ignition, stream metadata) and `coreos-installer iso customize`: binaries with no module;
  - `ping` in preflight, because no module sends ICMP;
  - `playbooks/tools/oc.yml`, a human convenience;
  - `govc` in the migration roles, for what no module manages: `sso.user.ls/create` (vCenter SSO users; the new
    user's password can only go in as `-p`, so that task is `no_log` and runs only in the toolbox), `find -i` (host
    IDs), `host.esxcli system ssh key list/add` (MTV's restricted key for root, through vCenter).

  `plugins/modules/ocp_csr_approve.py` exists because no module approves CSRs (the approval is a subresource).
  `plugins/modules/purefa_realm_admin.py` exists because `everpure.flasharray` 1.45.1 can't make a role-less,
  policy-only user. Retire it once a pinned release has `purefa_user` `access_policy` (upstream PR
  Everpure-Ansible/FlashArray-Collection#1087, released in 1.46.0). `purefa_resource_access.py` gives array-level
  hosts/host groups access to realms (`purehost access create --realm`): the collection only does it as a side effect
  of `purefa_host` `move`, which needs a host without connections. `purefa_volume_info.py` reads a volume's serial
  without the volumes scan broken on Purity 6.12 (#1103). The only shell file is the optional `tools/sync.sh`.
- **Secrets never on a command line.** OpenTofu gets credentials as `TF_VAR_*` through `environment:`, because
  `community.general.terraform` echoes `-var` on failure. Secrets live in `clusters/vault.yml` (Ansible Vault). Tasks
  that carry them use `no_log: true`.
- **Pin every version**: Dockerfile ARGs, `docker/requirements.{txt,yml}`, `platforms/*/versions.tf`. Only use
  releases at least two weeks old.
- **Idempotent**: a second run of any playbook or phase must report `changed=0`. ISO rebuilds are decided by an
  inputs digest, not by Ansible's `changed` (which also flips on file modes).
- **Platform-neutral core**: roles, filters and playbooks are hypervisor-neutral. Hypervisor code lives only in
  `platforms/<name>/` (OpenTofu module, `tasks/*.yml`, `vm_address.j2`) and the `PLATFORMS` table in
  `plugins/filter/cluster_model.py`. The contract is in `platforms/README.md`. Implemented: `vsphere`. Don't add code for
  a platform that can't be tested end to end.
- **Opt-in features** (nested virtualization, extra NICs, users) default to off. The default story is a regular cluster.
- **Published on GitHub: no environment values in tracked files.** Real addresses, hostnames, vCenter names,
  datastores and credentials live only in gitignored files: `clusters/*` except the examples, `.state/` and
  `.sync.env`. Examples use RFC 5737 addresses and example.com. Before finishing, grep the tracked files
  (`git add -A --dry-run`).
- **Never commit or sync** `.state/` or `clusters/vault.yml`.

## Working rules
- Every playbook takes `-e cluster=<name>` (`clusters/<name>.yaml`). With several configs, always pass it
  explicitly: a phase run against the wrong cluster is the most damaging mistake possible here.
- The cluster model `c` (from the `cluster_model` role/filter) is the single source of truth. Derived values (FQDNs,
  MACs, LB backends, DNS records) are computed in the filter only. OpenTofu reads `.state/clusters/<name>/cluster.json`.
- Shared CoreDNS (`infra.dns.mode: shared`) serves other environments: run `playbooks/infra.yml --check --diff` and get
  the user's explicit go-ahead before a real run. `destroy.yml` also needs explicit consent every time.
- Long runs (a full deploy, rolling updates, `storage.yml --tags nics|node`) go in the background with output to
  `.state/<name>.log`. Then `grep -E "fatal|PLAY RECAP" -A8` the log.
- `storage.yml --tags teardown` and `destroy.yml` both need the user's explicit consent every time.
- Storage (`playbooks/storage.yml`) touches a shared FlashArray. Only the cluster's own realm, pod, policy and user
  are created. Never point a cluster at another cluster's realm. The admin token stays in the vault; the realm user's
  token lives only in `.state/clusters/<name>/` and in the `px-pure-secret` Secret.

## Checks before declaring a change done (in the toolbox)
- `python3 tests/filters_test.py`
- `ansible-playbook <each playbook> --syntax-check -e cluster=<name>`
- `tofu -chdir=platforms/<name> fmt -check` (and `validate` after an init)
- For changes to a phase: run it twice against a live cluster (`--check` first). The second run must be `changed=0`.

## Layout
```
compose.yaml, docker/          toolbox image (Fedora 44: coreos-installer, OpenTofu, govc, Helm, ansible-core + collections,
                               everpure.flasharray/flashblade)
ansible.cfg, inventory/        localhost; infra host added at run time (cluster_model)
playbooks/                     deploy (tags = phases), infra, users, rolling_update, storage (tags = phases; teardown
                               only with --tags teardown), addons, migration (tags array, vsphere, mtv), backup, status, destroy,
                               tools/oc
roles/                         one per phase; vms is reused by bootstrap, install_complete, rolling_update, destroy;
                               olm_operator installs one pinned OLM operator (Manual approval)
plugins/filter|modules         cluster_model, htpasswd, k8s; ocp_csr_approve, purefa_realm_admin, purefa_resource_access,
                               purefa_volume_info
platforms/<name>/              OpenTofu module + tasks/{credentials,preflight,post_apply,vm_state,console}.yml
clusters/                      <name>.yaml, <name>.htpasswd.yml, vault.yml (gitignored), examples
.state/                        vault-pass, ssh/, cache/, bin/<release>/, clusters/<name>/ (state, install, iso, nodes)
```

## Known gotchas (verified)
- **Docker DNS**: lab DNS servers can sit inside docker0's 172.17.0.0/16, so the image builds and runs with host
  networking (`docker buildx bake -f compose.yaml --allow=network.host toolbox`).
- **OpenShift's HTPasswd provider only accepts bcrypt labelled `$2y$`** ("Unrecognized hash type" for `$2b$`). The
  htpasswd filter relabels.
- **Bootstrap ignition certs expire after 24 h**: the isos phase refuses a bootstrap ISO from ignition older than 20 h.
  `master.ign`/`worker.ign` only point at the MCS, so workers added later reuse the same install dir.
- **Rebuilt nodes reuse IPs with new host keys**: ssh to nodes with `UserKnownHostsFile=/dev/null`, as `ansible.cfg` does.
- **Workers reboot once (OS update) before their CSRs appear**, about 20 minutes after power-on. That's not a failure.
- An ingress pool with one node DOWN is normal: 2 routers on 3 nodes.
- **RHCOS ships the same `/etc/nvme/hostnqn` and `hostid` on every node.** The array tells hosts apart by NQN, so
  the `storage_node` MachineConfig derives them once from the SMBIOS UUID.
- **systemd units in MachineConfigs: order them late (`Before=kubelet.service`) and never against early-boot units.**
  `Before=multipathd.service` (multipathd runs before `local-fs-pre.target`) plus the default `After=sysinit.target`
  is an ordering cycle. The job systemd dropped left master2 with no IP ("Failed to find a suitable node IP"), and the
  MCO stalled with that node down. Recovery without SSH, from the toolbox with pyvmomi: give the VM a temporary
  firmware boot delay, reset it while sending arrow keys to stop GRUB, edit the entry to add
  `systemd.mask=<unit>`, boot, fix the MachineConfig, and restore the boot delay. If the node then goes Degraded
  with "content mismatch" against its old rendered config: on 4.22 neither `/run/machine-config-daemon-force` nor
  patching the node annotations helps. The MCD validates against its on-disk record
  (`/etc/machine-config-daemon/currentconfig`). Write that file's old content back, byte for byte from the old
  rendered MachineConfig. The MCD then validates and performs a normal update to the new render. Do that only after
  a reboot *without* the `systemd.mask` karg. Otherwise the MCD's enable step fails on the masked unit and leaves every
  OpenShift unit disabled (kubelet, ovs-configuration, nodeip-configuration, ...). If that happens, re-enable the
  units the rendered config marks `enabled` and reboot.
- **Draining a master needs `force`** (unmanaged static-pod guard Pods). A PDB can block an eviction for a while
  right after the previous node's drain, so the drain retries.
- **NICs hot-plug on vSphere**: no reboot. `rolling_update` waits for a new bootID only if the VM's boot time changed.
- **NMState's webhook stamps a timestamp on every NNCP update**: apply only when the spec differs, or the phase is
  never `changed=0`.
- **FlashArray policy membership** can't be changed with `PATCH admins` (`management_access_policies` is refused).
  Use the policy-members endpoints, adding before removing. A role user is a member of the built-in policy named
  after the role; a role patch on a policy-only user fails with "User is not a role user".
- `module_defaults` group for the Pure modules: `group/everpure.flasharray.everpure.flasharray`.
- **Portworx Enterprise cloud drives over NVMe-oF need native NVMe multipath off**: PX won't start without
  `nvme_core.multipath=N` on the kernel command line ("FlashArray software dependencies missing"). PX-CSI alone
  works with it on. The filter derives `c.storage.kernel_args` and the `storage_node` MachineConfig sets it.
- **NVMe/TCP fails on an array that also serves NVMe-FC** (PX 3.7.1.1, cloud drives): the array returns the same
  subsystem NQN on its FC ports, with no portal, and PX runs `nvme connect -t tcp -a ""` against them, failing every
  attach ("failed to execute nvme cmd"). After one failed provision PX labels the node
  `portworx.io/provision-storage-node-handled` and every restart comes up storage-less ("Storage less node detected"
  on kvdb). Use iSCSI there (`PURE_ISCSI_ALLOWED_CIDRS`, derived from `storage.networks`, keeps PX off unreachable
  portals), or an array whose FC ports serve SCSI only. Before a retry, uninstall the StorageCluster
  (UninstallAndDelete) and remove the NVMe hosts PX left in the realm.
- **PX-CSI integration in Portworx Enterprise is one-way** (`portworx.io/pure-csi-integration`): once on, it can't be
  disabled. It needs PX Enterprise ≥ 3.7.0 and serves the FADA volumes. The license is activated through `pxctl` in a
  Portworx pod (`k8s_exec`), only while the SKU reads Trial.
- **A rebuild must not inherit OpenTofu flags**: `tofu.tfvars.json` keeps `bootstrap_enabled`/`attach_iso` from the
  old cluster's finished install (both false). The `vms` role ignores them when the state has no managed resources.
  Before that fix, a rebuild booted VMs with no ISO and no bootstrap, which stopped in the EFI Boot Manager.
- **vCenter renames break OpenTofu destroys**: the state stores datastore names, and every plan, even a targeted one,
  refreshes `vsphere_file` with the old name. Recovery that worked: delete the VMs by the UUIDs recorded in the
  state (`vmware_guest`, after checking name and folder), delete the ISOs with `vsphere_file` under the new name, then
  archive the state. Update the cluster config to the new datastore names first.
- **`purefa_info` `volumes` (also `purefa_volume`, `purefa_vg`) fails on Purity//FA 6.12** when any volume on the
  array has no priority adjustment (`AttributeError`; Everpure-Ansible/FlashArray-Collection#1103). The fix, #1105, is
  on master after 1.46.0. Until a pinned release has it, `storage_smoke` checks the realm pod's provisioned space
  (`pods` subset), not the volume list. `purefa_volume` can create a volume but fails on an existing one, so the
  migration datastore volume is created only when it isn't connected to its host group yet (`hgroups` subset `vols`).
- **`purefa_user` (1.45.1) returns a new API token as a plain string in `user_info`**, not `user_info.user_api` as
  documented; given a `password` for an existing user without `old_password` it exits without doing anything. A token
  is shown only at creation: keep it in the same task run.
- **MTV copy offload (XCOPY) on FlashArray**: SCSI only (iSCSI/FC, no NVMe datastores), source datastore + FADA
  volumes + Portworx cloud drives on the same array, realm and pod. A realm volume connects to an array-level host
  group only when the **group itself** has realm access ("Connection outside of the realm without resource access is
  not allowed"); host access alone isn't enough. Some hosts of a group may not see a new high LUN (LUN 251: two of four
  hosts didn't, even after rescans): format through a host that sees it and pin the clones (`clone_hosts`, which
  become `dedicatedMigrationHosts`) to such hosts.
- **vCenter SSO**: the system domain isn't always `vsphere.local` (`govc sso.idp.ls`), and the default password policy
  caps passwords at 20 characters ("password policy is not satisfied").
- **MTV with the Portworx hook**: the hook adds the target namespace's ServiceAccount to the `fa-pxd-converter`
  ClusterRoleBinding and SCC at run time; reapplying them with a fixed subject list removes it (create the binding only
  when missing, never set the SCC's `users`). On the pod network set `preserveStaticIPs: false`, and give the guest a
  netplan that matches the NIC driver (static on `vmxnet3`, DHCP on `virtio_net`).
- **`oc` short names collide with ACM**: `subscription` resolves to ACM's `apps.open-cluster-management.io`
  Subscription; use `subscriptions.operators.coreos.com` for OLM.
- **Portworx Backup 3.x needs the `pxc-credentials` Secret before `helm install`** (six keys; the pre-install hook
  fails with "secret pxc-credentials not found"). The role generates them once into `.state/clusters/<name>/px-backup/`.
  `mongodb-master-encryption-key` (96 characters) can never change: losing it loses the backup metadata. A failed
  first install leaves a `failed` Helm release and a failed `pre-install-check` Job: uninstall and delete them first.

## Skills
`.claude/skills/`: `ocp-upi-deploy`, `ocp-upi-infra`, `ocp-upi-status`, `ocp-upi-storage`, `ocp-upi-migration`,
`ocp-upi-teardown`.
