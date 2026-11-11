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
            # Opt-in: other zones the cluster must resolve, forwarded to their own DNS servers
            # ([{zone: lab.example, servers: [192.0.2.53], probe: vcenter.lab.example}]), e.g. a source vCenter's zone
            # for VM migration. probe: a name in the zone that must resolve after a change.
            "forward_zones": [],
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
        # kubernetes-nmstate operator for the storage IPs (pinned CSV). A cluster upgraded past it records its CSV here.
        "nmstate_csv": "kubernetes-nmstate-operator.4.22.0-202609301304",
        "flasharray": {
            "endpoint": "",
            "token_var": "vault_flasharray_api_token",  # vault variable with this array's admin API token
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
            # csi: PX-CSI only (FlashArray Direct Access volumes). enterprise: Portworx Enterprise with storage pools on
            # FlashArray cloud drives, plus the PX-CSI integration that takes over the Direct Access (FADA) volumes.
            "mode": "csi",
            "channel": "stable",
            "operator_csv": "portworx-operator.v26.4.0",
            "image": "portworx/px-pure-csi-driver:26.3.0",       # csi mode
            "san_type": "NVMEOF-TCP",
            "namespace": "portworx",
            "storageclass": {"name": "px-fa", "default": False},  # the FADA StorageClass (both modes)
            "enterprise": {
                "image": "portworx/oci-monitor:3.7.1.1",
                "cloud_drive_gib": 500,          # storage pool drive per node, in the realm pod
                "metadata_drive_gib": 64,        # system metadata drive per node
                "csi_integration": True,         # portworx.io/pure-csi-integration (PX >= 3.7.0; cannot be reverted)
                "misc_args": "-T px-storev2",
                "kvdb_tls": True,                # internal KVDB over TLS (the operator deploys cert-manager)
                "telemetry": True,
                "kubevirt_storageclasses": True,  # px-rwx-block-kubevirt (live migration) + px-rwx-file-kubevirt
            },
        },
    },
    # Day-2 operators (playbooks/addons.yml): each pinned to an exact CSV with Manual approval, plus an optional
    # instance (the operator's CR) and what to wait for. Empty by default.
    "addons": [],
    # Opt-in: VM migration from vSphere with the Migration Toolkit for Virtualization (playbooks/migration.yml; MTV
    # itself is an addon), with FlashArray XCOPY copy offload into the cluster's realm pod and, in Portworx
    # Enterprise mode, the Portworx hook that moves the disks onto Portworx volumes.
    "migration": {
        "enabled": False,
        "namespace": "openshift-mtv",
        "source": {
            "name": "",               # MTV Provider name, e.g. the vCenter's short name
            "server": "",             # vCenter FQDN
            "datacenter": "",
            "insecure": True,
            "sso_domain": "vsphere.local",   # the vCenter's SSO system domain (where the MTV user is created)
            "admin_user_var": "vault_vsphere_user",          # vault variables of an SSO administrator
            "admin_password_var": "vault_vsphere_password",
            "user": "",               # default mtv-<cluster>
            "role": "",               # default MTV-<cluster>
            "clone_hosts": [],        # ESXi hosts (vCenter names) that get MTV's SSH key and run the XCOPY clones
        },
        "copy_offload": {
            "enabled": True,
            # Array-level FlashArray objects (the clone hosts as the array names them, and their host group) that get
            # access to the cluster's realm, so realm volumes can be connected to them.
            "host_access": {"hosts": [], "host_groups": []},
            # VMFS datastores on volumes in the realm pod (XCOPY needs source and target in the same realm and pod):
            # [{name, size: 1T, host_group, vsphere_cluster, format_host}]
            "datastores": [],
            "storageclass": "px-fa",                       # FADA target of the XCOPY populator
            "access_mode": "ReadWriteMany",
            "volume_mode": "Block",
            "portworx_hook": True,                         # enterprise mode: move the disks onto Portworx volumes
            "pxd_storageclass": "px-rwx-block-kubevirt",
            "hook_image": "docker.io/portworx/fa-pxd-hook:v1.0.1@sha256:"
                          "b688b7c901437d04ae3e0f09f545e369f914f48aa4abce0389841f9b1609074f",
            "converter_image": "docker.io/portworx/fa-pxd-converter:v1.0.1@sha256:"
                               "120cf7b2cd1060d8d72b6d7c732fbfe40061de5cb33cae191caccc1625f78c52",
        },
        "network_map": [],            # [{source: <port group name>, destination: pod}]
        "plans": [],                  # [{name, vms: [names], target_namespace, type: cold}] (started by hand)
    },
    # Opt-in: Portworx Backup (playbooks/backup.yml), the px-central Helm chart with pxbackup on, on this cluster.
    # Its databases want a block StorageClass (several hundred GiB, thin): FADA volumes by default.
    "backup": {
        "enabled": False,
        "namespace": "px-backup",
        "chart_repo": "https://charts.portworx.io/",
        "chart": "px-central",
        "chart_version": "3.1.2",
        "storageclass": "px-fa",
        "ui_host": "",                # default px-backup.apps.<cluster domain> (edge Route)
        "values": {},                 # extra chart values, merged last
        # Optional S3 target on a FlashBlade: object store account, user + access key (kept in .state), bucket.
        "s3": {
            "flashblade": "",         # FlashBlade management address
            "token_var": "vault_flashblade_api_token",
            "endpoint": "",           # data VIP the cluster reaches for S3 (http(s)://<endpoint>)
            "account": "",            # default: the cluster name
            "user": "px-backup",
            "bucket": "",             # default: <cluster>-px-backup
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
    seen_zones = set()
    for fz in dns["forward_zones"]:
        zname = str(fz.get("zone", "")).rstrip(".").lower()
        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$", zname):
            _die(f"infra.dns.forward_zones: '{fz.get('zone')}' is not a DNS zone name")
        if zname in seen_zones or zname == c["cluster"]["domain"]:
            _die(f"infra.dns.forward_zones: {zname} is listed twice or is the cluster's own zone")
        seen_zones.add(zname)
        servers = fz.get("servers") or []
        if not servers:
            _die(f"infra.dns.forward_zones {zname}: servers is empty")
        for s in servers:
            try:
                ipaddress.ip_address(str(s))
            except ValueError:
                _die(f"infra.dns.forward_zones {zname}: '{s}' is not an IP address")
        probe = str(fz.get("probe", "")).rstrip(".").lower()
        if not probe.endswith("." + zname):
            _die(f"infra.dns.forward_zones {zname}: probe must be a name in the zone that resolves (checked after a change)")
        fz["zone"] = zname
        fz["servers"] = [str(s) for s in servers]
        fz["probe"] = probe
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
    _addons(c)
    _migration(c)
    _backup(c)
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
    if not re.match(r"^vault_[a-z0-9_]+$", str(fa["token_var"])):
        _die("storage.flasharray.token_var must name a vault variable (vault_...)")
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
        if px["mode"] not in ("csi", "enterprise"):
            _die("storage.portworx.mode must be csi or enterprise")
        if px["mode"] == "enterprise":
            ent = px["enterprise"]
            for key in ("cloud_drive_gib", "metadata_drive_gib"):
                if not isinstance(ent[key], int) or ent[key] <= 0:
                    _die(f"storage.portworx.enterprise.{key} must be a positive number of GiB")
            if ent["csi_integration"] and _image_version(ent["image"]) < (3, 7):
                _die("storage.portworx.enterprise.csi_integration needs Portworx Enterprise 3.7.0 or later "
                     f"(image {ent['image']})")
    # The node MachineConfig is per transport (99-<role>-pure-<transport>); the phase removes the other transports'.
    st["transport"] = {"NVMEOF-TCP": "nvme-tcp", "NVMEOF-RDMA": "nvme-rdma", "ISCSI": "iscsi", "FC": "fc"}.get(
        px["san_type"], "nvme-tcp")
    # Kernel arguments for the storage MachineConfig. Portworx Enterprise's FlashArray cloud drives over NVMe-oF need
    # dm-multipath, so RHCOS's native NVMe multipath goes off (PX refuses to start without it). PX-CSI alone keeps it.
    nvme_enterprise = px["enabled"] and px["mode"] == "enterprise" and st["transport"].startswith("nvme")
    st["kernel_args"] = ["nvme_core.multipath=N"] if nvme_enterprise else []
    # Portworx env. iSCSI: log in only to the array's portals on the storage networks (an array can have iSCSI
    # interfaces on subnets the nodes don't reach, and a failed login fails the attach).
    px["env"] = [{"name": "PURE_FLASHARRAY_SAN_TYPE", "value": px["san_type"]}]
    if st["transport"] == "iscsi" and st["networks"]:
        px["env"].append({"name": "PURE_ISCSI_ALLOWED_CIDRS", "value": ",".join(sn["cidr"] for sn in st["networks"])})


def _image_version(image):
    """'portworx/oci-monitor:3.7.1.1' -> (3, 7, 1, 1); untagged or non-numeric -> (0,)."""
    tag = str(image).rsplit(":", 1)[-1] if ":" in str(image) else ""
    parts = re.findall(r"\d+", tag.split("-", 1)[0])
    return tuple(int(p) for p in parts) or (0,)


def _addons(c):
    """Validate the addons list: pinned OLM operators with an optional instance CR."""
    seen = set()
    for i, a in enumerate(c["addons"]):
        for key in ("name", "package", "channel", "csv", "namespace"):
            if not a.get(key):
                _die(f"addons[{i}]: missing '{key}'")
        if a["name"] in seen:
            _die(f"addon {a['name']} defined twice")
        seen.add(a["name"])
        a.setdefault("source", "redhat-operators")
        a.setdefault("all_namespaces", False)
        inst = a.get("instance")
        if inst is not None:
            for key in ("apiVersion", "kind", "metadata"):
                if not inst.get(key):
                    _die(f"addon {a['name']}: instance needs '{key}'")
            a.setdefault("wait", {"type": "Available", "status": "True"})
        # Operators the instance installs itself (with the parent's Manual approval): pinned CSVs to approve.
        a.setdefault("approve_installplans", [])
        for d in a["approve_installplans"]:
            if not (isinstance(d, dict) and d.get("namespace") and d.get("csv")):
                _die(f"addon {a['name']}: approve_installplans entries need namespace and csv")
        if a["approve_installplans"] and inst is None:
            _die(f"addon {a['name']}: approve_installplans needs an instance (it installs those operators)")


def _migration(c):
    """Validate the migration section and derive its names (MTV user/role, the hook's on/off)."""
    m = c["migration"]
    if not m["enabled"]:
        return
    src, co = m["source"], m["copy_offload"]
    for key in ("name", "server", "datacenter"):
        if not src[key]:
            _die(f"migration.source.{key} is required")
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", src["name"]):
        _die("migration.source.name must be a DNS label (it names the MTV Provider)")
    for key in ("admin_user_var", "admin_password_var"):
        if not re.match(r"^vault_[a-z0-9_]+$", str(src[key])):
            _die(f"migration.source.{key} must name a vault variable (vault_...)")
    src["user"] = src["user"] or f"mtv-{c['cluster']['name']}"
    src["role"] = src["role"] or f"MTV-{c['cluster']['name']}"
    px = c["storage"]["portworx"]
    if co["enabled"]:
        if not px["enabled"]:
            _die("migration.copy_offload needs storage.portworx.enabled (FADA target volumes in the realm pod)")
        if px["san_type"] not in ("ISCSI", "FC"):
            _die("migration.copy_offload (XCOPY) needs a SCSI transport: storage.portworx.san_type ISCSI or FC")
        if not src["clone_hosts"]:
            _die("migration.copy_offload needs migration.source.clone_hosts (ESXi hosts that run the clones)")
        names = set()
        for i, ds in enumerate(co["datastores"]):
            for key in ("name", "size", "host_group", "vsphere_cluster", "format_host"):
                if not ds.get(key):
                    _die(f"migration.copy_offload.datastores[{i}]: missing '{key}'")
            if ds["name"] in names:
                _die(f"migration datastore {ds['name']} defined twice")
            names.add(ds["name"])
            if ds["host_group"] not in co["host_access"]["host_groups"]:
                _die(f"migration datastore {ds['name']}: host group {ds['host_group']} needs realm access "
                     "(migration.copy_offload.host_access.host_groups)")
        # The Portworx hook moves the FADA disks onto Portworx volumes: only with Portworx Enterprise.
        co["portworx_hook"] = bool(co["portworx_hook"]) and px["mode"] == "enterprise"
    seen = set()
    for i, p in enumerate(m["plans"]):
        if not p.get("name") or not p.get("vms"):
            _die(f"migration.plans[{i}]: needs name and vms")
        if p["name"] in seen:
            _die(f"migration plan {p['name']} defined twice")
        seen.add(p["name"])
        p.setdefault("target_namespace", "default")
        p.setdefault("type", "cold")
        if p["type"] not in ("cold", "warm"):
            _die(f"migration plan {p['name']}: type must be cold or warm")
        if co["enabled"] and p["type"] != "cold":
            _die(f"migration plan {p['name']}: copy offload migrates cold only")
    # NetworkMap pairs: destination "pod" or "<namespace>/<NetworkAttachmentDefinition>" (multus).
    m["network_map_spec"] = []
    for i, nm in enumerate(m["network_map"]):
        if not nm.get("source"):
            _die(f"migration.network_map[{i}]: missing 'source'")
        dest = nm.setdefault("destination", "pod")
        if dest == "pod":
            d = {"type": "pod"}
        elif re.match(r"^[a-z0-9-]+/[a-z0-9-]+$", str(dest)):
            d = {"type": "multus", "namespace": dest.split("/")[0], "name": dest.split("/")[1]}
        else:
            _die(f"migration.network_map[{i}]: destination must be pod or <namespace>/<network-attachment>")
        m["network_map_spec"].append({"source": {"name": nm["source"]}, "destination": d})


def _backup(c):
    """Validate the backup section and derive the UI host."""
    b = c["backup"]
    if not b["enabled"]:
        return
    if not c["storage"]["portworx"]["enabled"]:
        _die("backup needs storage.portworx.enabled (Stork and a block StorageClass for its databases)")
    if not re.match(r"^\d+\.\d+\.\d+$", str(b["chart_version"])):
        _die("backup.chart_version must be an exact version (pinned)")
    if not isinstance(b["values"], dict):
        _die("backup.values must be a mapping of extra chart values")
    b["ui_host"] = b["ui_host"] or f"px-backup.apps.{c['cluster']['domain']}"
    s3 = b["s3"]
    if s3["flashblade"]:
        if not s3["endpoint"]:
            _die("backup.s3.endpoint (the FlashBlade data VIP for S3) is required with backup.s3.flashblade")
        if not re.match(r"^vault_[a-z0-9_]+$", str(s3["token_var"])):
            _die("backup.s3.token_var must name a vault variable (vault_...)")
        s3["account"] = s3["account"] or c["cluster"]["name"]
        s3["bucket"] = s3["bucket"] or f"{c['cluster']['name']}-px-backup"
        # S3 bucket names: 3-63 lower-case letters, digits, '-' (and '.')
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", s3["bucket"]):
            _die(f"backup.s3.bucket '{s3['bucket']}' is not a valid S3 bucket name")


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
