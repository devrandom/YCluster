# BMC provisioning via ipmitool at bootstrap

## Status

Draft. Direction decided (ipmitool-driven, not DHCP auto-pairing). Not yet
implemented. Open decisions below need to be resolved before code lands.

## Problem

BMCs for managed nodes need a reachable IP so that the BMC web UI / IPMI
commands work, and a DNS name so that operators can find them by hostname
(e.g. `ssh nv1a.xc`-style access, KVM console links in admin pages). Today
this is broken end-to-end:

1. **DHCP registration rejects BMC hostnames.** `dhcp_server.py` explicitly
   drops any DHCP request whose hostname ends in `a` (the BMC marker):
   "Ignoring AMT hostname request ... AMT interfaces use static
   configuration". BMC DHCP requests fall through to the data-subnet
   dynamic range and get a random `dhcp-NNN` lease on `10.0.0.x` instead
   of the management subnet.

2. **The data NIC and BMC end up as unrelated allocations.** A box that
   DHCPs both interfaces appears as two independent `dhcp-NNN` entries in
   etcd, with no link between them. The `amt_ip` field is never written
   by the DHCP path.

3. **No `*.a` DNS records are created for DHCP-registered BMCs.**
   `app.py`'s dnsmasq generator only emits `*.a` A-records for nodes that
   have a non-AMT hostname (e.g. `nv1`). DHCP-allocated `dhcp-NNN`
   hostnames get no BMC sibling.

4. **BMCs that are physically present on the wire have no documented way
   to be configured.** The existing nv* records carry `amt_ip` values
   that are aspirational — set by `/api/allocate` or backfilled by
   `app.py` — but nothing in the repo actually configures the BMC to
   listen at that IP. Whether the BMC is reachable depends on whether
   the operator manually configured it via the vendor's out-of-band tool
   (`racadm`, `hponcfg`, etc.).

The result: BMC reachability and DNS naming is operator-managed, fragile,
and undocumented.

## Why ipmitool, not DHCP auto-pairing

Two candidate architectures were considered:

### A. DHCP auto-pairing

Detect BMCs at DHCP time, pair them with their data NIC via DHCP option
43 (chassis serial), vendor OUI, or DHCP-arrival timing window.

**Failure modes:**

- BMC ships with DHCP disabled (most vendors' default) → no DHCP packet
  to observe
- DHCP option 43 doesn't carry a usable chassis serial (Intel AMT often
  doesn't; some firmware versions strip it)
- Two identical boxes boot simultaneously → ambiguous pairing by OUI
- The BMC and data NIC arrive outside the timing window (e.g. BMC
  powers up hours before the OS) → no pair
- Silent misallocation (BMC gets a random IP, no operator-visible
  error) when pairing fails

### B. ipmitool-driven bootstrap (chosen)

After the data NIC is provisioned (existing `/api/allocate` flow), the
bootstrap explicitly configures the BMC over IPMI: discover → validate
pairing → configure IP → verify → record.

**Properties:**

- Deterministic — every step either succeeds or raises an explicit error
- Vendor-agnostic — same `ipmitool` syntax for Dell iDRAC, HP iLO,
  Supermicro, Intel AMT, generic BMCs
- Works whether or not the BMC is DHCP-speaking (we configure it
  statically)
- Doesn't depend on DHCP option 43 content, chassis serial in the DHCP
  packet, or timing heuristics
- Validation step (cross-check chassis serial from BMC FRU vs data NIC
  DMI) catches mispairing loudly

The trade-off is a per-bootstrap step that requires (a) BMC reachable
from the data NIC, (b) known or default BMC credentials, (c) `ipmitool`
installed on the node. All three are satisfiable with minor additions.

## Flow

Runs once per node, at bootstrap time, immediately after `/api/allocate`
returns the allocation. Lives in the existing per-node-type bootstrap
scripts (`nvidia-bootstrap.sh.j2` and the parallel templates for other
node types).

```
1. /api/allocate?mac=<data-nic-mac>&type=<node-type>
       → returns {hostname, ip, amt_ip, ...}
2. Set hostname, install admin user, set up SSH (existing)
3. BMC discovery            ← NEW
4. BMC-DMI pairing check    ← NEW
5. BMC IP configuration     ← NEW
6. BMC reachability verify  ← NEW
7. Record BMC MAC + chassis serial in etcd  ← NEW (optional)
```

The `amt_ip` value from step 1 is the canonical target. Every later step
is anchored to it.

### Step 3: BMC discovery

Discover the BMC's current reachable endpoint. Three strategies, in
preference order:

1. **Vendor default creds + candidate IPs.** Try the vendor's
   out-of-the-box BMC IP and default credentials. Dell iDRAC defaults
   to `192.168.0.120` (`root` / `calvin`); HP iLO defaults to
   `192.168.1.1` (creds on the tag); Supermicro defaults to
   `192.168.0.120` (`ADMIN` / `ADMIN`). One `ipmitool mc info` per
   candidate; first success wins. Cheap and works on a fresh BMC.

2. **SSDP discovery (Dell iDRAC).** iDRAC broadcasts SSDP on the local
   subnet; a small Python helper using `python-ssdpy` listens for the
   `ipmi` service type and returns the BMC's MAC + current IP. Cleaner
   than ARP scan, vendor-specific.

3. **ARP-scan fallback.** `arp-scan --localnet --oui=<vendor-list>` to
   find BMC-vendor MACs (Dell `9c6b00e*`, HP `00237d*`, Intel AMT
   varies). Noisy but works for any vendor.

If all three fail, log an error and surface to the operator. Do not
silently skip — the BMC will be unreachable forever otherwise.

### Step 4: Pairing validation

Read the chassis serial from two independent sources and compare:

- BMC side: `ipmitool ... fru print` → manufacturer, product, serial
- Data NIC side: `dmidecode -s system-serial-number` (or
  `dmidecode -t 1` for the full System Information record)

If they don't match, the BMC is for a different physical box — abort
loudly. This catches the case where BMC discovery returned the wrong
endpoint (e.g. another node's BMC, a vendor-default BMC from a previous
tenant).

### Step 5: BMC IP configuration

```
ipmitool -H <discovered-bmc-ip> -U <user> -P <pass> lan set 1 ipaddr <amt_ip>
ipmitool -H <discovered-bmc-ip> -U <user> -P <pass> lan set 1 netmask 255.255.255.0
ipmitool -H <discovered-bmc-ip> -U <user> -P <pass> lan set 1 defgw ipaddr <gw>
```

Where `<gw>` is the gateway for the BMC subnet. If we collapse the
management subnet into the data subnet (see "Subnet decision" below),
this is the same `10.0.0.254` as the data NIC. Otherwise it's a
separate gateway on `10.10.10.x`.

Optionally disable DHCP on the BMC (`lan set 1 access off` for some
vendors, or `dhcp set disabled` for Intel AMT) so the BMC stays at this
IP across reboots and vendor firmware updates.

### Step 6: Reachability verify

From the data NIC, `ping <amt_ip>` and `ipmitool -H <amt_ip> -U <user>
-P <pass> mc info`. Both must succeed before the bootstrap proceeds.

### Step 7: Record in etcd

The `amt_ip` field is already in the etcd allocation record (set by
`/api/allocate` in step 1). Optionally extend the record with:

- `bmc_mac`: normalized MAC of the BMC NIC
- `bmc_chassis_serial`: from `fru print`
- `bmc_credentials_vault_ref`: pointer to the per-node BMC password in
  Ansible vault (not the password itself)

These let the admin-api and inventory pages show BMC info alongside the
node, and let re-provisioning find the BMC later without re-doing
discovery.

## Subnet decision

Open: keep `10.10.10.x` as a logical label on the same wire, or collapse
into `10.0.0.x`?

| | Keep `10.10.10.x` | Collapse to `10.0.0.x` |
|---|---|---|
| Existing nv1 etc. BMC configs | unchanged | need netplan rewrite on every BMC-equipped node |
| `amt_ip` field semantics | "BMC IP, possibly on same wire" | redundant (it's just an IP) |
| DNS `*.a` records | useful disambiguation | less useful (same subnet) |
| Future physical separation | already correct addresses | would need re-addressing |

Recommendation: keep `10.10.10.x` for now (no migration churn on
existing nodes), with a single follow-up to remove the field if a later
decision collapses the subnets.

## Credentials

Default vendor credentials are world-known (`root/calvin`, `ADMIN/ADMIN`,
etc.). On first login via `ipmitool`, the bootstrap should:

1. Use the default creds to authenticate (acceptable for a private-LAN
   BMC at bootstrap time).
2. Change the BMC password to a per-node random value.
3. Store the new password in the Ansible vault under a per-node key.
4. Update the `bmc_credentials_vault_ref` field in the etcd record to
   point at it.

Re-provisioning later (e.g. BMC firmware reset) reads the vault ref and
uses the stored password. The window of default-creds exposure is the
bootstrap duration only.

## DNS / etcd integration

No changes needed to `app.py`'s dnsmasq generator (`app.py:611-615`):
once `amt_ip` is in the etcd allocation record (already set by
`/api/allocate`), the generator emits the `{amt_ip} {name}a {name}a.xc`
A-record on the next dnsmasq reload. The reload already triggers on
etcd changes via the existing handler chain.

## Implementation outline

1. **Shared helper module** — `config/ansible/admin/files/scripts/bmc-provision.sh`
   (or a Python equivalent if shell gets unwieldy). Inputs:
   `<amt_ip> <data-nic-mac>`. Steps 3-6 above, exit non-zero on any
   failure with a clear error.
2. **Wire into per-type bootstrap scripts** — call the helper from:
   - `config/ansible/admin/files/templates/nvidia-bootstrap.sh.j2`
   - Parallel templates for compute / storage / macos / nas (look up
     the matching template files)
3. **Add `ipmitool` to apt lists** — `install-ycluster-package.yml`'s apt
   section. Verify it isn't already there; add the package and any
   `dmidecode` deps if missing.
4. **Extend `/api/allocate` response** — add the optional
   `bmc_mac` / `bmc_chassis_serial` / `bmc_credentials_vault_ref` fields
   (defaults: absent). Bootstrap fills them; existing callers ignore.
5. **Vault layout** — `vault/bmc/<hostname>` per node, holds the BMC
   password. `setup-vault.yml` or equivalent creates the entry on
   bootstrap.
6. **Verification path for legacy nodes** — a one-off playbook
   `admin/verify-bmc-config.yml` that runs `ipmitool ... lan print` and
   reports whether each existing nv* BMC's IP matches the `amt_ip` in
   etcd. Mismatches are operator-actionable.

## Failure modes

| Failure | Detection | Recovery |
|---|---|---|
| BMC unreachable from data NIC | `ping <default-bmc-ip>` fails | log error, operator investigates physical cabling / BMC power |
| Default creds don't work | `ipmitool mc info` auth failure | log error, operator supplies real creds |
| Pairing validation fails (chassis serial mismatch) | `fru print` vs `dmidecode` mismatch | log error, abort; operator must manually pair |
| BMC firmware rejects `lan set` command | `ipmitool` non-zero exit | log vendor + error, operator uses vendor tool |
| `amt_ip` already in use on the wire | `ping <amt_ip>` from data NIC returns a different MAC | log error, abort; investigate address collision |

In all cases, the bootstrap fails loudly rather than silently. The
node's data NIC allocation is unaffected (the BMC step is downstream of
`/api/allocate`), so the node still comes up even if BMC provisioning
fails — it just has an unconfigured BMC.

## Out of scope (this doc)

- BMC firmware updates (separate playbook; not bootstrap-coupled)
- BMC user / role management beyond the initial password change
- Redfish API as an alternative to IPMI (future, if a vendor lacks
  IPMI support)
- Per-node BMC monitoring / alerting (existing `ipmi` monitoring could
  be extended later)
- Removing the DHCP rejection at `dhcp_server.py:467-470` (the bug is
  real but this doc's solution makes it irrelevant — bootstrap handles
  BMCs directly, DHCP doesn't need to)

## Open decisions

1. **BMC vendors in this cluster** — Dell only, or mixed? Drives the
   default-creds table in the discovery step.
2. **Existing nv BMC IPs** — are they currently at `10.10.10.<n>` or
   at vendor defaults? Affects whether the legacy verification
   playbook finds matches or mismatches.
3. **Subnet decision** — keep `10.10.10.x` as a label, or collapse?
   See table above.
4. **Credential rotation** — change on first login (recommended), or
   accept default-creds window as an acceptable risk for a private-LAN
   BMC?
5. **Vendor-specific quirks** — known gotchas per vendor (e.g. some HP
   iLO firmware versions require a specific `lan` channel number;
   some Supermicro BMCs need a separate `pef` enable step). Worth
   capturing during the first real run.
