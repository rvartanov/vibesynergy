# Running VibeSynergy as a service

One athlete per installation, on a machine that is always on (a home server or a small VPS).

## 1. Install or update

```bash
uv tool install git+https://github.com/rvartanov/vibesynergy   # update: uv tool upgrade vibesynergy
vibesynergy init          # first time: config + database
vibesynergy db upgrade    # after an update, before the restart: applies migrations
```

Before an update, make an online backup of the database while it runs (never copy the
file while it is being written):

```bash
sqlite3 ~/.local/share/vibesynergy/db/vibesynergy.sqlite ".backup '/path/to/backup.sqlite'"
```

## 2. Configure `~/.config/vibesynergy/config.toml`

```toml
[user]
timezone = "Europe/Amsterdam"

[server]
public_url = "https://server.your-tailnet.ts.net"     # the Funnel address, no path

[sources.garmin]
enabled = true

[sources.withings]
enabled = true

[sources.strava]
enabled = true

[schedule]            # minutes between automatic syncs (0 = off); also on Data -> Sources
garmin = 15
withings = 60
strava = 60
"garmin.scheduled_workouts" = 15   # one dataset more often: the training calendar
dropfolder = 5        # the drop folder check
alerts = 15           # the alert check
```

## 3. Run the service

```bash
cp deploy/vibesynergy.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now vibesynergy
loginctl enable-linger "$USER"
curl -s http://127.0.0.1:8448/vibesynergy/health
```

The service listens on 127.0.0.1:8448 only. It runs the webhook receivers, the job worker
and the scheduler.

**Update now** (Settings → System → About this version) starts the user unit
`vibesynergy-autodeploy.service` (the same unit the switched-off `vibesynergy-autodeploy.timer`
would start), so the deploy runs outside the service and survives its restart. It asks for
the coach passphrase, is audited (`portal.update.start`), and is refused while a job runs or
when there is nothing new. Without that unit the button is not shown.

Finished jobs stay in the queue. `uv run vibesynergy jobs prune` counts the done and cancelled
jobs older than 30 days (`--days`, `--dead` to include failed ones); `--apply` removes
them. It always keeps the newest finished job of each kind (sync status reads it) and the
history import jobs.

## 4. Expose only `/vibesynergy` through the Funnel

Expose only the `/vibesynergy` path, so nothing else on the machine is reachable. If the
Funnel already serves something on port 443, this adds a path next to it:

```bash
sudo tailscale funnel --bg --set-path /vibesynergy http://127.0.0.1:8448
tailscale funnel status      # /vibesynergy must be listed
curl -s https://server.your-tailnet.ts.net/vibesynergy/health
```

The service answers with or without the `/vibesynergy` prefix, so it does not matter whether
Tailscale strips it.

## 5. Log in and subscribe to instant updates

The developer apps must point at this machine:
- Withings app, callback URL: `https://server.your-tailnet.ts.net/vibesynergy/withings/callback`
- Strava app, Authorization Callback Domain: `server.your-tailnet.ts.net`


```bash
uv run vibesynergy auth garmin
uv run vibesynergy auth withings
uv run vibesynergy auth strava
uv run vibesynergy webhooks subscribe withings    # needs step 4 working
uv run vibesynergy webhooks subscribe strava
```

## 6. Connect Claude (or another MCP client)

Open the portal and go to **Settings → Coach apps**. Set the passphrase there (at least 12
characters; the page only shows whether one is set). Copy the coach address, which is
`https://server.your-tailnet.ts.net/vibesynergy/mcp` when the public address is set.

Add that address as a custom connector in Claude (Settings, Connectors) and click Connect.
A VibeSynergy page opens that names the app and lists each permission. Choose what it may do,
type the passphrase and click Allow. Each app is approved once and keeps access through its
own tokens. Personal tokens (name, permissions, 30/90/365 days) are created on the same
page and shown once.

- Coach apps → Activity lists what each app did. Revoke there, or with
  `uv run vibesynergy mcp revoke <client id>`.
- After 5 wrong passphrases in 15 minutes for one app, that app's approvals are blocked
  for the rest of those 15 minutes; other apps are not. Above 50 wrong passphrases
  across all apps in 15 minutes every approval waits. Creating a personal token in
  Settings needs the passphrase too, under the same rule. Every approval and failure
  is in the audit log.

**The old secret link** is gone: `/vibesynergy/<secret>/mcp` answers 404 and a leftover
`[server] mcp_secret_url` line is ignored (one warning in the log; delete the line when
convenient). `secrets/mcp/access.json` and `secrets/mcp-clients/` are no longer read.

For local clients on the server itself, use stdio: `uv run vibesynergy mcp`. That session is the
athlete, not a coach app.

## 7. Web portal (tailnet only)

`vibesynergy serve` also runs the portal on 127.0.0.1:8447. Publish that port on the tailnet
only; this adds port 8447 and leaves the Funnel on 443 (Garmin connector and `/vibesynergy`)
as it is:

```bash
sudo tailscale serve --bg --https=8447 http://127.0.0.1:8447
tailscale serve status       # 8447 listed as tailnet only
tailscale funnel status      # unchanged: 443 still has the connector and /vibesynergy
```

Open https://server.your-tailnet.ts.net:8447 from any device on the tailnet. Settings, all
optional:

```toml
[portal]
port = 8447          # local port (must differ from the service port 8448)
enabled = true
hosts = []           # extra host names to accept besides localhost and the tailnet name
tiles = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"   # map background (tile server)
tile_cache = true    # serve map tiles from the server's own cache (see below)
tile_max_age_days = 30
```

**Map tiles are cached on the server.** The browser gets map tiles from the portal
(`/tiles/{z}/{x}/{y}.png`), never from OpenStreetMap directly. The portal fetches a tile
once, keeps it under `~/.local/share/vibesynergy/tiles/osm/{z}/{x}/{y}.png`, refreshes it after
`tile_max_age_days`, and keeps serving the old copy when OpenStreetMap cannot be reached, so
every map viewed once also works offline. `uv run vibesynergy tiles status` shows the folder and
its size.

To have new activities' maps offline before you open them, cache their routes ahead:

```bash
uv run vibesynergy tiles prefetch --period last_7d     # the route tiles of last week's activities
```

It fetches only the tiles an activity page opens with plus those along the route (tens per
activity), one at a time with a pause, and at most `--limit` (2000) per run. OpenStreetMap's
tile policy forbids bulk downloading, so keep periods short; a daily run for `last_7d` (for
example from a systemd timer) is fine, a whole history is not.

Other tools can use the cache: as an XYZ tile layer from any tailnet device
(`https://server.your-tailnet.ts.net:8447/tiles/{z}/{x}/{y}.png`, e.g. QGIS → XYZ Tiles), or
directly from the folder on the server (`file:///home/<you>/.local/share/vibesynergy/tiles/osm/{z}/{x}/{y}.png`).
Show "© OpenStreetMap contributors" wherever the tiles are displayed.

Never run `tailscale funnel` for port 8447: the portal has no login of its own; the tailnet
is its access control.

## 8. History

Connect your sources (above), then fill in their past on **Data & sync → History**. Per source
there are two routes, with an estimate and a recommendation:

- **From the account export** (recommended when there's a lot of history): request it once,
  then importing takes minutes and hits no API limits. Upload the zip on the History card (an
  interrupted upload continues where it stopped), or put it in the drop folder
  (`~/.local/share/vibesynergy/inbox`): it's recognised, imported in the background, then moved to
  `imported/`.
  - Garmin: garmin.com → Account → Data Management → Export Your Data (a zip by email).
  - Strava: Settings → My Account → Download or delete your account → Request your archive.
- **Over the API** (fine for a year or two, or a source without an export such as Withings):
  the service fetches month by month in the background, after the regular syncs. A restart
  continues where it stopped, a rate limit (Strava's daily budget) waits until it resets,
  months where some datasets failed are fetched once more at the end, and after 6 failed tries
  in a row the run stops as stuck and sends an alert. Pause, resume or cancel on the card.
  After `vibesynergy auth <source>` the command line offers to start it too.

Both can be combined: the export for the bulk, the regular syncs for everything after it.
Importing twice, or an export after an API fill, is harmless: the same files and activities
are recognised.

The same from the command line:

```bash
uv run vibesynergy backfill garmin --start 2016-01-01 --background   # the service fills it
uv run vibesynergy backfill garmin --start 2016-01-01                # or here, in the terminal
uv run vibesynergy import garmin-export ~/Downloads/<export>.zip
uv run vibesynergy import strava-export ~/Downloads/export_<id>.zip
```

`vibesynergy status` lists the runs too. Logs: `journalctl --user -u vibesynergy -f`.

## 9. Alerts by email (optional)

Calendar (iCal / CalDAV, any provider incl. iCloud and Google): in the portal, **Settings →
Calendar**: sign in, choose calendars, save. Nothing is entered in the shell; see
[docs/SOURCES.md](../docs/SOURCES.md#ical--caldav-calendars-caldav-opt-in-read-only). Deploying this
adds the `cryptography` dependency (`uv sync`); credentials are stored encrypted with a key in
`<config dir>/keys/` (back it up together with `secrets/`, or sign in again after a restore).

Alerts: in the portal, **Settings → Alerts**. Enter the address to alert, open **Mail
settings** (any provider: SMTP server, port, STARTTLS/SSL, username, an app-specific
password; optionally IMAP to keep a copy in Sent), press **Send test alert**, and restart
the service when the notice asks. The WhatsApp channel and its bridge were removed on
31-10-2026. An old `[alerts] whatsapp_phone` is ignored with a warning.

Push notifications: **Settings → Alerts → Push notifications → Turn on for this
device** (on an iPhone from the home-screen app, iOS 16.4+). Alerts then also arrive as a
notification; per kind of alert the Push column chooses which. VibeSynergy sends them itself
(Web Push, RFC 8291/8292): the message is encrypted for that one browser, so Apple's or
Google's push service only carries ciphertext. VibeSynergy's signing key is made on first use
and kept in the credential store (`secrets/push`). No extra package or account is needed;
devices that are gone are removed automatically.

## Coach

Advice comes from a coach connected in the portal (Settings → Coach apps). VibeSynergy does not
run an internal model. `serve` starts the sync, webhook and archive lanes. There is no
assessment lane and no `/api/ai/running` check. An old `[ai]` block in config is ignored;
the service still starts.
