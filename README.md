# Lookout

A dashboard that tells you when the things you run have updates. It doesn't install them.

Open `http://<server>:18099` to see everything Lookout watches, what's out of date, and to
add more. Updates are also sent to Discord, ntfy or Gotify.

What it can watch:

- **Containers on this server:** found automatically through the Docker socket.
- **Containers on other machines:** add a *Docker host* once and all its containers are
  watched. The other machine runs a small read-only proxy (see below).
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

Lookout never pulls images or restarts containers. The Docker socket is mounted read-only.

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
