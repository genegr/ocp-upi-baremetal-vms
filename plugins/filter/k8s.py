"""Small helpers for reading Kubernetes objects returned by kubernetes.core.k8s_info."""


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


class FilterModule:
    def filters(self):
        return {
            "k8s_condition": k8s_condition,
            "k8s_node_readiness": k8s_node_readiness,
            "k8s_unhealthy_operators": k8s_unhealthy_operators,
            "k8s_pending_csrs": k8s_pending_csrs,
        }
