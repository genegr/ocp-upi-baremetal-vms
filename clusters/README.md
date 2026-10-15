# Cluster configs

One file per cluster, named after `cluster.name`: `clusters/<name>.yaml` (start from `../cluster.example.yaml`).
Select it with `-e cluster=<name>` on every playbook:

```bash
docker compose run --rm toolbox ansible-playbook playbooks/status.yml -e cluster=ocp4-a
```

Also here, all gitignored and kept on the jump box (never synced from a workstation):
- `vault.yml`: Ansible Vault with the secrets shared by all clusters (vSphere credentials, pull secret). See
  `vault.yml.example`.
- `<name>.htpasswd.yml`: optional HTPasswd user list for a cluster. See `htpasswd.example.yml`.

Each cluster's generated state is kept apart in `../.state/clusters/<name>/`: the pinned release, the OpenTofu state,
the install dir with kubeconfig and kubeadmin password, and the ISOs. Clusters can't overwrite each other's state.
Each needs its own platform folder or ISO directory, VIPs and node addresses.
