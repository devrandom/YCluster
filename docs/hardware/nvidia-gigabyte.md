# Gigabyte Nvidia GPU Server (nv1+)

The `nv*` compute nodes are Gigabyte chassis with Nvidia GPUs, bootstrapped
via `/bootstrap/nvidia`. Unlike the MS01/VP2440 storage nodes, these boxes
ship with a dedicated AMI MegaRAC BMC.

## BMC Access (in-band)

Sensor data (fans, temperatures, voltages, AC-loss events) is reachable
from the host OS over the in-band KCS channel without depending on the
BMC's dedicated Ethernet port being cabled or configured.

### One-time setup

```bash
sudo apt install ipmitool
sudo modprobe ipmi_si ipmi_devintf ipmi_msghandler
```

The `ipmi_si` driver autodetects KCS via the SMBIOS IPMI table. If
`/dev/ipmi0` doesn't appear, read the base address from SMBIOS and load
explicitly:

```bash
sudo dmidecode --type 38           # look for "Base Address"
sudo modprobe ipmi_si type=kcs ports=0xca2
```

Persist across reboots via `/etc/modules-load.d/ipmi.conf`:

```
ipmi_si
ipmi_devintf
ipmi_msghandler
```

### Everyday commands

| What you want | Command |
| --- | --- |
| Fans, temps, voltages (one line each) | `sudo ipmitool sdr` |
| Same with thresholds | `sudo ipmitool sdr elist` |
| Verbose sensor view | `sudo ipmitool sensor` |
| Board / chassis / serials | `sudo ipmitool fru` |
| Chassis power + LED state | `sudo ipmitool chassis status` |
| System event log (AC loss, thermal trips) | `sudo ipmitool sel list` |
| Clear SEL after review | `sudo ipmitool sel clear` |

`freeipmi-tools` is an alternative frontend over the same kernel path
(`ipmi-sensors`, `ipmi-sel`, `ipmi-fru`) if you prefer it.

### Configuring the dedicated BMC NIC

Once the dedicated BMC Ethernet is cabled, configure it in-band so it's
reachable over LAN:

```bash
# Channel 1 is typically the dedicated BMC NIC on MegaRAC; confirm with:
sudo ipmitool lan print 1

# Set static addressing:
sudo ipmitool lan set 1 ipsrc static
sudo ipmitool lan set 1 ipaddr 10.10.10.X
sudo ipmitool lan set 1 netmask 255.255.255.0
sudo ipmitool lan set 1 defgw ipaddr 10.10.10.1

# Create/enable an admin user:
sudo ipmitool user set name 2 admin
sudo ipmitool user set password 2
sudo ipmitool user priv 2 4 1
sudo ipmitool user enable 2
```

## BIOS Settings

- Restore on AC Power Loss: `Always On`
- PXE boot on the primary NIC (if re-imaging is expected)

## Metrics (optional)

`prometheus-ipmi-exporter` wraps `ipmitool` and exposes readings on
`:9290`, suitable for Prometheus scraping alongside node-exporter if
long-term sensor history is needed.
