"""htpasswd helpers for htpasswd.yml (bcrypt, as OpenShift's HTPasswd identity provider expects).

htpasswd_render(users, existing, exclusive) -> htpasswd file content
  users      list of {name, password}
  existing   current file content ("" if none)
  exclusive  drop users that are not in `users`

A user's existing hash is kept when the password still matches it, so the output only changes when a
password, the user list or the order changes. That keeps reruns idempotent (no OAuth pod rollout).
Needs the `bcrypt` Python package in the Ansible environment.
"""
from ansible.errors import AnsibleFilterError

try:
    import bcrypt
except ImportError:  # pragma: no cover
    bcrypt = None


def _parse(content):
    entries = {}
    for line in (content or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            user, hashed = line.split(":", 1)
            entries[user] = hashed
    return entries


def _matches(password, hashed):
    try:
        # htpasswd writes $2y$; the bcrypt package only knows $2a$/$2b$, which are the same algorithm.
        normalized = hashed.replace("$2y$", "$2b$", 1)
        return bcrypt.checkpw(password.encode(), normalized.encode())
    except ValueError:
        return False


def _htpasswd_bcrypt(hashed):
    """OpenShift's HTPasswd provider only accepts bcrypt labelled $2y$ (as `htpasswd -B` writes it); the bcrypt
    package emits $2b$. Same algorithm, so relabelling keeps the hash valid."""
    for prefix in ("$2b$", "$2a$"):
        if hashed.startswith(prefix):
            return "$2y$" + hashed[len(prefix):]
    return hashed


def htpasswd_render(users, existing="", exclusive=True):
    if bcrypt is None:
        raise AnsibleFilterError("htpasswd_render needs the 'bcrypt' Python package in the Ansible environment")
    current = _parse(existing)
    lines, seen = [], set()
    for u in users:
        name, password = str(u["name"]), str(u["password"])
        if not password:
            raise AnsibleFilterError(f"user {name}: empty password")
        if name in seen:
            raise AnsibleFilterError(f"user {name} is listed twice")
        seen.add(name)
        hashed = current.get(name)
        if not (hashed and _matches(password, hashed)):
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        lines.append(f"{name}:{_htpasswd_bcrypt(hashed)}")
    if not exclusive:
        lines += [f"{name}:{_htpasswd_bcrypt(hashed)}" for name, hashed in current.items() if name not in seen]
    return "\n".join(lines) + "\n"


def _binding_name(*parts):
    name = "-".join(str(p) for p in parts).lower()
    name = "".join(c if c.isalnum() or c in ".-" else "-" for c in name)
    return name[:253].strip("-.")


def htpasswd_bindings(users):
    """Normalize each user's role assignments into binding specs.

    Per user, `roles` is a list whose items are either
      "cluster-reader"                         ClusterRole, cluster-wide (ClusterRoleBinding)
      {role: admin, namespace: team-a}         ClusterRole, only in that namespace (RoleBinding)
      {role: deployer, namespace: team-a, kind: Role}   the namespace's own Role (RoleBinding)
    `cluster_role: X` is shorthand for one cluster-wide "X".
    """
    out, seen = [], set()
    for u in users:
        name = str(u["name"])
        items = list(u.get("roles") or [])
        if u.get("cluster_role"):
            items.append(u["cluster_role"])
        for item in items:
            if isinstance(item, str):
                item = {"role": item}
            if not isinstance(item, dict) or not item.get("role"):
                raise AnsibleFilterError(f"user {name}: each role must be a name or a mapping with 'role'")
            role, ns = str(item["role"]), item.get("namespace")
            kind = item.get("kind", "ClusterRole")
            if kind not in ("ClusterRole", "Role"):
                raise AnsibleFilterError(f"user {name}, role {role}: kind must be ClusterRole or Role")
            if kind == "Role" and not ns:
                raise AnsibleFilterError(f"user {name}, role {role}: a Role needs a namespace")
            if ns:
                spec = {"scope": "namespace", "namespace": str(ns),
                        "name": _binding_name("htpasswd", name, kind, role)}
            else:
                spec = {"scope": "cluster", "namespace": None, "name": _binding_name("htpasswd", name, role)}
            spec.update({"user": name, "role": role, "kind": kind,
                         "key": f"{ns}/{spec['name']}" if ns else spec["name"],
                         "ref": f"Role {ns}/{role}" if kind == "Role" else f"ClusterRole {role}"})
            if spec["key"] in seen:
                raise AnsibleFilterError(f"user {name}: role {role} is assigned twice")
            seen.add(spec["key"])
            out.append(spec)
    return out


def resource_keys(resources):
    """k8s_info resources -> ["namespace/name" or "name"], comparable with htpasswd_bindings()[*].key."""
    keys = []
    for r in resources or []:
        meta = r.get("metadata", {})
        keys.append(f"{meta['namespace']}/{meta['name']}" if meta.get("namespace") else meta["name"])
    return keys


class FilterModule:
    def filters(self):
        return {"htpasswd_render": htpasswd_render, "htpasswd_bindings": htpasswd_bindings,
                "resource_keys": resource_keys}
