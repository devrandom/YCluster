# macOS Compute Node Setup

## Bootstrap

From the macOS system, run:

```bash
curl -sf http://admin.xc/macos/bootstrap | sudo bash
```

This allocates a hostname (m1, m2, etc.) and IP from etcd, creates an `admin` user with SSH key auth, enables Remote Login, and disables sleep.

Prerequisites: macOS connected to cluster network on `en0`, `jq` installed.

## IP Allocation

macOS nodes get IPs in `10.0.0.91-110` (m1=10.0.0.91, m2=10.0.0.92, etc.)

## Troubleshooting

Bootstrap logs: `/var/log/ycluster-bootstrap.log`

The script is idempotent and can be re-run safely.
