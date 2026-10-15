---
name: ocp-upi-infra
description: Deploy, update, verify or roll back the DNS records and HAProxy load balancer an OpenShift UPI cluster of this project depends on, as containers on the infra Docker host (role infra, playbooks/infra.yml, run from the toolbox container). Use whenever the user wants to set up or change the cluster DNS zone or load balancer, add records or nodes to HAProxy, switch between managed and shared CoreDNS, cut over from a hand-made HAProxy, debug api/api-int/*.apps resolution or LB ports 6443/22623/80/443, or remove a cluster's infra.
---

# Cluster DNS + HAProxy on the infra Docker host

Run from the toolbox on the jump box (see the `ocp-upi-deploy` skill for the prefix):

```bash
ansible-playbook playbooks/infra.yml -e cluster=<name> --check --diff   # ALWAYS first; show the user the diff
ansible-playbook playbooks/infra.yml -e cluster=<name>                  # deploy; verified, rolled back on failure
ansible-playbook playbooks/infra.yml -e cluster=<name> --tags dns|lb
ansible-playbook playbooks/infra.yml -e cluster=<name> -e infra_state=absent
```

The toolbox reaches `infra.user@infra.host` with the project key `.state/ssh/id_ed25519` (authorized once on that
host). That user must be able to run `docker`.

## What it manages
Rendered from the cluster model. Never hand-edit the generated files on the host: the next run overwrites them.
- **`managed` DNS**: container `<cluster>-coredns` in `<infra.base_dir>/<cluster>/coredns/`, authoritative for the
  cluster zone, forwarding the rest, bound to `infra.dns.listen`.
- **`shared` DNS**: adds `db.<zone>` to `infra.dns.shared.config_dir` and a Corefile block between
  `# BEGIN/END ocp-upi <zone>` markers. Optionally comments out the cluster's old records in `legacy_zone_file`.
  Restarts `infra.dns.shared.container` (`docker_container restart`) only when something changed.
- **HAProxy**: compose project `<cluster>-haproxy` in `<infra.base_dir>/<cluster>/haproxy/`, publishing 6443, 22623,
  80→8080, 443→4443 and stats on both VIPs. The config is validated with `haproxy -c` before it's written.
  `infra.haproxy.replace_compose_dir` stops a hand-made HAProxy once, and brings it back if the new one fails.
- **Verification**: from the toolbox with the `community.general.dig` lookup (every record, plus
  `infra.dns.verify_names`), `wait_for` on every VIP port, and the stats page.

## Rules
- **Shared CoreDNS = other people's DNS.** A real run restarts it (≈1 s blip for every zone) and may edit their
  parent zone file. Show the `--check --diff` output and get the user's explicit go-ahead in this conversation first.
  The same goes for a HAProxy cutover.
- Before a shared-mode change, record `lookup('community.general.dig', ...)` answers for a few other zones and
  compare afterwards. The playbook's `verify_names` does this for the names listed.
- Backups: Ansible writes `<file>.<timestamp>~` next to each changed file on the Docker host.
- `--check` can't show container restarts or verification (they're skipped). Say so when reporting a dry run.
