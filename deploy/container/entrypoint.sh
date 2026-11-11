#!/bin/sh
# VibeSynergy in a container. All state lives in /data (config, secrets, database, files).
# First start: the config, the database and your account (VIBESYNERGY_EMAIL), with a one-time
# login link in the log; VIBESYNERGY_DEMO=1 adds synthetic data to look around. A newer image on the same volume: backup, then its migrations.
set -eu

if [ "${1:-serve}" = "serve" ]; then
  [ "$#" -gt 0 ] && shift
  if [ ! -f "$VIBESYNERGY_CONFIG" ]; then
    demo=""
    if [ "${VIBESYNERGY_DEMO:-}" = "1" ]; then
      demo="--demo"
    fi
    if [ -n "${VIBESYNERGY_EMAIL:-}" ]; then
      vibesynergy init --email "$VIBESYNERGY_EMAIL" $demo
    else
      vibesynergy init $demo
      echo "No VIBESYNERGY_EMAIL set: make your login with" \
        "'docker exec -it vibesynergy vibesynergy init --email you@example.com'."
    fi
    if [ -n "$demo" ]; then
      vibesynergy sync demo >/dev/null
    fi
  else
    vibesynergy db upgrade --backup
  fi
  exec vibesynergy serve --host 0.0.0.0 --port 8448 --portal-host 0.0.0.0 "$@"
fi
exec vibesynergy "$@"
