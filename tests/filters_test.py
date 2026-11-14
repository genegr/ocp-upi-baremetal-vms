"""Unit tests for the filter plugins. Run in the toolbox:  python3 tests/filters_test.py"""
import copy
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins", "filter"))

from ansible.errors import AnsibleFilterError  # noqa: E402
from cluster_model import cluster_model, haproxy_pools  # noqa: E402
from htpasswd import htpasswd_bindings, htpasswd_render  # noqa: E402
from k8s import (k8s_condition, k8s_node_readiness, k8s_pending_csrs, k8s_unhealthy_operators,  # noqa: E402
                 k8s_csinodes_with_driver, k8s_names_with_condition, kv_lines, nmstate_storage_interfaces,
                 nns_ifaces_by_mac)

EXAMPLE = os.path.join(HERE, "..", "cluster.example.yaml")
failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def rejects(name, raw, fragment):
    try:
        cluster_model(raw, EXAMPLE)
    except AnsibleFilterError as exc:
        check(f"{name} (rejected: {fragment})", fragment in str(exc))
        return
    check(f"{name} (should have been rejected)", False)


base = yaml.safe_load(open(EXAMPLE))

# --- cluster_model ----------------------------------------------------------------------------------------------
c = cluster_model(base, EXAMPLE)
check("example: standard topology, masters not schedulable",
      c["cluster"]["topology"] == "standard" and c["cluster"]["masters_schedulable"] is False)
check("example: ingress on workers", [b["name"] for b in c["lb"]["ingress"]] == ["worker0", "worker1", "worker2"])
check("example: bootstrap is the API backup", [b["backup"] for b in c["lb"]["api"]] == [True, False, False, False])
check("example: nested virtualization off by default", not any(n["nested_virt"] for n in c["nodes"]))
check("example: users_file next to the config", c["identity"]["htpasswd"]["users_file"].endswith("cluster.example.htpasswd.yml"))

compact = copy.deepcopy(base)
compact["nodes"] = [n for n in compact["nodes"] if n["role"] != "worker"]
cc = cluster_model(compact, EXAMPLE)
check("compact: masters schedulable and carry ingress",
      cc["cluster"]["topology"] == "compact" and cc["cluster"]["masters_schedulable"] is True
      and [b["name"] for b in cc["lb"]["ingress"]] == ["master0", "master1", "master2"])

compact_nested = copy.deepcopy(compact)
compact_nested["roles"]["master"]["nested_virt"] = True
cn = cluster_model(compact_nested, EXAMPLE)
check("compact: nested virt allowed on masters, never on bootstrap",
      [n["nested_virt"] for n in cn["nodes"]] == [False, True, True, True])

extra = copy.deepcopy(base)
extra["roles"]["worker"]["extra_networks"] = ["Storage A", "Storage B"]
ce = cluster_model(extra, EXAMPLE)
w0 = next(n for n in ce["nodes"] if n["name"] == "worker0")
check("extra NICs: 4th octet + 1, + 2", [n["mac"] for n in w0["nics"]] == ["00:50:56:01:00:54", "00:50:56:02:00:54", "00:50:56:03:00:54"])

bad = copy.deepcopy(compact); bad["cluster"]["masters_schedulable"] = False
rejects("compact with masters_schedulable false", bad, "masters_schedulable must be auto or true")
bad = copy.deepcopy(base); bad["platform"] = "proxmox"
rejects("unknown platform", bad, "platform must be one of")
bad = copy.deepcopy(base); del bad["platform"]
rejects("missing platform", bad, "platform must be one of")
bad = copy.deepcopy(base); bad["nodes"][1]["mac"] = "52:54:00:01:00:51"
rejects("MAC outside the vSphere range", bad, "is not accepted by vsphere")
bad = copy.deepcopy(base); bad["nodes"][1]["mac"] = "01:50:56:01:00:51"
rejects("multicast MAC", bad, "must be a unicast")
bad = copy.deepcopy(base); bad["nodes"][2]["ip"] = bad["nodes"][1]["ip"]
rejects("duplicate IP", bad, "duplicate ip")
bad = copy.deepcopy(base); bad["network"]["api_vip"] = "198.51.100.3"
rejects("VIP outside the machine network", bad, "outside")

# --- storage ------------------------------------------------------------------------------------------------------
st = copy.deepcopy(compact)
st["roles"]["master"]["extra_networks"] = ["Storage A", "Storage B"]
st["storage"] = {
    "networks": [{"name": "nvme-a", "nic": 1, "cidr": "198.51.100.0/24", "host": "same"},
                 {"name": "nvme-b", "nic": 2, "cidr": "203.0.113.0/24", "host": "same", "mtu": 9000}],
    "flasharray": {"endpoint": "192.0.2.200", "quota": "10T"},
    "portworx": {"enabled": True},
}
cs = cluster_model(st, EXAMPLE)
m0 = next(n for n in cs["nodes"] if n["name"] == "master0")
check("storage: host: same mirrors the machine-net host number",
      [(s["name"], s["address"], s["prefix"], s["mtu"]) for s in m0["storage"]]
      == [("nvme-a", "198.51.100.51", 24, 1500), ("nvme-b", "203.0.113.51", 24, 9000)])
check("storage: bound to the extra NICs by MAC", [s["mac"] for s in m0["storage"]] == [m0["nics"][1]["mac"], m0["nics"][2]["mac"]])
check("storage: no addresses on the bootstrap", next(n for n in cs["nodes"] if n["role"] == "bootstrap")["storage"] == [])
fa = cs["storage"]["flasharray"]
check("storage: realm/policy/user defaults from the cluster name",
      (fa["realm"], fa["policy"], fa["user"]) == ("ocp4", "ocp4-storage", "ocp4-px"))
check("storage: realm mode, pod path realm::pod", fa["use_realm"] and fa["pod_path"] == "ocp4::px-csi")
arr = copy.deepcopy(st); arr["storage"]["flasharray"]["realm"] = False
fa_arr = cluster_model(arr, EXAMPLE)["storage"]["flasharray"]
check("storage: array mode (realm false): no realm, cluster pod, built-in storage_admin policy",
      (fa_arr["use_realm"], fa_arr["realm"], fa_arr["pod"], fa_arr["pod_path"], fa_arr["policy"], fa_arr["user"])
      == (False, "", "ocp4-px-csi", "ocp4-px-csi", "storage_admin", "ocp4-px"))
arr2 = copy.deepcopy(arr); arr2["storage"]["flasharray"]["pod"] = "lab-pod"
check("storage: array mode keeps an explicit pod name",
      cluster_model(arr2, EXAMPLE)["storage"]["flasharray"]["pod_path"] == "lab-pod")
bad = copy.deepcopy(st); bad["storage"]["flasharray"]["realm"] = True
rejects("storage: realm is a name, empty or false", bad, "must be a name, empty")
std = copy.deepcopy(st); std["nodes"] = base["nodes"]; std["roles"]["worker"]["extra_networks"] = ["Storage A", "Storage B"]
csd = cluster_model(std, EXAMPLE)
check("storage: standard topology puts storage IPs on workers only",
      [n["name"] for n in csd["nodes"] if n["storage"]] == ["worker0", "worker1", "worker2"])
check("storage: MachineConfig pools follow the workload nodes",
      (cs["storage"]["mc_roles"], csd["storage"]["mc_roles"]) == (["master"], ["worker"]))
check("storage: storage node list", cs["storage"]["nodes"] == ["master0", "master1", "master2"])

bad = copy.deepcopy(st); bad["storage"]["networks"][0]["nic"] = 3
rejects("storage: NIC index beyond the node's NICs", bad, "has no NIC 3")
bad = copy.deepcopy(st); bad["storage"]["networks"][0]["cidr"] = base["network"]["cidr"]
rejects("storage: address colliding with a node IP", bad, "collides")
bad = copy.deepcopy(st); bad["storage"]["networks"] = []
rejects("storage: NVMe/TCP without storage networks", bad, "needs storage.networks")
bad = copy.deepcopy(st); bad["storage"]["flasharray"]["endpoint"] = ""
rejects("storage: portworx without a FlashArray endpoint", bad, "needs storage.flasharray.endpoint")
check("storage: portworx mode defaults to csi", cs["storage"]["portworx"]["mode"] == "csi")
check("storage: admin token variable defaults to vault_flasharray_api_token",
      cs["storage"]["flasharray"]["token_var"] == "vault_flasharray_api_token")
bad = copy.deepcopy(st); bad["storage"]["flasharray"]["token_var"] = "api_token"
rejects("storage: token_var must name a vault variable", bad, "must name a vault variable")
ent = copy.deepcopy(std); ent["storage"]["portworx"]["mode"] = "enterprise"
ce = cluster_model(ent, EXAMPLE)
check("storage: enterprise defaults (3.7.1.1, 500/64 GiB drives, PX-CSI integration on)",
      (ce["storage"]["portworx"]["enterprise"]["image"].endswith(":3.7.1.1"),
       ce["storage"]["portworx"]["enterprise"]["cloud_drive_gib"], ce["storage"]["portworx"]["enterprise"]["metadata_drive_gib"],
       ce["storage"]["portworx"]["enterprise"]["csi_integration"], ce["storage"]["mc_roles"]) == (True, 500, 64, True, ["worker"]))
bad = copy.deepcopy(ent); bad["storage"]["portworx"]["mode"] = "hybrid"
rejects("storage: unknown portworx mode", bad, "mode must be csi or enterprise")
bad = copy.deepcopy(ent); bad["storage"]["portworx"]["enterprise"] = {"image": "portworx/oci-monitor:3.6.2"}
rejects("storage: PX-CSI integration needs PX 3.7", bad, "needs Portworx Enterprise 3.7.0")
bad = copy.deepcopy(ent); bad["storage"]["portworx"]["enterprise"] = {"cloud_drive_gib": 0}
rejects("storage: cloud drive size must be positive", bad, "positive number of GiB")
check("storage: native NVMe multipath off only for PX Enterprise on NVMe-oF",
      (ce["storage"]["kernel_args"], cs["storage"]["kernel_args"]) == (["nvme_core.multipath=N"], []))
ent_iscsi = copy.deepcopy(ent); ent_iscsi["storage"]["portworx"]["san_type"] = "ISCSI"
ci = cluster_model(ent_iscsi, EXAMPLE)["storage"]
check("storage: PX Enterprise on iSCSI keeps native NVMe multipath", ci["kernel_args"] == [])
check("storage: transport from san_type", (cs["storage"]["transport"], ci["transport"]) == ("nvme-tcp", "iscsi"))
check("storage: iSCSI limits Portworx to the storage networks' portals",
      [e["name"] for e in ci["portworx"]["env"]] == ["PURE_FLASHARRAY_SAN_TYPE", "PURE_ISCSI_ALLOWED_CIDRS"]
      and ci["portworx"]["env"][1]["value"] == ",".join(n["cidr"] for n in ent_iscsi["storage"]["networks"]))
check("storage: NVMe/TCP env is the SAN type only",
      cs["storage"]["portworx"]["env"] == [{"name": "PURE_FLASHARRAY_SAN_TYPE", "value": "NVMEOF-TCP"}])

# --- addons -------------------------------------------------------------------------------------------------------
ad = copy.deepcopy(base)
ad["addons"] = [{"name": "cnv", "package": "kubevirt-hyperconverged", "channel": "stable", "csv": "kubevirt-hyperconverged-operator.v4.22.0",
                 "namespace": "openshift-cnv",
                 "instance": {"apiVersion": "hco.kubevirt.io/v1beta1", "kind": "HyperConverged",
                              "metadata": {"name": "kubevirt-hyperconverged", "namespace": "openshift-cnv"}}}]
ca = cluster_model(ad, EXAMPLE)["addons"][0]
check("addons: defaults (redhat-operators, own namespace, wait for Available)",
      (ca["source"], ca["all_namespaces"], ca["wait"]) == ("redhat-operators", False, {"type": "Available", "status": "True"}))
check("addons: none by default", cluster_model(base, EXAMPLE)["addons"] == [])
bad = copy.deepcopy(ad); del bad["addons"][0]["csv"]
rejects("addons: csv is required (pinned)", bad, "missing 'csv'")
bad = copy.deepcopy(ad); bad["addons"].append(copy.deepcopy(ad["addons"][0]))
rejects("addons: duplicate name", bad, "defined twice")
bad = copy.deepcopy(ad); del bad["addons"][0]["instance"]["kind"]
rejects("addons: instance needs kind", bad, "instance needs 'kind'")
check("addons: no dependent install plans by default", ca["approve_installplans"] == [])
bad = copy.deepcopy(ad); bad["addons"][0]["approve_installplans"] = [{"namespace": "multicluster-engine"}]
rejects("addons: dependent install plans need a pinned csv", bad, "need namespace and csv")

# --- migration ----------------------------------------------------------------------------------------------------
check("migration: off by default", cluster_model(base, EXAMPLE)["migration"]["enabled"] is False)
mg = copy.deepcopy(ent_iscsi)
mg["migration"] = {
    "enabled": True,
    "source": {"name": "vc1", "server": "vc1.example.com", "datacenter": "dc1", "clone_hosts": ["esx1.example.com"]},
    "copy_offload": {"host_access": {"hosts": ["esx1"], "host_groups": ["esx-hg"]},
                     "datastores": [{"name": "mtv-ds01", "size": "1T", "host_group": "esx-hg",
                                     "vsphere_cluster": "c1", "format_host": "esx1.example.com"}]},
    "plans": [{"name": "p1", "vms": ["vm1"]}]}
cm = cluster_model(mg, EXAMPLE)["migration"]
check("migration: derived user/role, hook on in enterprise mode, plan defaults",
      (cm["source"]["user"].startswith("mtv-"), cm["source"]["role"].startswith("MTV-"),
       cm["copy_offload"]["portworx_hook"], cm["plans"][0]["type"], cm["plans"][0]["target_namespace"])
      == (True, True, True, "cold", "default"))
bad = copy.deepcopy(mg); bad["storage"]["portworx"]["san_type"] = "NVMEOF-TCP"
rejects("migration: copy offload needs a SCSI transport", bad, "needs a SCSI transport")
bad = copy.deepcopy(mg); bad["storage"]["flasharray"]["realm"] = False
rejects("migration: copy offload needs a realm (not array mode)", bad, "needs a FlashArray realm")
bad = copy.deepcopy(mg); bad["migration"]["copy_offload"]["host_access"]["host_groups"] = []
rejects("migration: datastore host group needs realm access", bad, "needs realm access")
bad = copy.deepcopy(mg); bad["migration"]["plans"][0]["type"] = "warm"
rejects("migration: copy offload is cold only", bad, "migrates cold only")
bad = copy.deepcopy(mg); bad["migration"]["source"]["clone_hosts"] = []
rejects("migration: copy offload needs clone hosts", bad, "needs migration.source.clone_hosts")
mgc = copy.deepcopy(mg); mgc["storage"]["portworx"]["mode"] = "csi"
check("migration: no Portworx hook in PX-CSI mode",
      cluster_model(mgc, EXAMPLE)["migration"]["copy_offload"]["portworx_hook"] is False)
mgp = copy.deepcopy(mg); mgp["migration"]["plans"].append({"name": "p2", "vms": ["vm2"], "portworx_hook": False})
cp = cluster_model(mgp, EXAMPLE)["migration"]["plans"]
check("migration: per-plan hook (default inherits, false keeps FADA)",
      [p["portworx_hook"] for p in cp] == [True, False])
bad = copy.deepcopy(mgc); bad["migration"]["plans"][0]["portworx_hook"] = True
rejects("migration: per-plan hook needs Enterprise mode", bad, "portworx_hook needs copy offload")

# --- backup -------------------------------------------------------------------------------------------------------
check("backup: off by default", cluster_model(base, EXAMPLE)["backup"]["enabled"] is False)
bk = copy.deepcopy(ent); bk["backup"] = {"enabled": True}
cb = cluster_model(bk, EXAMPLE)["backup"]
check("backup: defaults (px-central pinned, FADA databases, UI host under apps)",
      (cb["chart"], cb["storageclass"], cb["ui_host"].startswith("px-backup.apps.")) == ("px-central", "px-fa", True))
bad = copy.deepcopy(bk); bad["backup"]["chart_version"] = "3.1"
rejects("backup: chart version is exact", bad, "exact version")
bad = copy.deepcopy(base); bad["backup"] = {"enabled": True}
rejects("backup: needs Portworx", bad, "needs storage.portworx.enabled")

# --- dns forward zones --------------------------------------------------------------------------------------------
fw = copy.deepcopy(base)
fw.setdefault("infra", {}).setdefault("dns", {})["forward_zones"] = [
    {"zone": "Lab.Example.", "servers": ["192.0.2.53"], "probe": "vcenter.lab.example"}]
cf = cluster_model(fw, EXAMPLE)["infra"]["dns"]["forward_zones"]
check("dns forward zones: normalized (lower case, no trailing dot)",
      cf == [{"zone": "lab.example", "servers": ["192.0.2.53"], "probe": "vcenter.lab.example"}])
check("dns forward zones: none by default", cluster_model(base, EXAMPLE)["infra"]["dns"]["forward_zones"] == [])
bad = copy.deepcopy(fw); bad["infra"]["dns"]["forward_zones"][0]["servers"] = ["dns.example"]
rejects("dns forward zones: servers are IP addresses", bad, "is not an IP address")
bad = copy.deepcopy(fw); bad["infra"]["dns"]["forward_zones"][0]["probe"] = "vcenter.other.example"
rejects("dns forward zones: probe inside the zone", bad, "probe must be a name in the zone")
bad = copy.deepcopy(fw); bad["infra"]["dns"]["forward_zones"].append(copy.deepcopy(fw["infra"]["dns"]["forward_zones"][0]))
rejects("dns forward zones: zone listed twice", bad, "listed twice")

# --- dns mode ad ------------------------------------------------------------------------------------------------
adc = copy.deepcopy(base)
adc.setdefault("infra", {}).setdefault("dns", {})["mode"] = "ad"
ca = cluster_model(adc, EXAMPLE)
check("dns ad: server defaults to the first dns_server, http port 5985, no PTRs",
      ca["infra"]["dns"]["ad"]["server"] == "192.0.2.2" and ca["infra"]["dns"]["ad"]["port"] == 5985
      and ca["dns_ptr_records"] == [])
check("dns ad: same forward records as the CoreDNS modes", ca["dns_records"] == cluster_model(base, EXAMPLE)["dns_records"])
rev = copy.deepcopy(adc); rev["infra"]["dns"]["ad"] = {"reverse_zone": "2.0.192.in-addr.arpa."}
cr = cluster_model(rev, EXAMPLE)
check("dns ad: PTRs for every node inside the reverse zone",
      {"name": "51", "target": "master0." + cr["cluster"]["domain"]} in cr["dns_ptr_records"]
      and len(cr["dns_ptr_records"]) == len(cr["nodes"]))
rev16 = copy.deepcopy(adc); rev16["infra"]["dns"]["ad"] = {"reverse_zone": "0.192.in-addr.arpa"}
check("dns ad: PTR names are relative to a shorter reverse zone",
      cluster_model(rev16, EXAMPLE)["dns_ptr_records"][1]["name"] == "51.2")
other = copy.deepcopy(adc); other["infra"]["dns"]["ad"] = {"reverse_zone": "51.100.198.in-addr.arpa"}
check("dns ad: no PTRs for nodes outside the reverse zone", cluster_model(other, EXAMPLE)["dns_ptr_records"] == [])
bad = copy.deepcopy(adc); bad["infra"]["dns"]["ad"] = {"server": "198.51.100.10"}
rejects("dns ad: server is one of the nodes' DNS servers", bad, "must be one of network.dns_servers")
bad = copy.deepcopy(adc); bad["infra"]["dns"]["ad"] = {"port": 443}
rejects("dns ad: WinRM port", bad, "5985 (http) or 5986")
bad = copy.deepcopy(adc); bad["infra"]["dns"]["ad"] = {"reverse_zone": "example.com"}
rejects("dns ad: reverse zone is in-addr.arpa", bad, "IPv4 reverse zone")
bad = copy.deepcopy(fw); bad["infra"]["dns"]["mode"] = "ad"
rejects("dns ad: no CoreDNS forward zones", bad, "conditional forwarders")
bad = copy.deepcopy(base); bad.setdefault("infra", {}).setdefault("dns", {})["mode"] = "bind"
rejects("dns: unknown mode", bad, "'managed', 'shared' or 'ad'")

# --- haproxy_pools ------------------------------------------------------------------------------------------------
csv = "\n".join([
    "# pxname,svname," + ",".join(f"f{i}" for i in range(2, 18)) + ",status",
    "api,bootstrap" + ",x" * 15 + ",DOWN",
    "api,master0" + ",x" * 15 + ",UP",
    "api,BACKEND" + ",x" * 15 + ",UP",
    "ingress,worker0" + ",x" * 15 + ",UP",
    "ingress,worker1" + ",x" * 15 + ",DOWN",
])
check("haproxy_pools: servers only, bootstrap ignored",
      haproxy_pools(csv) == {"api": {"up": ["master0"], "down": []}, "ingress": {"up": ["worker0"], "down": ["worker1"]}})

# --- htpasswd -----------------------------------------------------------------------------------------------------
users = [{"name": "alice", "password": "S3cret$pw"}]
first = htpasswd_render(users, "")
check("htpasswd: $2y$ hashes (OpenShift rejects $2b$)", first.split(":", 1)[1].startswith("$2y$"))
check("htpasswd: idempotent", htpasswd_render(users, first) == first)
b = htpasswd_bindings([{"name": "a", "password": "x", "roles": ["view", {"role": "edit", "namespace": "ns"}]}])
check("htpasswd: cluster-wide and namespaced bindings", [x["key"] for x in b] == ["htpasswd-a-view", "ns/htpasswd-a-clusterrole-edit"])

# --- k8s helpers --------------------------------------------------------------------------------------------------
def obj(name, **conds):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": k, "status": v} for k, v in conds.items()]}}


check("k8s_condition", k8s_condition(obj("x", Ready="True"), "Ready") == "True" and k8s_condition({}, "Ready") == "")
check("k8s_node_readiness", k8s_node_readiness([obj("n1", Ready="True"), obj("n2", Ready="False")])
      == [{"name": "n1", "ready": True}, {"name": "n2", "ready": False}])
check("k8s_unhealthy_operators", k8s_unhealthy_operators([obj("a", Available="True", Degraded="False"),
                                                          obj("b", Available="False", Degraded="False"),
                                                          obj("c", Available="True", Degraded="True")]) == ["b", "c"])
check("k8s_pending_csrs", k8s_pending_csrs([{"metadata": {"name": "p"}, "status": {}},
                                            {"metadata": {"name": "a"}, "status": {"conditions": [{"type": "Approved"}]}}]) == ["p"])
check("kv_lines", kv_lines(["a=1", "noise", "b=x=y"]) == {"a": "1", "b": "x=y"})
check("k8s_names_with_condition", k8s_names_with_condition([obj("p1", Available="True"), obj("p2", Available="False"),
                                                            {"metadata": {"name": "p3"}}], "Available") == ["p1"])
csinodes = [{"metadata": {"name": "n1"}, "spec": {"drivers": [{"name": "pxd.portworx.com"}]}},
            {"metadata": {"name": "n2"}, "spec": {"drivers": None}}]
check("k8s_csinodes_with_driver", k8s_csinodes_with_driver(csinodes, "pxd.portworx.com") == ["n1"])
nns = {"status": {"currentState": {"interfaces": [{"name": "ens33", "mac-address": "00:50:56:01:42:51"},
                                                  {"name": "ens34", "mac-address": "00:50:56:02:42:51"},
                                                  {"name": "br-ex", "type": "ovs-interface"}]}}}
by_mac = nns_ifaces_by_mac(nns)
check("nns_ifaces_by_mac", by_mac == {"00:50:56:01:42:51": "ens33", "00:50:56:02:42:51": "ens34"})
ifs = nmstate_storage_interfaces([m0["storage"][0] | {"mac": "00:50:56:02:42:51"}], by_mac)
check("nmstate_storage_interfaces: static IPv4 on the NIC found by MAC, no IPv6",
      ifs == [{"name": "ens34", "type": "ethernet", "state": "up", "mtu": 1500,
               "ipv4": {"enabled": True, "dhcp": False, "address": [{"ip": "198.51.100.51", "prefix-length": 24}]},
               "ipv6": {"enabled": False}}])
try:
    nmstate_storage_interfaces([m0["storage"][0] | {"mac": "00:50:56:09:09:09"}], by_mac)
    check("nmstate_storage_interfaces: unknown MAC rejected", False)
except AnsibleFilterError:
    check("nmstate_storage_interfaces: unknown MAC rejected", True)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all tests passed'}")
sys.exit(1 if failures else 0)
