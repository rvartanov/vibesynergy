# Agent events

VibeSynergy tells outside coach agents that something happened, so they can act right away
instead of waiting for a scheduled run. The contract is provider-neutral: an event only
says *what* happened and *which* ids; the agent reads the details and writes back over MCP
(an open standard), for example `vibesynergy_activity_insights`, `vibesynergy_activity` and
`vibesynergy_coach_write(activity_id=…, public_insight=…)`. Code: `query/agent_events.py`.

Agents get events in one of three ways:

- **Push**: a webhook link in Settings → Sync between sources ("Coach AI after a workout").
- **Pull**: the MCP tool `vibesynergy_events(since, types, limit)` (`read` scope) returns the
  same events, oldest first, with `next_since` to continue. For agents that can't receive
  webhooks.
- **Internal**: code inside VibeSynergy (`agent_events.internal_subscribers`) gets every event
  as `fn(rt, conn, event)` in the transaction that stores it. No HTTP, outbox, daily cap or
  quiet hours; each subscriber decides itself whether it is on, and a failing subscriber
  never stops the event. The only one is the API coach runner
  ([COACH_RUNNER.md](COACH_RUNNER.md)): when it is switched on, an `activity.published`
  with an activity still missing its insight queues one insight run.

## Types

- `activity.ready`: new merged activities (`data.activities`).
- `activity.published`: the athlete made the Strava copy public (`data.activities`, `final`).
- `coach.run_missed` (6-11-2026): a coach routine from the task register did not run
  by its due time (`data.task` morning/evening/weekly, `date`, `entry_type`, `prompt`,
  `idempotency_key` to write). Once per task and day. A webhook link to the routine starts
  it without anyone noticing first; the routine writes the entry for that `date`. The
  missed run also reaches the athlete as an alert, so a link that doesn't fire is still seen.

## Event schema (version 1)

```json
{
  "type": "activity.ready",
  "version": 1,
  "id": "evt_3f9c0b2a41d8e6f7a5c1",
  "occurred_at": "2026-10-31T10:05:00Z",
  "data": {
    "activities": [
      {
        "id": "garmin:123",
        "sources": ["garmin:123", "strava:456"],
        "local_date": "2026-10-31",
        "sport": "running",
        "public_insight_missing": true
      }
    ]
  }
}
```

- `type`: `activity.ready` or `activity.published`. Reserved for later: `day.checkin`,
  `plan.changed`.
  The Test button sends `vibesynergy.test` with empty `data`; it is never listed by
  `vibesynergy_events`.
- `version` changes only when a field changes meaning or goes away; new fields may appear
  in the same version, so consumers ignore what they don't know.
- `id` is unique and stable across retries: use it to deduplicate.
- `data` holds ids and non-sensitive facts only. No names, descriptions, health values,
  diary or location.

### `activity.ready`

New activities become ready once nothing merged into them for 5 minutes, so the Garmin and
Strava copies end up as one activity. A Strava-only activity waits up to 30 minutes for its
Garmin copy while Garmin is connected. Activities that become ready together share one event.
An activity is emitted once, also after a later merge or a restart. Only activities that start
after events first ran on this installation, and within the last two days, count: nothing
old is backfilled. `public_insight_missing` is the state at emission time.

### `activity.published`

The athlete made the Strava copy public (that is the trigger for the
coach's public insight). Strava's webhook sends an update with `updates: {"private":
"false"}`; VibeSynergy fetches the activity again, and once its stored `private` flag is false
the next run emits this event. Before going public the athlete has deleted any duplicate
workouts (a delete webhook removes that copy, so it never emits) and finished the title,
description and gear. The stats block on Strava is already there by then, published while
the activity was still private; the insight line is added when it arrives.

```json
{
  "type": "activity.published",
  "version": 1,
  "id": "evt_9a1c…",
  "occurred_at": "2026-11-01T18:40:00Z",
  "data": {
    "final": ["title", "description", "gear"],
    "activities": [
      {
        "id": "garmin:123",
        "sources": ["garmin:123", "strava:456"],
        "local_date": "2026-11-01",
        "sport": "cycling",
        "public_insight_missing": true,
        "public": ["strava:456"]
      }
    ]
  }
}
```

- `final`: what the athlete has settled; the texts themselves are never in the event (read
  them over MCP). Private notes are never sent anywhere.
- `public`: the Strava members that are public now.
- Emitted once per activity, also when it goes private and public again. Only activities
  first seen after this type existed on the installation, within the last two days, and
  settled (the 5-minute debounce) count. It does not wait for a Garmin copy: myWhoosh and
  other Strava-only activities emit too. An activity that is public from the start emits
  once it is settled.
- An activity never made public gets no event; the evening routine fills its insight from
  `vibesynergy_activity_insights` (`missing` lists it, with `strava_public: false`).

## Webhook links

Each link has a name, an address, the event types it wants ("Send a message when" in the
link settings; a new link starts with `activity.published`), an on/off switch (off when
added), and one of three kinds:

| Kind | Request |
| --- | --- |
| Webhook with signature | Body: the event JSON. [Standard Webhooks](https://www.standardwebhooks.com/) headers `webhook-id` (the event id), `webhook-timestamp` (Unix seconds) and `webhook-signature: v1,<base64 HMAC-SHA256 of "id.timestamp.body">`. A `whsec_…` secret is base64 after the prefix; any other secret is used as UTF-8 bytes. The page can make a `whsec_` secret in the browser to copy to the agent. |
| Webhook with token | Body: the event JSON, `webhook-id` and `webhook-timestamp`, and `Authorization: Bearer <token>`. |
| Claude Code routine | An adapter only: `POST https://api.anthropic.com/v1/claude_code/routines/<id>/fire` with `Authorization: Bearer <routine token>`, `anthropic-beta: experimental-cc-routine-2026-04-01` and `{"text": "event=activity.published event_id=evt_… activity_ids=garmin:123"}` (the event's type, id and activity ids). The response's `claude_code_session_url` shows in the delivery log. |

Addresses are `https://` (plain `http://` only for this computer); credentials never go in
the address. Secrets and tokens are stored encrypted in the `agent_events` secret store,
are write-only in the page (it only sees whether one is set) and never appear in logs, the
audit log, errors or API responses.

Delivery rules, per link:

- **Outbox**: one row per event and link (`agent_event_delivery`), written with the event,
  so a restart never loses or repeats one. A row is marked `sending` before the request; if
  the process dies mid-request the row becomes `unknown` and is not sent again.
- **Retries**: 429, 5xx and network errors are retried after 5, 10 and 20 minutes
  (`Retry-After` wins, at most 2 hours), four tries in all; then `gave_up`. Other 4xx
  answers (wrong address or token) stop at once (`failed`).
- **Daily limit** (default 10, 1–50): sends per local day, tests included. Over the limit a
  delivery waits for the next day.
- **Quiet hours** (default 22:30–06:30, local time): deliveries wait until the end.
- **Expiry**: a delivery not sent within 18 hours is dropped (`expired`).
- **Only activities without a coach insight** (default on): an `activity.ready` or
  `activity.published` whose activities all got a public insight meanwhile is `skipped`; a
  Claude routine is only told the ids still missing one.
- Every send is in the audit log (`events.delivery`, target `subscription:<id>`) with the
  event id, attempt, HTTP status and activity ids; link changes are `events.subscription.*`
  (field names only). The page shows the last eight deliveries per link.

The scheduler checks every few minutes (`agent_events` job, like the automatic Strava
publication); the debounce starts after every derive.

## Coach after a workout (Claude Code routine)

The routine "Coach na training – AI-inzicht" listens for `activity.published`: it runs when
The athlete makes the activity public on Strava, reads it over MCP (the athlete's title,
description, RPE and feel when there are any; Zwift and myWhoosh don't ask for RPE, and the
insight does without) and writes the public insight with `vibesynergy_coach_write`. The
automatic publication then adds the line to the Strava, Garmin and intervals.icu block within
about five minutes (see `docs/MCP.md`). The evening routine stays the safety net for anything
not made public that day or missed by the quick run.

The link that existed before `activity.published` (a Claude routine listening only for
`activity.ready`, "only without a coach insight" on) is moved to `activity.published` once by
code (`agent_events.migrate`, audit `events.subscription.update` with actor `vibesynergy`). Set
it back in the link settings if wanted; the move does not repeat. To connect it: in
claude.ai/code/routines open the routine → Trigger + → API → Generate token, then in VibeSynergy
Settings → Sync between sources → Coach AI after a workout → Add a link: kind "Claude Code
routine", address the routine's id or API address, the token; then switch it on and send a
test.

## Related

The old `vibesynergy-watcher` timer (outside this repository, posting to the retired external coach's
coach's webhook) did the same job from outside VibeSynergy. It still runs for now; it can be
replaced by a webhook link later (#8).

How a coach uses these events in its daily and weekly loop, for any AI provider:
[COACH_PROTOCOL.md](COACH_PROTOCOL.md).
