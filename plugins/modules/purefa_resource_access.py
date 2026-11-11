#!/usr/bin/python
"""Give an array-level FlashArray resource (a host, a host group, ...) access to realms: purehost access create/delete."""

DOCUMENTATION = r"""
module: purefa_resource_access
short_description: Resource accesses of an array-level FlashArray object into realms
description:
  - Ensures that an array-level resource, for example the host of an ESXi server, has (or no longer has) access to
    the given realms, like C(purehost access create --realm <realm1>,<realm2> <host>). With access, volumes in the
    realm can be connected to that host, for example the temporary FADA volumes of an MTV copy offload (XCOPY), which
    must be in the same realm as the source datastore's ESXi hosts can reach.
  - Only the listed realms are touched; accesses to other realms are left as they are. Check mode supported.
  - Exists because C(everpure.flasharray) (1.45.1, also 1.46.0 and master in 2026-11) creates a resource access only
    as a side effect of C(purefa_host) C(move) with C(modify_resource_access=create), and a move needs a host without
    connected volumes. Uses the same client library (py-pure-client, REST 2.54+ resource-accesses). Retire it once a
    pinned release of the collection manages resource accesses directly.
options:
  fa_url: {description: FlashArray management address., type: str, required: true}
  api_token: {description: API token of an array admin., type: str, required: true, no_log: true}
  validate_certs: {description: Verify the array certificate., type: bool, default: false}
  resource: {description: Name of the array-level resource (no realm prefix)., type: str, required: true}
  resource_type: {description: Its type (endpoint name)., type: str, default: hosts, choices: [hosts, host-groups]}
  realms: {description: Realms the resource gets (present) or loses (absent) access to., type: list, elements: str, required: true}
  state: {description: present or absent., type: str, choices: [present, absent], default: present}
requirements: [py-pure-client]
"""

EXAMPLES = r"""
- name: ESXi host esx01-fc gets access to realm mycluster
  purefa_resource_access:
    fa_url: 192.0.2.10
    api_token: "{{ vault_flasharray_api_token }}"
    resource: esx01-fc
    realms: [mycluster]

- name: Its host group too (needed to connect a realm volume to the group)
  purefa_resource_access:
    fa_url: 192.0.2.10
    api_token: "{{ vault_flasharray_api_token }}"
    resource: esx-fc
    resource_type: host-groups
    realms: [mycluster]
"""

RETURN = r"""
realms: {description: Realms the resource has access to after this run (only those this array reports)., type: list, returned: always}
added: {description: Realms whose access was created in this run., type: list, returned: always}
removed: {description: Realms whose access was deleted in this run., type: list, returned: always}
"""

from ansible.module_utils.basic import AnsibleModule

try:
    from pypureclient import flasharray
    HAS_PYPURECLIENT = True
except ImportError:  # pragma: no cover
    HAS_PYPURECLIENT = False


def _ok(module, res, what):
    if res.status_code != 200:
        msg = "; ".join(getattr(e, "message", str(e)) for e in getattr(res, "errors", []) or [])
        module.fail_json(msg=f"{what} failed: {msg or res.status_code}")
    return list(getattr(res, "items", []) or [])


def _accesses(module, array, resource, resource_type):
    """{realm name: access id} for the resource's accesses into realms."""
    out = {}
    for a in _ok(module, array.get_resource_accesses(), "list resource accesses"):
        res, scope = getattr(a, "resource", None), getattr(a, "scope", None)
        if (getattr(res, "name", None) == resource and getattr(res, "resource_type", None) == resource_type
                and getattr(scope, "resource_type", None) == "realms"):
            out[scope.name] = a.id
    return out


def main():
    module = AnsibleModule(
        argument_spec={
            "fa_url": {"type": "str", "required": True},
            "api_token": {"type": "str", "required": True, "no_log": True},
            "validate_certs": {"type": "bool", "default": False},
            "resource": {"type": "str", "required": True},
            "resource_type": {"type": "str", "default": "hosts", "choices": ["hosts", "host-groups"]},
            "realms": {"type": "list", "elements": "str", "required": True},
            "state": {"type": "str", "choices": ["present", "absent"], "default": "present"},
        },
        supports_check_mode=True,
    )
    if not HAS_PYPURECLIENT:
        module.fail_json(msg="the py-pure-client Python package is required")
    p = module.params
    if "::" in p["resource"]:
        module.fail_json(msg=f"{p['resource']}: give the array-level name (a resource inside a realm needs no access)")
    array = flasharray.Client(target=p["fa_url"], api_token=p["api_token"], verify_ssl=p["validate_certs"],
                              user_agent="ocp-upi purefa_resource_access")
    getter = array.get_hosts if p["resource_type"] == "hosts" else array.get_host_groups
    if not _ok(module, getter(names=[p["resource"]]), f"look up {p['resource_type']} {p['resource']}"):
        module.fail_json(msg=f"{p['resource_type']} {p['resource']} not found")
    known = {r.name for r in _ok(module, array.get_realms(), "list realms")}
    missing = sorted(set(p["realms"]) - known)
    if missing:
        module.fail_json(msg=f"realm(s) not found: {', '.join(missing)}")

    have = _accesses(module, array, p["resource"], p["resource_type"])
    wanted = sorted(set(p["realms"]))
    add = [r for r in wanted if r not in have] if p["state"] == "present" else []
    remove = [r for r in wanted if r in have] if p["state"] == "absent" else []
    if not module.check_mode:
        if add:
            _ok(module, array.post_resource_accesses_batch(items=[flasharray.ResourceAccess(
                resource=flasharray.ReferenceWithType(name=p["resource"], resource_type=p["resource_type"]),
                scope=flasharray.ReferenceWithType(name=r, resource_type="realms")) for r in add]),
                f"create access of {p['resource']} to {', '.join(add)}")
        if remove:
            _ok(module, array.delete_resource_accesses(ids=[have[r] for r in remove]),
                f"delete access of {p['resource']} to {', '.join(remove)}")
        if add or remove:
            have = _accesses(module, array, p["resource"], p["resource_type"])
            bad = [r for r in add if r not in have] + [r for r in remove if r in have]
            if bad:
                module.fail_json(msg=f"the array does not report the change for realm(s): {', '.join(bad)}")
        realms = sorted(have)
    else:
        realms = sorted((set(have) | set(add)) - set(remove))
    module.exit_json(changed=bool(add or remove), realms=realms, added=add, removed=remove)


if __name__ == "__main__":
    main()
