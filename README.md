# Overview

Cluster management for small AI clusters.

Uses Ceph, Qdrant, Postgres.

# Bootstrap

## Initial PXE Setup for s1

On admin host, set up the PXE environment:

```sh
docker compose up --build --profile bootstrap -d
```

PXE boot the first node (s1) and wait for it to come up.  It will install the OS, and then reboot.

## Initial Ansible

Set up admin services on s1 from the admin host:

```sh
docker compose exec ansible ansible-playbook /etc/ansible/setup-admin-services.yml
```

## Switch the Admin Host to normal mode

After the initial setup, switch the admin host to normal mode (providing HTTP proxy and NTP server):

```sh
docker compose down --profile bootstrap
docker compose up --build -d
```

## Further Nodes

For each additional node (s2, s3, etc.), PXE boot the node and wait for it to come up.  Then run on s1 (or other
set-up nodes):

```sh
cd /etc/ansible
ansible-playbook --limit s2 setup-admin-services.yml
```

# Check Cluster Status

## Web Status Dashboard

View the cluster status dashboard at: http://10.0.0.1:12723/status

The status page shows:
- All cluster nodes with their types and IP addresses
- Current leadership assignments (storage and DHCP leaders)
- DNS health status for each node
- Overall health status and individual service status
- Auto-refreshes every 30 seconds

## Command Line Status

On any node, run the following commands to check the status of the cluster:

    check_cluster.py

# initialize the postgres database

```sh
ansible-playbook --tag init_db /etc/ansible/install-postgres.yml /etc/ansible/install-etcd-leader-election.yml
```

## SSH

SSH into cluster machines from the admin host.  Uses the dnsmasq lease file to look up cluster nodes.

```
Host *.xc
  ProxyCommand sh -c 'ip=$(dig %h @10.0.0.1 +short); if [ -n "$ip" ]; then exec nc $ip 22; else echo "Host %h not found in dnsmasq.leases" >&2; exit 1; fi'
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```

## AMT Setup

- set the MEBx password (defaults to admin, but the new password needs 8 chars, digit, special char
- user consent - None
- network setup / FQDN - *a (i.e. append "a" to the hostname)
- network setup / DHCP - enabled 
- network access state - active

Notes:

- on some recent BIOSes, only TLS is supported - port 16993

# Notes

- IP addresses for storage nodes are 10 + node number (e.g., s1 is at 10.0.0.11)
- IP addresses for AMT interfaces are 110 + main IP address (e.g., s1 is at 10.0.0.111)
