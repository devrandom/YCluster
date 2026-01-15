# macOS Compute Node Setup

## Initial Install

Perform initial setup on the Mac, erasing any old data if present.

When prompted, create a user `admin` used for first log-in.  Skip all optional software configuration, apple account sign-in, sending of any info to Apple, etc.

For software updates, choose "Only download automatically".

## Bootstrap

Log-in to the UI.  Grant "Full Disk Access" to `Terminal`: **System Settings → Privacy & Security → Full Disk Access**.

Run the bootstrap script:

```bash
curl -sf http://admin.xc/macos/bootstrap | sudo bash
```

This allocates a hostname (m1, m2, etc.) and IP from etcd, creates an `admin` user with SSH key auth, enables Remote Login, and disables sleep.

Prerequisites: macOS connected to cluster network on `en0`, `jq` installed.

## IP Allocation

macOS nodes get IPs in `10.0.0.91-110` (m1=10.0.0.91, m2=10.0.0.92, etc.)

## Run Ansible and Reboot

Run Ansible on the new node.  Afterward, you may have to reboot before the services (launch configurations) run properly. 

## Troubleshooting

Bootstrap logs: `/var/log/ycluster-bootstrap.log`

The script is idempotent and can be re-run safely.
