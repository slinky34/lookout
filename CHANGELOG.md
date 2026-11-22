# Changelog

## 0.4.1 - 2026-11-22

- **Smaller "Needs attention" cards.** Each card is now three short lines: name and flags,
  where it runs and the version change, then the actions. The reason it was flagged is
  hidden behind **Why flagged**; click it to expand, or use **Expand all**.

## 0.4.0 - 2026-11-22

New dashboard layout, and release notes for every update.

- **Release notes and importance.** Each pending update shows the release notes between the
  version you run and the new one, found through the image's source labels and GitHub.
  Updates with **security fixes**, **breaking changes** or a **major version** jump are
  flagged, listed first under "Needs attention" with the line that triggered the flag,
  and sent first (and at high priority for security) in notifications.
- **Redesigned dashboard.** A sidebar with Overview, Problems, each environment with its
  update count, and the watch lists. Click any update for a details panel with versions,
  flags, release notes, how it's managed and the Update button. Filters per environment,
  search (press `/`), and a phone layout with a menu.
- **Managed by** stays visible on medium screens, and the compose file's location is shown
  for compose projects, with steps to move a Limited project into Portainer.
- When a newer version tag exists, the button says **Re-pull :tag** so it's clear it doesn't
  change the tag.
- Portainer and its agent no longer get an Update button (Portainer can't update itself).
- "Stack deployment is already in progress" from Portainer is reported as already updating,
  not as a failure.
- The page loads version-stamped scripts, so a browser never mixes old and new files after
  an upgrade.

## 0.3.2 - 2026-11-22

- New **Problems** list near the top: each problem says where it is, what went wrong and
  what to do, with **Check again** (and **Remove** for watched images and repos).
- The **Problems** and **Updates available** tiles jump to their lists.
- A Portainer connection error now counts as a problem.
- "Updates only" filter renamed to "Updates & problems only", which is what it shows.

## 0.3.1 - 2026-11-22

- Saving Portainer or container settings now checks straight away, so Portainer's
  environments appear without waiting for the next scheduled check.
- An environment Portainer can't read (down, or Portainer can't list it) now shows with
  the reason instead of being left out.
- **Test connection** lists each environment with its container count or its problem.

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
