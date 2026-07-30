# Move NTFY_TOPIC out of repo

`config/ansible/monitoring/files/ntfy-webhook.py` has a hardcoded
deployment-specific UUID at line 18 (`NTFY_TOPIC = "..."`). This leaks a
production value into the repo.

Move to env var / vault / systemd `EnvironmentFile` — whichever fits the
existing secret-handling pattern in this codebase. Verify the alertmanager
receiver still works after the change.
