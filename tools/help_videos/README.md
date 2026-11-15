# Help videos

The short manuals under Help (`#/help`) are recorded from a real browser against a
throwaway demo portal, never against your own data. Re-record them when a page changes.

1. A demo portal in its own home, on its own port:

   ```bash
   export XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data
   uv run vibesynergy init --demo --email tester@example.test
   printf '\n[portal]\nport = 8593\n' >> "$XDG_CONFIG_HOME/vibesynergy/config.toml"
   # Share links need a public address; any test name will do.
   sed -i 's|# public_url = ""|public_url = "https://vibesynergy.example.test"|' "$XDG_CONFIG_HOME/vibesynergy/config.toml"
   uv run vibesynergy sync
   uv run vibesynergy serve --port 8590 --no-schedule
   ```

2. A test password in `~/vs-help-home/test-password.txt` (12+ characters), then log in once:
   open the link from `vibesynergy login-link`, choose that password, and save the browser
   session to `~/vs-help-home/tour/state.json` (Playwright `storage_state`).

3. Record and compress:

   ```bash
   uv run --with playwright python tools/help_videos/run.py          # or: run.py today coach
   tools/help_videos/build.sh                                          # or: build.sh today coach
   ```

Which videos a change makes out of date: `uv run python tools/check_help_videos.py`
(compares with `origin/main`; `coverage.json` maps each video to the files it shows). CI runs
the same check on every pull request; see CONTRIBUTING.md#help-videos.

Each scenario in `scenarios.py` also checks what it does (a failed step is reported, with a
screenshot in `tour/out/`). Captions come from the `r.say(nl, en)` calls. A new manual needs
a scenario and an entry in `MANUALS` in `static/js/pages/help.js`; `tests/test_help_manuals.py`
checks the files are there.
