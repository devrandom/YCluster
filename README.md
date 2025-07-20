# Overview

Cluster management for small AI clusters.

Uses Ceph, Qdrant, Postgres.

# HOWTO

SSH into cluster machines from the admin host.  Uses the dnsmasq lease file to look up cluster nodes.

```
Host *.xc
  ProxyCommand sh -c 'ip=$(dig %h @10.0.0.1 +short); if [ -n "$ip" ]; then exec nc $ip 22; else echo "Host %h not found in dnsmasq.leases" >&2; exit 1; fi'
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
