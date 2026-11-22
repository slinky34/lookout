# Changelog

## 0.3.0 - 2026-11-22

Portainer support.

- Connect Portainer (address + access token) in Settings to watch the containers in every
  Portainer environment, with nothing to install on those machines. An environment that's
  the same Docker as this server isn't listed twice.
- New **Managed by** column: **Stack · name** (Portainer, total control), **Compose · name**
  (started outside Portainer, limited control) or **Standalone**.
- **Update** button for what Portainer can update: redeploys a stack with a fresh pull
  (editor and Git stacks), or recreates a standalone container with a fresh pull. Shows
  exactly what will happen and asks first. Can be turned off in Settings.
- If an update includes Lookout's own container, Lookout answers first and then restarts.
- `PORTAINER_URL` and `PORTAINER_TOKEN` environment variables.

## 0.2.0 - 2026-11-22

Adds a dashboard on port 18099.

- See everything Lookout watches, what's out of date and what failed, on one page.
- Add GitHub repos and images from the dashboard, a whole list at a time.
- Watch other machines' containers by adding a Docker host (through a read-only socket proxy).
- Track images for services Lookout can't see, and mark them updated when you've updated.
- Set notifications, the check interval and the GitHub token in the dashboard, and send a test notice.
- "Check now" for everything or for one host.
- Optional password (`LOOKOUT_PASSWORD`).
- Data moves to `/data/lookout.json`; what 0.1 already announced is imported, so nothing is sent twice.
- Clearer messages when Docker or a host can't be reached.


## 0.1.0 - 2026-11-21

First release.

- Checks running containers' images against their registries (Docker Hub, GHCR, lscr.io,
  Quay, private registries with login) and reports new images for the same tag.
- Reports newer version tags with the same shape (`1.25.3` → `1.27.2`, `-alpine` stays `-alpine`).
- Watches GitHub repositories for new releases.
- Notifies through Discord webhooks, ntfy and Gotify; each update is sent once.
- Per-container labels `lookout.enable` and `lookout.tags`; `--dry-run`, `--once`, `--test-notify`.
