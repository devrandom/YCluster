# WireGuard overlay

The cluster runs a WireGuard overlay (`wg0`) so that nodes outside the cluster LAN can participate as first-class cluster members. The tunnel is served by whichever core node currently holds the gateway VIP (`10.0.0.254`); a systemd timer reconciles the interface every 30 seconds and tears it down on non-VIP holders.

Remote nodes live on `10.0.1.0/24`. They get a cluster hostname and IP just like LAN nodes — the IP is computed the same way but offset into the `10.0.1.x` range. All cluster-internal routing to `10.0.1.0/24` goes via the gateway VIP; cluster nodes have this route installed by netplan at boot.

---

## Joining a remote node

### Linux compute or Nvidia node

```bash
curl -sf https://admin.<domain>/bootstrap/wg | sudo bash -s -- --type compute
# or for an Nvidia GPU node:
curl -sf https://admin.<domain>/bootstrap/wg | sudo bash -s -- --type nvidia
```

The script:
1. Generates a WireGuard keypair (or reuses one from a previous run)
2. Allocates a hostname and cluster IP via `/api/wg/register`
3. Prints the assigned hostname and waits (up to one hour) to be approved

Then on any core node:
```bash
ycluster wg approve <hostname>
```

The script writes `/etc/wireguard/wg0.conf`, enables `wg-quick@wg0`, sets the hostname, creates the `admin` user, and installs the Ansible SSH key. After approval the node is reachable from inside the cluster by name (`ssh <hostname>.xc` or just `ssh <hostname>` from a core node).

### macOS node

See [macos.md](macos.md#remote-wireguard-joined-nodes) — the flow is the same but uses the `wg-macos` bootstrap and requires Homebrew preinstalled.

### Dev mode (personal machine, no host changes)

```bash
curl -sf https://admin.<domain>/bootstrap/wg | sudo bash -s -- --dev
```

`--dev` implies `--type dev`, skips hostname/admin-user/SSH-hardening steps, and just brings up the tunnel. The node joins as `d<N>` at `10.0.1.201+`. Useful for a laptop that is already personalised.

---

## Bringing a remote node in-cluster (LAN migration)

When a WireGuard-joined node is physically moved onto the cluster LAN:

1. Disable `wg0` on the node:
   ```bash
   systemctl disable --now wg-quick@wg0
   ```

2. Bring up the cluster network interface and enable DHCP on it.

3. Clear the node's stale WireGuard identity from etcd. `ycluster wg delete`
   removes **both** the wg peer and the node allocation (`by-hostname` +
   `by-mac`) — which is what you want here, because step 4 recreates a fresh LAN
   allocation. Do this **before** re-bootstrapping. Never run `wg delete` on a
   node that is *already* on the LAN: it drops the live allocation (use
   `wg revoke` for that — see [Peer management](#peer-management)).
   ```bash
   ycluster dhcp list all                 # inspect first
   ycluster wg delete <hostname>          # old wg peer + stale 10.0.1.x allocation, e.g. nv1
   ycluster dhcp delete <dynamic-entry>   # e.g. dhcp-209, if the new iface grabbed a dynamic lease
   ```
   (`dhcp delete` alone does **not** touch the wg peer, and a leftover peer will
   black-hole the node once it's back on the LAN — see the troubleshooting note
   below.)

4. Re-run the LAN bootstrap on the node to register the new interface MAC and get the canonical in-cluster IP:
   ```bash
   # For an Nvidia node:
   curl http://admin.xc/bootstrap/nvidia | sudo bash
   # For a compute node:
   curl http://admin.xc/bootstrap/nvidia | sudo bash -s -- --type compute
   ```
   The bootstrap calls `/api/allocate?mac=<MAC>&type=<type>`, which assigns the node its canonical LAN IP (`10.0.0.x`) and hostname.

5. Renew the DHCP lease on the cluster interface so dnsmasq serves the static reservation:
   ```bash
   dhclient -r <iface> && dhclient <iface>
   ```
   Or reboot.

---

## Peer management

```bash
ycluster wg list                    # all peers (hostname, status, type, IP, fingerprint)
ycluster wg list --pending          # pending only
ycluster wg list --approved         # approved only

ycluster wg approve <hostname>      # approve a pending peer (reconciles wg0 immediately)
ycluster wg revoke <hostname>       # remove peer from the wg0 tunnel, KEEP the node allocation
ycluster wg delete <hostname>       # remove peer AND the node allocation (by-hostname + by-mac)

ycluster wg show                    # server config (public key, endpoint, listen port)
ycluster wg render                  # print rendered server wg0.conf
ycluster wg render --client <hostname>  # print what the client's wg0.conf looks like
ycluster wg reconcile               # manually re-sync wg0 from etcd
```

`approve` and `revoke` call `reconcile` automatically. The timer runs reconcile every 30 seconds as a safety net.

**`delete` vs `revoke`.** `delete` cascades — it removes the wg peer *and* the
node's `by-hostname`/`by-mac` allocation (wg register created both together, so
delete tears both down). Use it only when decommissioning a node or wiping a
stale identity before a fresh re-bootstrap. For a node that stays in the cluster
but should stop using wg (e.g. one migrated onto the LAN), use **`revoke`** — it
drops the peer from `wg0` while leaving the node allocation intact. Running
`delete` on an already-LAN node removes it from the cluster (gone from the status
page, loses its DHCP reservation); recover by re-running its LAN bootstrap.

---

## Server initialisation (one-time)

If the WG server has never been set up (new cluster or key rotation):

```bash
ycluster wg init <public-ip-or-hostname>[:port]
# port defaults to 51820

# Rotate keypair (existing peers must re-register):
ycluster wg init <endpoint> --rotate
```

Then run the playbook to install the reconcile timer:
```bash
./run-playbook.sh admin/setup-wg.yml
```

---

## Troubleshooting

**Peer not connecting after approval.** Run `ycluster wg reconcile --up` on the VIP holder to force an immediate sync. Check `wg show wg0` on both sides.

**Wrong node holds wg0.** The reconcile timer checks VIP ownership; a non-VIP holder tears wg0 down. Confirm VIP placement with `ip addr show | grep 10.0.0.254`.

**Node re-registration rejected.** The server rejects a new pubkey for an already-approved peer. If you need to replace the key, revoke the peer first (`ycluster wg revoke <hostname>`), delete `/etc/wireguard/wg0.key` on the client, then re-run the bootstrap.

**Tunnel up but `.xc` names don't resolve.** DNS for remote nodes goes via the cluster VIP over the tunnel. Confirm the resolver is in place:
- Linux: `resolvectl status` — the `wg0` interface should use the cluster VIP as DNS
- macOS: see the DNS section in [macos.md](macos.md#dns-split-dns)

**A LAN node is reachable from most core nodes but not the gateway/VIP holder (stale peer hijacks routing).** Symptom: a node that has been migrated wg→LAN (now on `10.0.0.x`) can be pinged from non-VIP core nodes but not from whichever node currently holds the gateway VIP — so it loses its gateway, NTP, DNS and internet whenever the VIP lands there. Cause: a leftover **approved wg peer** for that node whose `AllowedIPs` followed its allocation to the new **LAN** IP. `wg0` runs only on the VIP holder, so only that node installs the poisoned `10.0.0.x dev wg0` route; it receives the node's packets fine but sends the replies into the (dead) tunnel.

Diagnose on the VIP holder:
```bash
ip route get <node-lan-ip>          # shows "dev wg0" instead of the LAN interface
wg show wg0 allowed-ips             # a 10.0.0.x/32 entry (every legit peer is 10.0.1.x)
ycluster wg list                    # the migrated node still listed, IP now 10.0.0.x
```
A tcpdump on the VIP holder confirms it: requests arrive `In` on the cluster NIC, replies go `Out wg0`.

Fix — use `revoke`, **not** `delete` (the node is already a live LAN member;
`delete` would also remove its allocation, see [Peer management](#peer-management)):
```bash
ycluster wg revoke <hostname>       # drop the peer from wg0, keep the node allocation
ssh <vip-holder> 'ycluster wg reconcile'   # apply now (else the ≤30s timer does it)
# syncconf does NOT remove the wg-quick-installed route; clear it explicitly:
ssh <vip-holder> 'ip route del <node-lan-ip> dev wg0'
```
Prevent it by clearing the wg peer as part of any wg→LAN migration, **before**
re-bootstrapping (see the migration steps above).
