# PDU management

APC Switched Rack PDUs (NMC2 cards, e.g. AP8858EU3) are managed via SSH
CLI and SNMPv3. Static IPs are allocated from 10.0.0.141–145
(`pdu1.xc`–`pdu5.xc`), outside the DHCP pool.

## Credentials

All credentials live in etcd under `/cluster/config/pdu/`:

| Key | Purpose |
|---|---|
| `admin-password` | NMC login (SSH + web), user `apc` |
| `snmpv3-auth` | SNMPv3 auth passphrase (SHA) |
| `snmpv3-priv` | SNMPv3 privacy passphrase (AES-128) |

Read them from the bastion with:
```bash
python3 -c "
from ycluster.common.etcd_utils import get_etcd_client
client = get_etcd_client()
print(client.get('/cluster/config/pdu/admin-password')[0].decode())
"
```

The default NMC credentials are `apc`/`apc`; first SSH login forces a
password change.

## Adding a new PDU

The PDU must be reachable from the bastion (typically on the DHCP
address it booted with). Then run the two playbooks from the bastion:

```bash
# 1. Allocate a slot (pdu1, pdu2, ...), register MAC/IP in etcd,
#    set the static IP, and reboot the card.
cd /etc/ansible && ./run-playbook.sh admin/setup-pdu-ip.yml \
  -e pdu_current_ip=10.0.0.211

# 2. Enable SNMPv3 and verify.
./run-playbook.sh admin/setup-pdu.yml -e pdu=1
```

Both playbooks are idempotent. `setup-pdu-ip.yml` matches on the PDU's
MAC address (from ARP), so re-running it against an already-registered
PDU is a no-op. If the PDU is already at its static IP, pass that IP as
`pdu_current_ip` and the tcpip/reboot step is skipped.

Requires SSH access from the bastion — the playbooks pipe commands into
the NMC's CLI via `sshpass`. The NMC drops the session mid-reboot; both
playbooks treat only that exit as acceptable and fail on anything else.

## Reading values with SNMP

`ycluster snmp` wraps pysnmp (pure Python, no binary dependency) and
resolves `pdu{N}` hostnames via etcd:

```bash
ycluster snmp list                          # registered devices
ycluster snmp get pdu1 .1.3.6.1.2.1.1.1.0   # sysDescr
ycluster snmp walk pdu1 .1.3.6.1.2.1.1      # system subtree
```

Raw `snmpget`/`snmpwalk` with credentials from etcd:

```bash
AUTH=$(python3 -c "
from ycluster.common.etcd_utils import get_etcd_client
print(get_etcd_client().get('/cluster/config/pdu/snmpv3-auth')[0].decode())")
PRIV=$(python3 -c "
from ycluster.common.etcd_utils import get_etcd_client
print(get_etcd_client().get('/cluster/config/pdu/snmpv3-priv')[0].decode())")
snmpget -v3 -l authPriv -u ycluster -a SHA -A "$AUTH" -x AES -X "$PRIV" \
  pdu1.xc .1.3.6.1.2.1.1.1.0
```

## Useful OIDs (APC PowerNet MIB, enterprise 318)

The card is a Switched Rack PDU 2G, so the interesting subtrees are
`318.1.1.12` (switched outlets) and `318.1.1.26` (metered device
status). Values observed on pdu1:

| OID subtree | Meaning |
|---|---|
| `318.1.1.12.1.5.0` | Model (e.g. AP8858EU3) |
| `318.1.1.12.1.6.0` | Serial number |
| `318.1.1.12.1.7.0` | Outlet count |
| `318.1.1.26.6.3.1.6.1` | Input voltage (V) |
| `318.1.1.26.6.3.1.7.1` | Bank current, tenths of A |
| `318.1.1.26.11.3.0` | Ambient temperature (°C) |
| `318.1.1.26.11.2.0` | Humidity (%) |
| `318.1.1.12.2.2.1.x.<outlet>` | Per-outlet status table |
| `318.1.1.12.2.3.1.1.x.<outlet>` | Per-outlet control (SET) |

Outlet control uses the PowerNet "OutletControl" table — the command
column accepts 1=on, 2=off, 3=reboot, plus delayed variants. **Verify
against the PowerNet MIB before scripting SETs**: writing the wrong
column on this MIB family is interpreted as a command.

## Gotchas

- **NMC reboots are required** for both IP changes and SNMPv3 changes.
  The CLI returns `E002: Success` immediately but the card keeps
  running on the old config until `reboot -Y`. Both playbooks now send
  this automatically; manual changes need it too.
- **The CLI is slow to prompt.** Sleep-based pipes (5s before each
  command, 3s between) are the minimum that works reliably.
- **SSH does not support public keys** — password auth only, so
  automation goes through `sshpass` with the etcd-stored password.
- **SNMPv1 and SNMPv3 cannot both be disabled** in some firmware — if
  you disable v1, verify v3 still responds afterwards.
- **apt on s2/s3 has hung on proxy CLOSE-WAIT connections** after a
  Squid restart, holding the apt lock for days. Check for
  `apt-get -qq -y update` in `ps` if a playbook reports
  `/var/lib/apt/lists/lock` is held.
