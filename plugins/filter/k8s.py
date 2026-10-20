"""Small helpers for reading Kubernetes objects returned by kubernetes.core.k8s_info."""
from ansible.errors import AnsibleFilterError


def k8s_condition(resource, cond_type):
    """Status ("True"/"False"/"Unknown") of the condition `cond_type` in resource.status.conditions, or ""."""
    for cond in ((resource or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") == cond_type:
            return str(cond.get("status", ""))
    return ""


def k8s_node_readiness(nodes):
    """Node resources -> [{"name": ..., "ready": bool}]."""
    return [{"name": n["metadata"]["name"], "ready": k8s_condition(n, "Ready") == "True"} for n in nodes or []]


def k8s_unhealthy_operators(operators):
    """ClusterOperator resources -> names that are not Available or are Degraded."""
    return [o["metadata"]["name"] for o in operators or []
            if k8s_condition(o, "Available") != "True" or k8s_condition(o, "Degraded") == "True"]


def k8s_pending_csrs(csrs):
    """CertificateSigningRequest resources -> names not yet approved or denied."""
    return [r["metadata"]["name"] for r in csrs or [] if not ((r.get("status") or {}).get("conditions"))]


def k8s_names_with_condition(resources, cond_type, status="True"):
    """Resources -> names of those whose condition `cond_type` has `status`."""
    return [r["metadata"]["name"] for r in resources or [] if k8s_condition(r, cond_type) == status]


def k8s_csinodes_with_driver(csinodes, driver):
    """CSINode resources -> names of the nodes where the CSI driver `driver` is registered."""
    return [n["metadata"]["name"] for n in csinodes or []
            if any(d.get("name") == driver for d in ((n.get("spec") or {}).get("drivers") or []))]


def kv_lines(lines):
    """["key=value", ...] (e.g. a check Pod's log_lines) -> {key: value}; other lines are ignored."""
    return dict(str(line).split("=", 1) for line in lines or [] if "=" in str(line))


def nns_ifaces_by_mac(nns):
    """NodeNetworkState resource -> {mac (lowercase): interface name} for its interfaces that have a MAC."""
    ifaces = (((nns or {}).get("status") or {}).get("currentState") or {}).get("interfaces") or []
    return {str(i["mac-address"]).lower(): i["name"] for i in ifaces if i.get("mac-address") and i.get("name")}


def nmstate_storage_interfaces(storage, ifaces_by_mac):
    """A node's storage addresses (cluster model nodes[].storage) -> NMState desiredState.interfaces.

    Static IPv4 only: no gateway, no DNS, no IPv6, so the storage NICs never carry other traffic."""
    out = []
    for s in storage or []:
        name = (ifaces_by_mac or {}).get(str(s["mac"]).lower())
        if not name:
            raise AnsibleFilterError(f"no interface with MAC {s['mac']} ({s['name']}) on this node")
        out.append({
            "name": name,
            "type": "ethernet",
            "state": "up",
            "mtu": int(s["mtu"]),
            "ipv4": {"enabled": True, "dhcp": False,
                     "address": [{"ip": s["address"], "prefix-length": int(s["prefix"])}]},
            "ipv6": {"enabled": False},
        })
    return out


class FilterModule:
    def filters(self):
        return {
            "k8s_condition": k8s_condition,
            "k8s_node_readiness": k8s_node_readiness,
            "k8s_unhealthy_operators": k8s_unhealthy_operators,
            "k8s_pending_csrs": k8s_pending_csrs,
            "k8s_names_with_condition": k8s_names_with_condition,
            "k8s_csinodes_with_driver": k8s_csinodes_with_driver,
            "kv_lines": kv_lines,
            "nns_ifaces_by_mac": nns_ifaces_by_mac,
            "nmstate_storage_interfaces": nmstate_storage_interfaces,
        }
