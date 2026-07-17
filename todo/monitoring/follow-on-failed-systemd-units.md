# Follow-on: failed-systemd-units

The failed-systemd-units alert (`SystemdUnitFailed`, see `CHANGELOG.md`)
partly subsumes the per-service liveness and ansible-run-success items
above. Follow-on: an autossh tunnel unit it caught was fixed in place
but isn't repo-managed — consider ansible-izing it.
