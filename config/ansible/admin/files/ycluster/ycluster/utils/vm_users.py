"""User registry and VM access accounting for the SSH bastion."""

from . import vm_manager as vm

def user_get(user):
    return vm._get_json(vm.USERS_PREFIX + user)

def users_all():
    return vm._all_json(vm.USERS_PREFIX)

def validate_ssh_key(key):
    """Validate a single SSH public-key line; return the cleaned string.

    Security-critical: the value is later emitted verbatim into the jump
    bastion's authorized_keys (see _bastion_authorized_keys), so a stray
    newline or control char could split the line and smuggle in an entry
    WITHOUT the restrict/port-forwarding options. We check shape/characters,
    not the algorithm: a single '<type> <base64> [comment]' line of
    printable text, type a plain token, material valid base64."""
    if not isinstance(key, str):
        raise ValueError("That does not look like an SSH public key.")
    key = key.strip()
    if not key:
        raise ValueError("No SSH key provided.")
    if len(key) > vm.MAX_SSH_KEY_LEN:
        raise ValueError("SSH key is too long.")
    # \n \r \t (and other C0 controls) and DEL: an embedded newline is the
    # injection vector; a single line of printable text is all that's valid.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in key):
        raise ValueError("An SSH key must be a single line of printable text.")
    parts = key.split(None, 2)             # type, base64, [comment]
    if len(parts) < 2:
        raise ValueError("That does not look like an SSH public key.")
    if not vm._SSH_KEY_TYPE_RE.match(parts[0]):
        raise ValueError("That does not look like an SSH public key.")
    if len(parts[1]) < 16 or not vm._SSH_KEY_B64_RE.match(parts[1]):
        raise ValueError("That does not look like an SSH public key.")
    return key

def user_add_key(user, key):
    vm._valid_user(user)
    key = validate_ssh_key(key)
    rec = vm.user_get(user) or {"ssh_keys": []}
    if key in rec["ssh_keys"]:
        print(f"Key already registered for '{user}'.")
        return
    if len(rec["ssh_keys"]) >= vm.MAX_SSH_KEYS_PER_USER:
        raise ValueError(
            f"Too many SSH keys ({vm.MAX_SSH_KEYS_PER_USER} max) — remove one first.")
    rec["ssh_keys"].append(key)
    vm._put_json(vm.USERS_PREFIX + user, rec)
    print(f"Added SSH key for '{user}' ({len(rec['ssh_keys'])} total).")

def user_remove_key(user, match):
    rec = vm.user_get(user)
    if not rec:
        raise ValueError(f"No such user: {user}")
    kept = [k for k in rec.get("ssh_keys", []) if match not in k]
    removed = len(rec.get("ssh_keys", [])) - len(kept)
    if kept:
        rec["ssh_keys"] = kept
        vm._put_json(vm.USERS_PREFIX + user, rec)
    else:
        vm._delete(vm.USERS_PREFIX + user)          # no keys left -> drop the user
    print(f"Removed {removed} key(s) from '{user}'.")

def _print_user_keys(user, rec):
    print(f"{user}:")
    for key in rec.get("ssh_keys", []):
        print(f"  {key}")

def _print_user_summary(users):
    if not users:
        return print("No users registered.")
    for name, rec in sorted(users.items()):
        print(f"{name}: {len(rec.get('ssh_keys', []))} key(s)")

def user_list(user=None):
    if user:
        rec = vm.user_get(user)
        if not rec:
            print(f"No such user: {user}")
            return
        _print_user_keys(user, rec)
        return
    _print_user_summary(vm.users_all())

def _vm_access_users(rec):
    """Users who may SSH to a VM: its owner plus any additional users.

    The owner stays the billing/accounting identity; additional_users are
    extra registered users granted the same SSH access. Returns a list
    without duplicates."""
    owner = rec.get("owner")
    users = [owner] if owner else []
    return users + [u for u in rec.get("additional_users") or [] if u and u not in users]

def _access_vms():
    """Return {user: [vm names]} for every user who can SSH to a VM.

    A user appears for each VM they own AND each VM they are an additional
    user on; a VM can map to several users."""
    out = {}
    for name, rec in vm.vms_all().items():
        for u in vm._vm_access_users(rec):
            out.setdefault(u, []).append(name)
    return out

def _vm_access_keys(rec):
    """Union of every registered SSH key of all users with VM access.

    The VM's primary login user accepts the owner's keys *and* each
    additional user's keys, deduped by key material."""
    seen, keys = set(), []
    for u in vm._vm_access_users(rec):
        for k in (vm.user_get(u) or {}).get("ssh_keys", []):
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys
