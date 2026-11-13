# Notifications

Alerts leave the cluster through Alertmanager → `ntfy-webhook`
(`config/ansible/monitoring/files/ntfy-webhook.py`, `:9095`) → a ntfy.sh
topic. Delivery is split by alert class and by host, so one stream does not
have to carry everything:

1. An `IDSAlert` goes to the IDS topic — findings, coverage, and the IDS
   budget cap notices. This wins over every host route, so a security event
   never hides in a quieter stream.
2. A host with a direct **host → topic** entry uses that topic.
3. A host listed in a **host → location → topic** entry uses the location's
   topic.
4. Everything else goes to the main topic.

Hosts are matched against *any* label value on the alert, in bare hostname,
FQDN, `host:port` or URL form: `lab1`, `lab1.xc`, `lab1:9100` and
`https://lab1.xc:9218` all match the entry `lab1`.

## Where the topics live

Topics are bearer secrets: whoever knows one can publish, and whoever
subscribes receives everything sent to it. They are **not** in this repo.
They live in the private sibling repo and are linked in:

```
ycluster-private/group_vars/all/ntfy.yml
  ← ycluster/config/ansible/group_vars/all/ntfy.yml (symlink, gitignored)
```

`dev-sync` dereferences the symlink (`rsync --copy-unsafe-links`), so the file
lands on the cluster as a real file and Ansible auto-loads it for every host.
`monitoring/install-prometheus.yml` renders it into `/etc/ycluster/ntfy.env`
(mode 0640, root) and the unit reads it via `EnvironmentFile=`.

The file has this shape:

```yaml
ntfy_topic_main: "<topic for everything unrouted>"
ntfy_topic_ids: "<topic for IDSAlert>"

# Hosts that share a topic. Add one entry per place or role you want
# separated; hosts listed nowhere stay on the main topic.
ntfy_locations:
  <location>:
    topic: "<topic>"
    hosts: [<host>, ...]

# Optional single-host override; takes precedence over ntfy_locations.
ntfy_host_topics:
  <host>: "<topic>"
```

The play asserts that the two base topics are set and that every location has
a `topic` and a non-empty `hosts` list, so a deployment cannot come up with a
half-configured router. A malformed `NTFY_LOCATIONS` value at runtime is
logged and ignored, and such alerts fall back to the main topic.

Hosts that are not in the cluster inventory (a firewall host, a NAS watched
from the admin repo) are notified by the admin repo directly; point those
senders at the same topic that the matching location uses.

## Rotating a topic

1. Update the topic value in `ycluster-private/group_vars/all/ntfy.yml`.
2. Re-run `./run-playbook.sh monitoring/install-prometheus.yml --limit core`.
   The env file and unit change, and the handler restarts `ntfy-webhook`.
3. Subscribe the phone to the new topic before the old one stops receiving.
   The old topic is not deleted; it simply stops getting traffic.
4. Update any non-cluster sender pointed at the old value (see the admin
   repo's notifier variables) and re-run its playbook.

## Verifying

```bash
systemctl cat ntfy-webhook | grep EnvironmentFile   # unit wiring
journalctl -u ntfy-webhook -n 50                    # "Sent notification to ntfy: ..."
```

The topic itself is never logged (it is a bearer secret). An alert with no
configured topic anywhere is dropped, logged as an error, and answered with
503 so Alertmanager keeps retrying.
