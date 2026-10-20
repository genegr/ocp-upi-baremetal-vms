"""cluster_model: validate a cluster config and derive everything the playbooks and OpenTofu need.

    c: "{{ raw_config | cluster_model(config_file) }}"

The config (clusters/<name>.yaml or cluster.yaml) is the single source of truth. This filter merges the defaults,
validates the config, and adds the derived values used everywhere else:
  cluster.domain / topology / masters_schedulable (auto resolved)
  network.prefix, network.dns_search default
  nodes[].fqdn / hostname / cpus / memory_mb / disk_gb / nested_virt / nics[] (one MAC per NIC)
  lb.api / lb.ingress / lb.vips      (HAProxy backends)
  dns_records                        (the cluster zone)
  identity.htpasswd.users_file       (absolute path)
Hypervisor-specific rules live only in PLATFORMS (see platforms/README.md).
"""
import copy
import ipaddress
import os
import re

from ansible.errors import AnsibleFilterError

ROLES = ("bootstrap", "master", "worker")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

DEFAULTS = {
    "cluster": {
        "ocp_channel": "stable-4.22",
        "ocp_version": "",
        "architecture": "x86_64",
        "network_type": "OVNKubernetes",
        "cluster_network": "10.128.0.0/14",
        "host_prefix": 23,
        "service_network": "172.30.0.0/16",
        "pull_secret_file": "",          # optional; normally vault_pull_secret in clusters/vault.yml
        "ssh_pubkey_file": "",           # optional extra key for core@; the project key is always added
        "masters_schedulable": "auto",   # auto: true only when there are no workers (compact cluster)
        "hostname_style": "fqdn",
        "install_disk": "/dev/sda",
        "kubeconfig_copy": "",
        "install_config_extra": {},
    },
    "network": {"dns_search": []},
    "roles": {
        "bootstrap": {"cpus": 4, "memory_mb": 16384, "disk_gb": 120, "nested_virt": False, "extra_networks": []},
        "master": {"cpus": 4, "memory_mb": 16384, "disk_gb": 120, "nested_virt": False, "extra_networks": []},
        "worker": {"cpus": 8, "memory_mb": 32768, "disk_gb": 120, "nested_virt": False, "extra_networks": []},
    },
    "infra": {
        "enabled": True,
        "user": "ubuntu",
        "base_dir": "~/containers",
        "dns": {
            "mode": "managed",
            "listen": [],
            "forwarders": ["1.1.1.1"],
            "image": "coredns/coredns:1.12.0",
            "shared": {
                "config_dir": "",
                "container_config_dir": "/etc/coredns",
                "container": "coredns",
                "legacy_zone_file": "",
            },
        },
        "extra_records": [],
        "haproxy": {
            "image": "haproxy:3.2",
            "stats_port": 9000,
            "stats_user": "admin",
            "stats_password": "admin",
            "replace_compose_dir": "",
        },
    },
    # Day-2 identity: an HTPasswd identity provider configured at the end of the install (and by users.yml) from a
    # separate, gitignored user list (it holds passwords, so it can be vault-encrypted).
    "identity": {
        "htpasswd": {
            "enabled": False,
            "users_file": "",   # default: <config without .yaml>.htpasswd.yml, e.g. clusters/<name>.htpasswd.yml
        },
    },
    "tools": {
        "tofu_version": "1.12.6",
        "coreos_installer_image": "quay.io/coreos/coreos-installer:release",
    },
    # Day-2 persistent storage (playbooks/storage.yml): node storage networks (NMState, static IPs on extra NICs),
    # a dedicated FlashArray realm + user, and Portworx CSI (PX-CSI). All opt-in.
    "storage": {
        "networks": [],
        "flasharray": {
            "endpoint": "",
            "validate_certs": False,
            "realm": "",          # default: the cluster name
            "quota": "",          # e.g. 10T
            "pod": "px-csi",
            "policy": "",         # default: <realm>-storage
            "role": "storage",    # role granted by the policy, scoped to the realm
            "user": "",           # default: <cluster>-px
        },
        "portworx": {
            "enabled": False,
            "channel": "stable",
            "operator_csv": "portworx-operator.v26.4.0",
            "image": "portworx/px-pure-csi-driver:26.3.0",
            "san_type": "NVMEOF-TCP",
            "namespace": "portworx",
            "storageclass": {"name": "px-fa", "default": False},
        },
    },
}

# Hypervisor-specific rules; everything else here is platform-neutral. A new platform needs an entry here (its
# config section's defaults and required keys, MAC constraints) plus platforms/<name>/ — see platforms/README.md.
PLATFORMS = {
    "vsphere": {
        "defaults": {
            "insecure": True,
            "esxi_host": "",
            "resource_pool": "",
            "create_folder": True,
            "guest_id": "rhel9_64Guest",
            "firmware": "efi",
            "thin_disks": True,
        },
        "required": ("server", "datacenter", "cluster", "datastore", "iso_datastore", "iso_dir", "folder", "network"),
        # vSphere accepts static MACs only in its manual range 00:50:56:00:00:00-00:50:56:3f:ff:ff.
        "mac_prefix": "00:50:56:",
        "max_fourth_octet": 0x3F,
    },
}


def _die(msg):
    raise AnsibleFilterError(f"cluster config: {msg}")


def _plain(value):
    """Turn Ansible's wrapped/unsafe types into plain dicts/lists/strings."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        return str(value)
    return value


def _merge(base, over):
    """Deep-merge `over` onto a copy of `base` (dicts merge, everything else replaces)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _require(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or cur.get(part) in (None, ""):
            _die(f"missing required key '{path}'")
        cur = cur[part]
    return cur


def _nic_mac(mac, index, max_fourth):
    """Extra NIC n reuses NIC0's MAC with the 4th octet increased by n."""
    octets = mac.split(":")
    fourth = int(octets[3], 16) + index
    if fourth > max_fourth:
        _die(f"MAC {mac}: extra NIC {index} would push the 4th octet past {max_fourth:02x}")
    octets[3] = f"{fourth:02x}"
    return ":".join(octets)


def cluster_model(raw, config_path):
    c = _merge(DEFAULTS, _plain(raw or {}))
    config_path = str(config_path)

    platform = c.get("platform")
    if platform not in PLATFORMS:
        _die(f"platform must be one of {sorted(PLATFORMS)} (got {platform!r})")
    plat = PLATFORMS[platform]
    c[platform] = _merge(plat["defaults"], c.get(platform))

    for key in ("cluster.name", "cluster.base_domain", "network.cidr", "network.gateway",
                "network.dns_servers", "network.api_vip", "network.ingress_vip") \
            + tuple(f"{platform}.{k}" for k in plat["required"]):
        _require(c, key)
    if not c.get("nodes"):
        _die("no nodes defined")

    cl, net = c["cluster"], c["network"]
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", str(cl["name"])):
        _die("cluster.name must be a DNS label (lowercase letters, digits, '-'); it also names .state/clusters/<name>/")
    cl["domain"] = f"{cl['name']}.{cl['base_domain']}"
    if not net["dns_search"]:
        net["dns_search"] = [cl["base_domain"]]
    try:
        cidr = ipaddress.ip_network(net["cidr"], strict=True)
    except ValueError as exc:
        _die(f"network.cidr: {exc}")
    net["prefix"] = cidr.prefixlen
    for key in ("gateway", "api_vip", "ingress_vip"):
        if ipaddress.ip_address(net[key]) not in cidr:
            _die(f"network.{key} {net[key]} is outside {cidr}")
    if cl["hostname_style"] not in ("fqdn", "short"):
        _die("cluster.hostname_style must be 'fqdn' or 'short'")

    # Topology: "compact" = no workers, the masters also run workloads (3-node cluster); else "standard".
    has_workers = any(n.get("role") == "worker" for n in c["nodes"])
    cl["topology"] = "standard" if has_workers else "compact"
    sched = cl["masters_schedulable"]
    if sched == "auto":
        sched = not has_workers
    elif not isinstance(sched, bool):
        _die("cluster.masters_schedulable must be auto, true or false")
    if not has_workers and not sched:
        _die("no workers defined, so masters_schedulable must be auto or true (routers and apps run on the masters)")
    cl["masters_schedulable"] = sched

    seen = {"name": set(), "ip": set(), "mac": set()}
    nodes = []
    for n in c["nodes"]:
        for key in ("name", "role", "ip", "mac"):
            if not n.get(key):
                _die(f"node {n}: missing '{key}'")
        if n["role"] not in ROLES:
            _die(f"node {n['name']}: role must be one of {ROLES}")
        mac = n["mac"].lower()
        if not MAC_RE.match(mac) or int(mac[:2], 16) & 1:
            _die(f"node {n['name']}: MAC {mac} must be a unicast aa:bb:cc:dd:ee:ff address")
        if not mac.startswith(plat.get("mac_prefix", "")) or int(mac.split(":")[3], 16) > plat.get("max_fourth_octet", 0xFF):
            _die(f"node {n['name']}: MAC {mac} is not accepted by {platform} "
                 f"(prefix {plat.get('mac_prefix') or 'any'}, 4th octet <= {plat.get('max_fourth_octet', 0xFF):02x})")
        if ipaddress.ip_address(n["ip"]) not in cidr:
            _die(f"node {n['name']}: {n['ip']} is outside {cidr}")
        role = c["roles"][n["role"]]
        node = {
            "name": n["name"],
            "role": n["role"],
            "ip": n["ip"],
            "fqdn": f"{n['name']}.{cl['domain']}",
            "cpus": n.get("cpus", role["cpus"]),
            "memory_mb": n.get("memory_mb", role["memory_mb"]),
            "disk_gb": n.get("disk_gb", role["disk_gb"]),
            # Opt-in, for OpenShift Virtualization only: on the nodes that run workloads (workers, or masters when
            # they are schedulable). Never on bootstrap.
            "nested_virt": bool(n.get("nested_virt", role["nested_virt"]))
            if n["role"] == "worker" or (n["role"] == "master" and sched) else False,
        }
        node["hostname"] = node["fqdn"] if cl["hostname_style"] == "fqdn" else node["name"]
        # Network names are the platform's own (vSphere port group, Nutanix subnet, Proxmox bridge, ...).
        networks = [c[platform]["network"]] + list(n.get("extra_networks", role["extra_networks"]))
        node["nics"] = [{"network": netname, "mac": _nic_mac(mac, i, plat.get("max_fourth_octet", 0xFF)),
                         "primary": i == 0}
                        for i, netname in enumerate(networks)]
        for key, val in (("name", node["name"]), ("ip", node["ip"])):
            if val in seen[key]:
                _die(f"duplicate {key} {val}")
            seen[key].add(val)
        for nic in node["nics"]:
            if nic["mac"] in seen["mac"]:
                _die(f"duplicate MAC {nic['mac']} (node {node['name']})")
            seen["mac"].add(nic["mac"])
        nodes.append(node)
    c["nodes"] = nodes

    by_role = {r: [n for n in nodes if n["role"] == r] for r in ROLES}
    if len(by_role["bootstrap"]) != 1:
        _die("exactly one bootstrap node is required")
    if len(by_role["master"]) not in (1, 3, 4, 5):
        _die("control plane needs 1, 3, 4 or 5 masters")

    # Ingress lands on workers; with no workers the masters carry the routers.
    ingress = by_role["worker"] or by_role["master"]
    c["lb"] = {
        "api": [{"name": n["name"], "ip": n["ip"], "backup": n["role"] == "bootstrap"}
                for n in by_role["bootstrap"] + by_role["master"]],
        "ingress": [{"name": n["name"], "ip": n["ip"]} for n in ingress],
        "vips": sorted({net["api_vip"], net["ingress_vip"]}),
    }

    dns = c["infra"]["dns"]
    if dns["mode"] not in ("managed", "shared"):
        _die("infra.dns.mode must be 'managed' or 'shared'")
    if dns["mode"] == "shared" and not dns["shared"]["config_dir"]:
        _die("infra.dns.shared.config_dir is required in shared mode")
    if dns["mode"] == "managed" and not dns["listen"]:
        dns["listen"] = list(net["dns_servers"])
    if c["infra"]["enabled"]:
        _require(c, "infra.host")

    records = [
        {"name": "api", "ip": net["api_vip"]},
        {"name": "api-int", "ip": net["api_vip"]},
        {"name": "*.apps", "ip": net["ingress_vip"]},
    ] + [{"name": n["name"], "ip": n["ip"]} for n in nodes] + list(c["infra"]["extra_records"])
    c["dns_records"] = records

    # Resolve the HTPasswd user list: relative paths are relative to the config file's directory.
    htp = c["identity"]["htpasswd"]
    if not isinstance(htp["enabled"], bool):
        _die("identity.htpasswd.enabled must be true or false")
    config_abs = os.path.abspath(config_path)
    if htp["users_file"]:
        htp["users_file"] = os.path.abspath(os.path.join(os.path.dirname(config_abs),
                                                         os.path.expanduser(htp["users_file"])))
    else:
        htp["users_file"] = re.sub(r"\.ya?ml$", "", config_abs) + ".htpasswd.yml"

    _storage(c, nodes, net)
    return c


def _storage(c, nodes, net):
    """Validate the storage section and derive each node's static storage addresses."""
    st = c["storage"]
    taken = {n["ip"] for n in nodes} | {net["api_vip"], net["ingress_vip"], net["gateway"]} | set(net["dns_servers"])
    for n in nodes:
        n["storage"] = []
    seen_names = set()
    for sn in st["networks"]:
        for key in ("name", "nic", "cidr"):
            if sn.get(key) in (None, ""):
                _die(f"storage.networks entry {sn}: missing '{key}'")
        if sn["name"] in seen_names:
            _die(f"storage network {sn['name']} defined twice")
        seen_names.add(sn["name"])
        try:
            scidr = ipaddress.ip_network(sn["cidr"], strict=True)
        except ValueError as exc:
            _die(f"storage network {sn['name']}: {exc}")
        sn.setdefault("mtu", 1500)
        host = sn.get("host", "same")
        if host != "same" and "host_offset" not in sn:
            _die(f"storage network {sn['name']}: host must be 'same' or set host_offset")
        # Storage IPs go on the nodes that run workloads: workers, or the masters of a compact cluster.
        for n in nodes:
            if n["role"] == "bootstrap" or (n["role"] == "master" and not c["cluster"]["masters_schedulable"]):
                continue
            if not 0 < int(sn["nic"]) < len(n["nics"]):
                _die(f"storage network {sn['name']}: node {n['name']} has no NIC {sn['nic']} "
                     f"(add roles.{n['role']}.extra_networks)")
            hostnum = int(ipaddress.ip_address(n["ip"])) - int(ipaddress.ip_network(net["cidr"]).network_address)
            if host != "same":
                hostnum = int(sn["host_offset"]) + nodes.index(n)
            addr = ipaddress.ip_address(int(scidr.network_address) + hostnum)
            if addr not in scidr or addr in (scidr.network_address, scidr.broadcast_address):
                _die(f"storage network {sn['name']}: {addr} for {n['name']} is not a usable host in {scidr}")
            if str(addr) in taken:
                _die(f"storage network {sn['name']}: {addr} for {n['name']} collides with another address")
            taken.add(str(addr))
            n["storage"].append({"name": sn["name"], "mac": n["nics"][int(sn["nic"])]["mac"],
                                 "address": str(addr), "prefix": scidr.prefixlen, "mtu": int(sn["mtu"])})

    # MachineConfigPools of the nodes that run workloads (they get the multipath/udev MachineConfig).
    has_workers = any(n["role"] == "worker" for n in nodes)
    st["mc_roles"] = (["master"] if c["cluster"]["masters_schedulable"] else []) + (["worker"] if has_workers else [])
    st["nodes"] = [n["name"] for n in nodes if n["storage"]]

    fa = st["flasharray"]
    fa["realm"] = fa["realm"] or c["cluster"]["name"]
    fa["policy"] = fa["policy"] or f"{fa['realm']}-storage"
    fa["user"] = fa["user"] or f"{c['cluster']['name']}-px"
    for key in ("realm", "pod", "policy", "user"):
        if not re.match(r"^[A-Za-z0-9]([-A-Za-z0-9]*[A-Za-z0-9])?$", str(fa[key])):
            _die(f"storage.flasharray.{key} '{fa[key]}' must be letters, digits and '-'")
    px = st["portworx"]
    if px["enabled"]:
        if not fa["endpoint"]:
            _die("storage.portworx.enabled needs storage.flasharray.endpoint")
        if px["san_type"] not in ("NVMEOF-TCP", "ISCSI", "FC", "NVMEOF-RDMA"):
            _die("storage.portworx.san_type must be NVMEOF-TCP, ISCSI, FC or NVMEOF-RDMA")
        if px["san_type"] in ("NVMEOF-TCP", "ISCSI") and not st["networks"]:
            _die(f"storage.portworx.san_type {px['san_type']} needs storage.networks (nodes need a data path)")


def haproxy_pools(csv_text):
    """HAProxy stats CSV -> {pool: {"up": [...], "down": [...]}} (servers only; bootstrap and stats skipped)."""
    pools = {}
    for line in str(csv_text).splitlines()[1:]:
        f = line.split(",")
        if len(f) < 18 or f[1] in ("FRONTEND", "BACKEND") or f[0] == "stats" or f[1] == "bootstrap":
            continue
        pool = pools.setdefault(f[0], {"up": [], "down": []})
        pool["up" if f[17].startswith("UP") else "down"].append(f[1])
    return pools


class FilterModule:
    def filters(self):
        return {"cluster_model": cluster_model, "haproxy_pools": haproxy_pools}
