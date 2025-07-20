# Overview

Cluster management for small AI clusters.

Uses Ceph, Qdrant, Postgres.

# Getting Started

```
make
docker compose up --build -d
```

# HOWTO

## Ansible

```sh
docker compose exec ansible ansible-inventory
docker compose exec ansible ansible-playbook /etc/ansible/site.yml

# check etcd health
docker compose exec ansible ansible storage -m shell -a "etcdctl endpoint health"
docker compose exec ansible ansible storage -m shell -a "etcdctl member list"

# check ceph health
docker compose exec ansible ansible storage -m shell -a "microceph status"
docker compose exec ansible ansible storage -m shell -a "microceph cluster list"

# initialize the postgres database
docker compose exec ansible ansible-playbook --tag init_db /etc/ansible/install-postgres.yml /etc/ansible/install-etcd-leader-election.yml
```

## SSH

SSH into cluster machines from the admin host.  Uses the dnsmasq lease file to look up cluster nodes.

```
Host *.xc
  ProxyCommand sh -c 'ip=$(dig %h @10.0.0.1 +short); if [ -n "$ip" ]; then exec nc $ip 22; else echo "Host %h not found in dnsmasq.leases" >&2; exit 1; fi'
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
