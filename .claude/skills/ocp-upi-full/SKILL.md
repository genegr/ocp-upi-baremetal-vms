---
name: ocp-upi-full
description: End-to-end runbook for a cluster of this project, from nothing to fully configured - the install phases, DNS/HAProxy, users, PX-CSI storage on FlashArray, day-2 operators, Portworx Backup and migration - in order, running only what the cluster config enables, with every gate and timing and the skill that owns each step. The entry point for short or spoken (dictated) requests. Use whenever the user wants to deploy, rebuild, redeploy, resume or continue "the cluster", "OpenShift", "everything" or "from scratch", or asks where a deployment stands or what comes next. A project built on top of the cluster (a sibling checkout with its own CLAUDE.md, e.g. RHOSO in ../rhoso) has its own runbook, which calls this one.
when_to_use: Spoken prompts such as "deploy OCP", "deploy the cluster", "rebuild OpenShift", "continue the install", "where are we". Dictation may write OCP as "O C P", OpenShift as "open shift", Portworx as "port works", FlashArray as "flash array".
---

# A cluster, start to finish

Read `CLAUDE.md` first. Each step below names the skill that owns it; follow that skill.

## 1. Pin down the target before anything runs
- **Cluster**: the only `clusters/*.yaml` that isn't an example. If there are several, ask which one; never guess.
  Say it back in the first line of the reply ("Cluster X: ...").
- **Rebuild or resume?** A running cluster with that name is never torn down on the strength of "deploy" or
  "rebuild". Teardown is `ocp-upi-teardown`, with its own explicit yes.
- **Where are we**: run `ocp-upi-status` (read-only) and check the newest `.state/*.log`. The first phase not done
  is the starting point.
- Then give the plan in one line: the steps, roughly how long, and which steps will stop for a yes.

## 2. The steps
"Gate" = show the `--check --diff` result and stop until the user says yes in a reply. A voice prompt that asked
for the deployment is not that yes. Rows marked "if" run only when the config enables them.

| # | Step | How | Time | Gate |
|---|------|-----|------|------|
| 1 | Toolbox image | `docker buildx bake -f compose.yaml --allow=network.host toolbox` on the jump box (CLAUDE.md "Docker DNS"); only when `docker/` changed or there's no image | 5–10 min | — |
| 2 | DNS + HAProxy | `ocp-upi-infra`: `infra.yml` (also `deploy.yml --tags infra`) | 1 min | shared CoreDNS / AD DNS |
| 3 | Install | `ocp-upi-deploy`: `deploy.yml` (tools, preflight, ignition, isos, vms, bootstrap, workers, complete) | ~45 min | — |
| 4 | Users (if `identity.htpasswd.enabled`) | `ocp-upi-deploy` "Changes after install": `users.yml` | 2 min | removing kubeadmin |
| 5 | Storage (if `storage.portworx.enabled`) | `ocp-upi-storage`: `storage.yml --tags nics`, `network`, `node`, `flasharray`, `portworx`, `smoke` | 1 h (`node` reboots each node) | FlashArray |
| 6 | Operators (if `addons:`) | `addons.yml` (each pinned CSV, Manual approval) | 10–20 min | — |
| 7 | Backup (if `backup:`) | `backup.yml`; see CLAUDE.md "Portworx Backup" | 10 min | — |
| 8 | Migration (if asked) | `ocp-upi-migration`: `migration.yml --tags array,vsphere,mtv` | 20 min | FlashArray, vCenter |

The ignition from step 3 is valid for 24 h. Never resume a bootstrap from older ignition: the isos phase refuses
it, and then ignition, isos and vms run again.

## 3. How to run it
- Sync first (`bash tools/sync.sh`), then every command through the toolbox on the jump box, always with
  `-e cluster=<name>`.
- Long steps (3, storage `nics`/`node`) run in the background, with output to `.state/<name>-<step>.log`. Then read
  the result with `grep -E "fatal|PLAY RECAP" -A8`.
- After each step, give a one-line status: done, the `changed`/`failed` counts, the next step. A phase rerun must be
  `changed=0`.
- A failure: stop, read the log and the owning skill's troubleshooting table, fix the cause and rerun only that phase.
- When done, run `ocp-upi-status` and report in plain words. If a project built on the cluster is next (for
  example `../rhoso`), go back to its runbook (`../rhoso/.claude/skills/rhoso-lab/SKILL.md`) rather than ending.
