# Drop the bcrypt hasher from authentik

The 2026-06-11 Open-WebUI → authentik password migration
(`ycluster user import-owui`) copied plain-bcrypt hashes, verified via
the extra `BCryptPasswordHasher` in
`config/ansible/app/files/authentik-user-settings.py`. Django re-hashes
each user to PBKDF2 on their first successful authentik login, so the
imported hashes disappear over time.

Periodically check
`select count(*) from authentik_core_user where password like 'bcrypt$%'`
on the leader; when it reaches 0, remove the user_settings.py override
(or trim it back to the stock list), the compose mounts, and the playbook
task.

Removing it earlier strands not-yet-logged-in users on an unverifiable
hash (recoverable via reset link). Convergence here is also a good
readiness signal for the OWUI `ENABLE_LOGIN_FORM=false` flip.
