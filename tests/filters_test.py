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
