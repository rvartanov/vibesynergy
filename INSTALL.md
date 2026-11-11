# Installing VibeSynergy

VibeSynergy runs on your own computer or server. Pick one way:

| Your computer | Easiest way |
|---|---|
| Windows | [Container](#a-container-windows-macos-linux) (Docker Desktop) |
| macOS | [Container](#a-container-windows-macos-linux), or [uv](#b-uv-macos-and-linux) |
| Linux, home server, VPS | [uv](#b-uv-macos-and-linux), or the container |

Installing with an AI assistant (Claude Code, Codex, Cursor, …)? Point it at
[For AI assistants](#for-ai-assistants) below.

Status: alpha (0.0.1). Expect changes between versions.

## A. Container (Windows, macOS, Linux)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS),
or Docker / Podman (Linux). The image is `ghcr.io/rvartanov/vibesynergy` for amd64 and arm64
(Apple silicon included).

1. Make a folder, save [compose.yaml](compose.yaml) in it, and put your email address in it at
   `VIBESYNERGY_EMAIL`.
2. Start it:

   ```bash
   docker compose up -d
   ```

3. Get your one-time login link (valid 24 hours):

   ```bash
   docker compose logs vibesynergy
   ```

   It looks like `http://127.0.0.1:8447/login#link=…`. Open it in your browser on the same
   computer, choose a password, and optionally a second step (a code by mail or an
   authenticator app).

Without compose:

```bash
docker run -d --name vibesynergy --restart unless-stopped -e VIBESYNERGY_EMAIL=you@example.com -p 127.0.0.1:8447:8447 -v vibesynergy-data:/data ghcr.io/rvartanov/vibesynergy:latest
```

Everything (settings, encrypted tokens, the database, files) lives in the `vibesynergy-data`
volume. Other commands run inside the container:

```bash
docker exec -it vibesynergy vibesynergy status
docker exec -it vibesynergy vibesynergy login-link   # a new login link
docker exec -it vibesynergy vibesynergy db backup    # a copy of the database in /data/share/backups
```

**Updating**: `docker compose pull && docker compose up -d`. A new image backs up the
database before it applies its changes.

## B. uv (macOS and Linux)

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/).
On Windows, use the container, or this route inside WSL.

```bash
uv tool install git+https://github.com/rvartanov/vibesynergy
vibesynergy init --email you@example.com
vibesynergy serve
```

`init` prints your one-time login link; open it while `serve` runs. The portal is at
http://127.0.0.1:8447.

- Settings: `~/.config/vibesynergy/config.toml`. Data: `~/.local/share/vibesynergy`.
- Keep it running in the background (Linux, systemd): [deploy/README.md](deploy/README.md).
- **Updating**: `vibesynergy update` (backup, update, database changes, restart), or
  **Update now** in Settings → System → About this version.

## C. From source (developers)

```bash
git clone https://github.com/rvartanov/vibesynergy
cd vibesynergy
uv sync
uv run vibesynergy init --email you@example.com
uv run vibesynergy serve
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## After installing

- **Look around first** with synthetic data, no account needed: `vibesynergy init --demo` then
  `vibesynergy sync demo` (container: `VIBESYNERGY_DEMO: "1"` in `compose.yaml` before the
  first start).
- **Your own data**: Settings → Sources: Garmin, Withings, Strava, intervals.icu, calendars, or
  an account export. See [docs/SOURCES.md](docs/SOURCES.md).
- **An AI app**: Settings → Coach apps. See [docs/MCP.md](docs/MCP.md).

## Reaching it from other devices

The portal only answers on the computer it runs on, on purpose. To use it from your phone,
put it behind HTTPS: a reverse proxy such as Caddy, or Tailscale. Then add that name to
`[portal] hosts` in the config. Never publish port 8447 without HTTPS. The login needs a
secure connection (or the computer itself), so it won't work over plain `http://` from
another device. See [deploy/README.md](deploy/README.md).

## Problems

| What you see | What to do |
|---|---|
| The link says it expired or was used | Make a new one: `vibesynergy login-link` (container: `docker exec -it vibesynergy vibesynergy login-link`). |
| Forgot your password | "Forgot your password?" on the login page (needs mail set up), or a new login link as above. |
| Login keeps asking again from another device | You're on plain `http://`: use HTTPS (see above). |
| `serve` refuses to start: old database | `vibesynergy db upgrade`, then start again. |
| Port 8447 already in use | Container: change the left side of `127.0.0.1:8447:8447` (and use that port in the login link too). uv: `[portal] port = 8457` in the config. |
| Something else | The bug button at the top of every page sends a report. |

## Removing it

- Container: `docker compose down`, then `docker volume rm <folder>_vibesynergy-data`
  (this deletes all data).
- uv: `uv tool uninstall vibesynergy`, then delete `~/.config/vibesynergy` and
  `~/.local/share/vibesynergy` if you want the data gone too.

## For AI assistants

You are installing VibeSynergy for a person on their own machine. Follow these steps in
order. Stop and ask when a step says so.

**Rules**

- Ask the person for their email address. Don't guess it.
- The login link is the person's key. Show it to them and let them open it. Don't open it,
  don't set their password, and don't type passwords or tokens for them.
- Keep the portal on `127.0.0.1`. Don't publish it to the network or the internet. HTTPS
  exposure is the person's decision (see "Reaching it from other devices").
- Don't edit `config.toml` with regex or `sed`. Use the portal's Settings, a TOML-aware
  editor, or `vibesynergy` commands.
- Don't connect data sources (Garmin, Strava, …) for them: those logins are theirs, in
  Settings → Sources.

**1. Choose the route.** Windows → container. macOS or Linux with Docker already installed and
the person prefers it → container. Otherwise → uv. Check:

```bash
docker version --format '{{.Server.Version}}'   # a version: Docker works
uv --version                                    # a version: uv works
python3 --version                               # 3.12 or newer for the uv route
```

If neither Docker nor uv is there, ask which one the person wants installed.

**2a. Container.**

```bash
mkdir -p vibesynergy && cd vibesynergy
curl -fsSLO https://raw.githubusercontent.com/rvartanov/vibesynergy/main/compose.yaml
```

Replace `you@example.com` in `compose.yaml` with their address (one line, `VIBESYNERGY_EMAIL`),
then:

```bash
docker compose up -d
docker compose logs vibesynergy | grep "/login#link="
```

Expected: a line `http://127.0.0.1:8447/login#link=…` within a minute. The check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8447/login   # 200
docker inspect -f '{{.State.Health.Status}}' vibesynergy               # healthy (after ~1 min)
```

**2b. uv.**

```bash
uv tool install git+https://github.com/rvartanov/vibesynergy
vibesynergy version                              # 0.0.1a1 or newer
vibesynergy init --email PERSON@EXAMPLE.COM      # prints the login link
```

Start it: on Linux with systemd, as a user service (`deploy/vibesynergy.service`, see
[deploy/README.md](deploy/README.md)): copy it to `~/.config/systemd/user/`, then
`systemctl --user daemon-reload && systemctl --user enable --now vibesynergy`. Otherwise run
`vibesynergy serve` in a terminal that stays open. The check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8447/login   # 200
curl -s http://127.0.0.1:8448/vibesynergy/health                       # {"ok":true,…}
```

**3. Hand over.** Tell the person: open the link on this computer within 24 hours, choose a
password, and optionally set up a second step. Then connect their own accounts in
Settings → Sources (or, to look around first, `vibesynergy init --demo && vibesynergy sync demo`). A new link: `vibesynergy login-link`.

**When something fails:** show the last 30 lines of `docker compose logs vibesynergy` or
`journalctl --user -u vibesynergy -n 30`, and use the Problems table above. Don't delete the
data volume or the data folder to "start over" without the person's yes. It holds their data.
