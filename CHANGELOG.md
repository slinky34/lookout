# Changelog

## 0.1.0 - 2026-11-21

First release.

- Checks running containers' images against their registries (Docker Hub, GHCR, lscr.io,
  Quay, private registries with login) and reports new images for the same tag.
- Reports newer version tags with the same shape (`1.25.3` → `1.27.2`, `-alpine` stays `-alpine`).
- Watches GitHub repositories for new releases.
- Notifies through Discord webhooks, ntfy and Gotify; each update is sent once.
- Per-container labels `lookout.enable` and `lookout.tags`; `--dry-run`, `--once`, `--test-notify`.
