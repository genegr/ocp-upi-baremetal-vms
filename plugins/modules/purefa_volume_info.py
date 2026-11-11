#!/usr/bin/python
"""Read-only facts (serial, size) of named FlashArray volumes, without the volume-wide scan that fails on 6.12."""

DOCUMENTATION = r"""
module: purefa_volume_info
short_description: Serial and size of named FlashArray volumes (read-only)
description:
  - Returns, for each named volume (realm/pod-qualified names allowed), whether it exists, its serial (the SCSI LUN of
    a Pure volume is C(naa.624a9370) + the lower-case serial) and its provisioned size.
  - Exists because C(everpure.flasharray.purefa_info) C(volumes) (1.45.1, also 1.46.0) fails on Purity//FA 6.12 when
    any volume on the array has no priority adjustment (Everpure-Ansible/FlashArray-Collection#1103). Retire it
    together with that workaround once a pinned release has the fix (#1105).
options:
  fa_url: {description: FlashArray management address., type: str, required: true}
  api_token: {description: API token., type: str, required: true, no_log: true}
  validate_certs: {description: Verify the array certificate., type: bool, default: false}
  names: {description: Volume names., type: list, elements: str, required: true}
requirements: [py-pure-client]
"""

EXAMPLES = r"""
- name: Serial of the migration datastore volume
  purefa_volume_info:
    fa_url: 192.0.2.10
    api_token: "{{ vault_flasharray_api_token }}"
    names: ["ocp4::px::mtv-ds01"]
  register: vols
"""

RETURN = r"""
volumes:
  description: Per name, {exists, serial, provisioned}.
  type: dict
  returned: always
"""

from ansible.module_utils.basic import AnsibleModule

try:
    from pypureclient import flasharray
    HAS_PYPURECLIENT = True
except ImportError:  # pragma: no cover
    HAS_PYPURECLIENT = False


def main():
    module = AnsibleModule(
        argument_spec={
            "fa_url": {"type": "str", "required": True},
            "api_token": {"type": "str", "required": True, "no_log": True},
            "validate_certs": {"type": "bool", "default": False},
            "names": {"type": "list", "elements": "str", "required": True},
        },
        supports_check_mode=True,
    )
    if not HAS_PYPURECLIENT:
        module.fail_json(msg="the py-pure-client Python package is required")
    p = module.params
    array = flasharray.Client(target=p["fa_url"], api_token=p["api_token"], verify_ssl=p["validate_certs"],
                              user_agent="ocp-upi purefa_volume_info")
    out = {}
    for name in p["names"]:
        res = array.get_volumes(names=[name], destroyed=False)
        items = list(getattr(res, "items", []) or []) if res.status_code == 200 else []
        v = items[0] if items else None
        out[name] = {"exists": v is not None, "serial": (v.serial or "").lower() if v else "",
                     "provisioned": v.provisioned if v else 0}
    module.exit_json(changed=False, volumes=out)


if __name__ == "__main__":
    main()
