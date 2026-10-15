#!/usr/bin/python
"""Approve the kubelet CSRs of expected nodes and (optionally) wait until those nodes are Ready."""

DOCUMENTATION = r"""
module: ocp_csr_approve
short_description: Approve pending node CSRs for a known list of nodes
description:
  - Approves pending CertificateSigningRequests created by joining nodes, but only for the node names given
    (kubelet client CSRs whose subject is C(system:node:<name>), and kubelet serving CSRs requested by
    C(system:node:<name>)). Anything else is left alone.
  - With O(wait=true) it keeps approving (client, then serving certificates) until every listed node is Ready or
    O(timeout) expires.
  - No upstream module approves CSRs (the approval is a subresource), hence this small module.
options:
  kubeconfig: {description: Path to the admin kubeconfig., type: path, required: true}
  nodes: {description: Node names (as they register) whose CSRs may be approved., type: list, elements: str, required: true}
  wait: {description: Keep approving until all nodes are Ready., type: bool, default: false}
  timeout: {description: Seconds to wait when O(wait=true)., type: int, default: 3600}
  interval: {description: Seconds between polls., type: int, default: 20}
requirements: [kubernetes, cryptography]
"""

EXAMPLES = r"""
- name: Approve worker CSRs until the workers are Ready
  ocp_csr_approve:
    kubeconfig: "{{ install_dir }}/auth/kubeconfig"
    nodes: [worker0.ocp4.example.com, worker1.ocp4.example.com]
    wait: true
"""

RETURN = r"""
approved: {description: Names of the CSRs approved., type: list, returned: always}
ready: {description: Listed nodes that are Ready., type: list, returned: always}
"""

import time

from ansible.module_utils.basic import AnsibleModule

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from kubernetes import client, config
    HAS_LIBS = True
except ImportError:  # pragma: no cover
    HAS_LIBS = False

CLIENT_SIGNER = "kubernetes.io/kube-apiserver-client-kubelet"
SERVING_SIGNER = "kubernetes.io/kubelet-serving"
BOOTSTRAPPER = "system:serviceaccount:openshift-machine-config-operator:node-bootstrapper"


def _subject_cn(csr):
    pem = csr.spec.request
    if isinstance(pem, str):
        import base64
        pem = base64.b64decode(pem)
    req = x509.load_pem_x509_csr(pem)
    attrs = req.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else ""


def _is_pending(csr):
    return not (csr.status and csr.status.conditions)


def _expected(csr, nodes):
    """True if this CSR belongs to one of the listed nodes."""
    names = {f"system:node:{n}" for n in nodes}
    signer, user = csr.spec.signer_name, csr.spec.username
    if signer == CLIENT_SIGNER:
        return user in (BOOTSTRAPPER, *names) and _subject_cn(csr) in names
    if signer == SERVING_SIGNER:
        return user in names and _subject_cn(csr) == user
    return False


def _ready_nodes(core, nodes):
    ready = []
    for node in core.list_node().items:
        if node.metadata.name in nodes:
            for cond in node.status.conditions or []:
                if cond.type == "Ready" and cond.status == "True":
                    ready.append(node.metadata.name)
    return sorted(ready)


def main():
    module = AnsibleModule(
        argument_spec={
            "kubeconfig": {"type": "path", "required": True},
            "nodes": {"type": "list", "elements": "str", "required": True},
            "wait": {"type": "bool", "default": False},
            "timeout": {"type": "int", "default": 3600},
            "interval": {"type": "int", "default": 20},
        },
        supports_check_mode=True,
    )
    if not HAS_LIBS:
        module.fail_json(msg="the kubernetes and cryptography Python packages are required")
    p = module.params
    nodes = set(p["nodes"])
    config.load_kube_config(config_file=p["kubeconfig"])
    certs, core = client.CertificatesV1Api(), client.CoreV1Api()

    approved = []
    deadline = time.time() + p["timeout"]
    while True:
        for csr in certs.list_certificate_signing_request().items:
            if not _is_pending(csr) or not _expected(csr, nodes):
                continue
            approved.append(csr.metadata.name)
            if module.check_mode:
                continue
            csr.status = csr.status or client.V1CertificateSigningRequestStatus()
            csr.status.conditions = [client.V1CertificateSigningRequestCondition(
                type="Approved", status="True", reason="AnsibleApprove",
                message="Approved by ocp_csr_approve for an expected node")]
            certs.replace_certificate_signing_request_approval(csr.metadata.name, csr)
        ready = _ready_nodes(core, nodes)
        if not p["wait"] or module.check_mode or set(ready) == nodes:
            break
        if time.time() > deadline:
            module.fail_json(msg=f"timed out: {len(ready)}/{len(nodes)} nodes Ready", approved=approved, ready=ready)
        time.sleep(p["interval"])

    module.exit_json(changed=bool(approved), approved=sorted(set(approved)), ready=ready)


if __name__ == "__main__":
    main()
