# Lookout

A dashboard that tells you when the things you run have updates and, if you use
Portainer, updates them with one click.

Open `http://<server>:18099` to see everything Lookout watches, what's out of date, and to
add more. Updates are also sent to Discord, ntfy or Gotify.

What it can watch:

- **Containers on this server:** found automatically through the Docker socket.
- **Every Portainer environment:** connect Portainer once and every environment's
  containers are watched, with no setup on those machines (see below).
- **Containers on other machines outside Portainer:** add a *Docker host* once and all its
  containers are watched. The other machine runs a small read-only proxy (see below).
- **Images:** for services Lookout can't see, paste the image they use (for example
  `lscr.io/linuxserver/sonarr:latest`). Lookout remembers what's published now and tells
  you when that changes. Paste a whole list at once.
- **GitHub releases:** for apps that aren't containers, paste repos as `owner/name` or URLs.

For containers and images there are two kinds of notice:

- *New image*: the tag you use (`latest`, `1.25`, ...) now points at a new image.
- *Newer version*: you run `nginx:1.25.3` and `1.27.2` exists. Tags are only compared with
  the same shape, so `1.25.3-alpine` is compared with other `x.y.z-alpine` tags.

Each update is announced once. Containers clear themselves once you pull the new image.
For images and GitHub repos, click **I've updated it** in the dashboard.

Lookout only changes anything when you click **Update** and confirm, and only through
Portainer. Without Portainer it just watches and tells you.

## What's in an update

For each pending update Lookout looks up the release notes between the version you run and
the new one, using the image's own labels (`org.opencontainers.image.source` and
`.version`) and the project's GitHub releases. Most images have them, including all
linuxserver.io images. Lookout then flags:

- **Security**: the notes mention security fixes, CVEs, vulnerabilities, auth bypasses…
- **Breaking**: breaking changes, required migrations or manual steps
- **Major**: the major version goes up (e.g. 2.x → 3.x)

Flagged updates are listed first under **Needs attention**, with the line from the notes
that triggered each flag. They're also first in notifications, and security fixes go out
at high priority on ntfy and Gotify. Click any update to read its release notes.

Flags come from the wording of the notes, so treat them as a pointer, not a guarantee.
Add a GitHub token in Settings: lookups use GitHub's API, which allows 60 requests an hour
without one. Each update is looked up once and remembered.

## Portainer: watch every environment and update with one click

1. In Portainer: **My account → Access tokens → Add access token**. Copy it.
2. In Lookout: gear icon → **Portainer**, enter Portainer's address (e.g.
   `https://192.168.1.10:9443`) and the token, then **Test connection** and **Save**.

Every environment's containers now show up. The **Managed by** column says how each one
is run, which decides whether Lookout can update it:

| Managed by | Meaning | Update button |
|---|---|---|
| **Stack · name** `TOTAL` | A stack created in Portainer (total control) | Redeploys the stack with a fresh pull, like **Update the stack → Re-pull image and redeploy** |
| **Compose · name** `LIMITED` | Started with `docker compose` outside Portainer (limited control) | None: Portainer can't redeploy it. Run `docker compose pull && docker compose up -d` where it lives |
| **Standalone** | A single container, not in a stack | Recreates it with a fresh pull, like **Recreate → Re-pull image** |

Notes:

- Update re-pulls the tag the container already uses. For a *newer version* notice
  (`1.25.3` → `1.27.2`), change the tag in the stack yourself.
- You'll see what will happen and confirm before anything runs. In a stack, other
  services restart only if their image also changed.
- Git stacks are redeployed from their repository with a fresh pull.
- If the Portainer token's user can't manage a stack, the update says so.
- Turn the Update buttons off in Settings to keep Lookout read-only.
- Portainer and its agent are never updated through Portainer: the request would stop the
  process doing it. Update them on the machine itself.
- If Portainer is already deploying a stack, Lookout reports that instead of an error.

### Moving a compose project into Portainer

Open the container's details in Lookout. For a **Limited** project it shows where the
compose file lives and walks through the move. In short: copy the compose file, make
relative paths absolute, `docker compose down` (no `-v`), then create a Portainer stack
with **exactly the same name** so the existing named volumes are reused.

## Install

### Portainer

**Stacks → Add stack → Web editor**, paste [`docker-compose.yml`](docker-compose.yml),
and click **Deploy the stack**. Then open `http://<server>:18099`, click the gear, and add
your Discord webhook. You can also fill in the stack's environment variables instead;
values saved in the dashboard take priority.

### Docker Compose

```bash
curl -fsSLO https://raw.githubusercontent.com/slinky34/lookout/main/docker-compose.yml
docker compose up -d
```

Then open `http://<server>:18099`.

## Watching another machine's containers

On the other machine, deploy this stack (Portainer or `docker compose`). It exposes only
read access to containers and images, on port 2375:

```yaml
services:
  docker-proxy:
    image: ghcr.io/tecnativa/docker-socket-proxy:latest
    container_name: docker-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1
      IMAGES: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "2375:2375"
```

Then in Lookout: **Add → Docker host**, enter its address (e.g. `192.168.1.20:2375`), and
click **Test connection**. Keep port 2375 on your home network; don't forward it to the
internet.

## Settings

Everything is in the dashboard (gear icon): notifications, check interval, GitHub token,
whether to watch every container, newer-version alerts, stopped containers, and error
alerts. **Send test notification** checks your channels.

Environment variables (all optional):

| Variable | Default | |
|---|---|---|
| `LOOKOUT_PORT` | `18099` | Dashboard port. `0` turns the dashboard off |
| `LOOKOUT_PASSWORD` | | Ask for this password before showing the dashboard (any username) |
| `DISCORD_WEBHOOK_URL`, `NTFY_URL`, `NTFY_TOKEN`, `GOTIFY_URL`, `GOTIFY_TOKEN` | | Starting notification settings |
| `GITHUB_REPOS` | | Comma-separated repos to add on first start |
| `GITHUB_TOKEN` | | Raises GitHub's limit from 60 to 5000 checks an hour |
| `PORTAINER_URL`, `PORTAINER_TOKEN` | | Starting Portainer connection |
| `LOOKOUT_INTERVAL` | `6h` | How often to check |

A YAML file at `/config/config.yaml` also works, for logins to private registries and
container exclusion patterns. See [config.example.yaml](config.example.yaml).

### Per-container labels

| Label | Effect |
|---|---|
| `lookout.enable=false` | Don't check this container |
| `lookout.enable=true` | Check it even when "watch every container" is off |
| `lookout.tags=false` | Only check for new images, not newer version tags |

## Command line

```bash
docker exec lookout python -m lookout --test-notify   # send a sample notice
docker exec lookout python -m lookout --dry-run       # check now and print, send nothing
```

## Notes

- Docker Hub: digests are read with `HEAD` requests, which don't count toward Hub's pull
  limit. The newer-version check lists tags; turn it off in Settings if you watch many Hub
  images on a short interval.
- The first time Lookout sees a repo or image, it records the current release or image
  without a notice.
- Data lives in the `lookout-data` volume (`/data/lookout.json`). Upgrading from 0.1
  imports what was already announced, so nothing is sent twice.

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
LOOKOUT_DATA=./data .venv/bin/python -m lookout --config none   # dashboard on :18099
```
