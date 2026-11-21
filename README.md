# Lookout

Tells you when the things you run have updates. It doesn't install them.

- **Docker containers:** checks each running container's image against its registry
  (Docker Hub, GHCR, lscr.io, Quay, or your own). Two kinds of notice:
  - *Image updated*: the tag you use (`latest`, `1.25`, ...) now points at a new image.
  - *New version tag*: you run `nginx:1.25.3` and `1.27.2` exists. It only compares tags
    with the same shape, so `1.25.3-alpine` is compared with other `x.y.z-alpine` tags.
- **GitHub releases:** watches repos you list (for apps that aren't containers).
- **Notifications:** Discord webhook, ntfy, Gotify. Any mix.

Each update is announced once. When you pull it, Lookout forgets it, so the next one is
announced too. Images you built yourself (like `trove:latest`) are skipped because there's
no registry copy to compare with.

It never pulls images or restarts containers. The Docker socket is mounted read-only.

## Set up

The image is `ghcr.io/slinky34/lookout` (amd64 and arm64). Grab `docker-compose.yml`,
`.env.example` and `config.example.yaml` from this repo, then:

```bash
cp .env.example .env                        # add a Discord webhook and/or ntfy / Gotify
cp config.example.yaml config/config.yaml   # optional: GitHub repos, schedule, exclusions
docker compose up -d
docker compose run --rm lookout --test-notify
```

The last line sends a sample notice to every configured channel.

Without a config file, the `.env` alone works: `DISCORD_WEBHOOK_URL`, `NTFY_URL`,
`NTFY_TOKEN`, `GOTIFY_URL`, `GOTIFY_TOKEN`, `GITHUB_TOKEN`, `GITHUB_REPOS` (comma separated),
`LOOKOUT_INTERVAL` (default `6h`).

**Discord:** channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.
**ntfy:** any topic URL, e.g. `https://ntfy.sh/some-long-random-name`, then subscribe to it
in the ntfy app. **Gotify:** create an application and use its token.

## Per-container labels

| Label | Effect |
|---|---|
| `lookout.enable=false` | Don't check this container |
| `lookout.enable=true` | Check it even when `watch_by_default: false` |
| `lookout.tags=false` | Only check for new images, not newer version tags |

## Commands

```bash
docker compose run --rm lookout --dry-run       # check now, print results, send nothing
docker compose run --rm lookout --once          # check now and notify
docker compose logs -f lookout
```

## Notes

- Docker Hub: Lookout uses `HEAD` requests for digests, which don't count toward Hub's
  pull limit. The newer-tag check lists tags, so turn it off (`check_new_tags: false`) if
  you watch many Hub images on a short interval.
- GitHub allows 60 unauthenticated checks an hour. A token with no scopes raises that
  to 5000.
- The first time a GitHub repo is seen, its current release is recorded without a notice.
- State lives in the `lookout-data` volume (`/data/state.json`). Delete it to be
  re-notified about everything pending.

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```
