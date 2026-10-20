#!/usr/bin/python
"""Ensure a role-less FlashArray admin whose access comes only from management-access policies, plus its API token."""

DOCUMENTATION = r"""
module: purefa_realm_admin
short_description: Realm-scoped FlashArray user (policy membership only) with an API token
description:
  - Creates or updates a local FlashArray admin with B(no global role), whose permissions come only from the given
    management-access policies (for example a policy granting C(storage) scoped to one realm). This is the user
    pattern Portworx/PX-CSI secure multi-tenancy expects.
  - Creates an API token for that user only when it has none (or when O(rotate_token=true)) and returns it once.
  - Exists because C(everpure.flasharray.purefa_user) (1.45.1) always assigns a global role and no module of the
    collection adds a user to a management-access policy. It uses the same client library (py-pure-client) as the
    collection. Retire it once a pinned release of the collection has C(purefa_user) C(access_policy).
options:
  fa_url: {description: FlashArray management address., type: str, required: true}
  api_token: {description: API token of an array admin allowed to manage users and policies., type: str, required: true, no_log: true}
  validate_certs: {description: Verify the array certificate., type: bool, default: false}
  name: {description: User name., type: str, required: true}
  policies: {description: Management-access policies the user must belong to (exactly this set)., type: list, elements: str, required: true}
  password: {description: Password for a new user (random if omitted; the API token is what clients use)., type: str, no_log: true}
  token_timeout: {description: API token lifetime in milliseconds; 0 = no expiry., type: int, default: 0}
  rotate_token: {description: Replace an existing API token., type: bool, default: false}
  state: {description: present or absent., type: str, choices: [present, absent], default: present}
requirements: [py-pure-client]
"""

EXAMPLES = r"""
- name: Realm user for Portworx
  purefa_realm_admin:
    fa_url: 192.0.2.10
    api_token: "{{ vault_flasharray_api_token }}"
    name: ocp4-px
    policies: [ocp4-storage]
  register: realm_user
  no_log: true
"""

RETURN = r"""
created: {description: The user was created in this run., type: bool, returned: always}
api_token: {description: The new API token (only when one was created in this run; empty otherwise)., type: str, returned: always}
has_token: {description: The user has an API token after this run., type: bool, returned: always}
"""

import secrets

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


def _ref(name):
    # AdminPost/AdminPatch.management_access_policies is a list of ReferenceWithType (REST 2.36+).
    return flasharray.ReferenceWithType(name=name)


def _random_password():
    # Meets typical Purity complexity rules (upper, lower, digit, symbol); never needed again (token-only user).
    return "Px" + secrets.token_urlsafe(24) + "9!"


def main():
    module = AnsibleModule(
        argument_spec={
            "fa_url": {"type": "str", "required": True},
            "api_token": {"type": "str", "required": True, "no_log": True},
            "validate_certs": {"type": "bool", "default": False},
            "name": {"type": "str", "required": True},
            "policies": {"type": "list", "elements": "str", "required": True},
            "password": {"type": "str", "no_log": True},
            "token_timeout": {"type": "int", "default": 0},
            "rotate_token": {"type": "bool", "default": False},
            "state": {"type": "str", "choices": ["present", "absent"], "default": "present"},
        },
        supports_check_mode=True,
    )
    if not HAS_PYPURECLIENT:
        module.fail_json(msg="the py-pure-client Python package is required")
    p = module.params
    array = flasharray.Client(target=p["fa_url"], api_token=p["api_token"], verify_ssl=p["validate_certs"],
                              user_agent="ocp-upi purefa_realm_admin")
    res = array.get_admins(names=[p["name"]])
    current = list(getattr(res, "items", []) or []) if res.status_code == 200 else []
    user = current[0] if current else None
    result = {"changed": False, "created": False, "api_token": "", "has_token": False}

    if p["state"] == "absent":
        if user:
            result["changed"] = True
            if not module.check_mode:
                _ok(module, array.delete_admins(names=[p["name"]]), "delete admin")
        module.exit_json(**result)

    wanted = sorted(set(p["policies"]))
    if user is None:
        result.update(changed=True, created=True)
        if module.check_mode:
            module.exit_json(**result)
        _ok(module, array.post_admins(names=[p["name"]], admin=flasharray.AdminPost(
            password=p["password"] or _random_password(),
            management_access_policies=[_ref(n) for n in wanted])), "create admin")
    else:
        # A role user is a member of the built-in policy of that role; the array reports role.name for it.
        role = getattr(getattr(user, "role", None), "name", None)
        if role:
            module.fail_json(msg=f"user {p['name']} exists with global role '{role}'; refusing to manage it as a "
                                 "realm-scoped user")
        have = sorted(r.name for r in (getattr(user, "management_access_policies", None) or []))
        if have != wanted:
            result["changed"] = True
            if not module.check_mode:
                # PATCH admins refuses management_access_policies: membership changes go through the policy members
                # endpoints. Add first, then remove, so the user never has no access at all.
                for name in (n for n in wanted if n not in have):
                    _ok(module, array.post_policies_management_access_members(
                        policy_names=[name],
                        members=flasharray.PolicyMemberPost(members=[flasharray.PolicymemberpostMembers(
                            member=flasharray.ReferenceWithType(name=p["name"], resource_type="admins"))])),
                        f"add to policy {name}")
                for name in (n for n in have if n not in wanted):
                    _ok(module, array.delete_policies_management_access_members(
                        policy_names=[name], member_names=[p["name"]], member_types=["admins"]),
                        f"remove from policy {name}")

    # API token: create only when missing (or rotating). Its value can be read only at creation time.
    token_items = []
    if not result["created"]:
        tres = array.get_admins_api_tokens(names=[p["name"]])
        token_items = list(getattr(tres, "items", []) or []) if tres.status_code == 200 else []
    has_token = any(getattr(getattr(t, "api_token", None), "created_at", None) for t in token_items)
    if has_token and not p["rotate_token"]:
        result["has_token"] = True
        module.exit_json(**result)

    result["changed"] = True
    if module.check_mode:
        module.exit_json(**result)
    if has_token:
        _ok(module, array.delete_admins_api_tokens(names=[p["name"]]), "delete API token")
    kwargs = {"names": [p["name"]]}
    if p["token_timeout"]:
        kwargs["timeout"] = p["token_timeout"]
    items = _ok(module, array.post_admins_api_tokens(**kwargs), "create API token")
    token = getattr(getattr(items[0], "api_token", None), "token", "") if items else ""
    if not token:
        module.fail_json(msg="the array did not return the new API token")
    result.update(api_token=token, has_token=True)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
