# Getting Started

## First Run

- during the first run, Open WebUI will download an embedding model.  This may take a while depending on your internet connection.

## Users

- hit https://yourdomain.ai in your browser
- register the first admin user.  Note that the email address is not verified.
- click on your user icon at the bottom left and select "Admin Panel"
- you can create additional users under the Users tab

## Models

- if you don't yet have inference nodes:
  - add a cloud connection *Settings -> Connections* tabs
  - change some models to public visibility in the *Settings -> Models* tab
- most modern models support "native" tools calls - enable this in "Advanced Params -> Function Calling" under the model configuration.

## Building and Upgrading

The build is performed from a fork (`github.com/devrandom/open-webui.git`) with plugins injected from `github.com/devrandom/open-webui-plugins.git`.

Source is checked out to `/opt/src/open-webui-build/` (or `/opt/src/open-webui-build-stage/` for staging). Once the initial checkout is done, the build playbook does not `git pull` — it's up to you to update the repos manually before rebuilding:

```bash
cd /opt/src/open-webui-build/open-webui
git pull   # or: git fetch && git merge v0.6.x
cd /opt/src/open-webui-build/open-webui-plugins
git pull
```

Then build and push the new image:

```bash
ansible-playbook app/build-open-webui.yml
```

The service on the storage leader uses `--pull=always`, so restarting it picks up the new image. Run the install playbook to restart:

```bash
ansible-playbook app/install-open-webui.yml
```

Or restart the service directly on the storage leader:

```bash
systemctl restart open-webui.service
```

### Staging Instance

A staging instance runs on port 8381 with a separate database, config, and image tag (`:stage`). Use it to test upgrades before applying to production.

The staging source is checked out to `/opt/src/open-webui-build-stage/`. Unlike production, the build playbook does not auto-update the repos either — update them manually before building.

Deploy staging (install only, uses existing image):

```bash
ansible-playbook app/open-webui-stage.yml
```

Build and deploy staging:

```bash
ansible-playbook app/open-webui-stage.yml --tags all,build
```

Restart staging on the leader:

```bash
systemctl restart open-webui-stage.service
```

## Troubleshooting

To enable DEBUG logging, create a systemd override on the storage leader:

```bash
systemctl edit open-webui.service
```

Add:

```ini
[Service]
Environment=GLOBAL_LOG_LEVEL=DEBUG
```

Then restart:

```bash
systemctl restart open-webui.service
```

Remove the override to go back to INFO:

```bash
systemctl revert open-webui.service
systemctl restart open-webui.service
```

## Storage

Open WebUI uses a database for user data.  It also uses a cache in `/rbd/misc/app/open-webui-data`

TODO - use qdrant as a vector database intead of chroma.
