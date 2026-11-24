# Agent events

VibeSynergy notes that something happened, so an AI coach can act right away instead of
waiting for a scheduled run. The contract is provider-neutral: an event only
says *what* happened and *which* ids; the agent reads the details and writes back over MCP
(an open standard), for example `vibesynergy_activity_insights`, `vibesynergy_activity` and
`vibesynergy_coach_write(activity_id=…, public_insight=…)`. Code: `query/agent_events.py`.

Events reach an AI coach in one of two ways:

- **Pull**: the MCP tool `vibesynergy_events(since, types, limit)` (`read` scope) returns the
  events, oldest first, with `next_since` to continue.
- **Internal**: code inside VibeSynergy (`agent_events.internal_subscribers`) gets every event
  as `fn(rt, conn, event)` in the transaction that stores it. Nothing leaves the process;
  each subscriber decides itself whether it is on, and a failing subscriber never stops the
  event. The only one is the API coach runner
  ([COACH_RUNNER.md](COACH_RUNNER.md)): when it is switched on, an `activity.published`
  with an activity still missing its insight queues one insight run.

## Types

- `activity.ready`: new merged activities (`data.activities`).
- `activity.published`: the athlete made the Strava copy public (`data.activities`, `final`).
- `coach.run_missed` (6-11-2026): a coach routine from the task register did not run
  by its due time (`data.task` morning/evening/weekly, `date`, `entry_type`, `prompt`,
  `idempotency_key` to write). Once per task and day. The AI coach inside VibeSynergy
  catches the run up when it is on; the run writes the entry for that `date`. The missed run
  also reaches the athlete as an alert.

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
- `version` changes only when a field changes meaning or goes away; new fields may appear
  in the same version, so consumers ignore what they don't know.
- `id` is unique and stable: use it to deduplicate.
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

The scheduler checks every few minutes (`agent_events` job); the debounce starts after every
derive.

## Outgoing links (removed)

Until #182 (24-11-2026) VibeSynergy could also push events to webhooks: routines in an AI app,
set up under Coach → Settings ("AI coach after a workout"), signed, with quiet hours and a
daily limit per link. Nothing used them any more (the last delivery was on 6-11-2026); the AI
coach inside VibeSynergy reacts to `activity.published` itself, and AI apps pull.

On the first run after the upgrade the `agent_events` job drops the stored links and their
secrets from the encrypted store once (audit `events.subscription.delete`, actor
`vibesynergy`, the link's name only) and closes deliveries that were still waiting
(`skipped`). The tables `agent_event`, `agent_event_delivery` and `agent_event_watch` stay,
and so does the delivery history: old deliveries are no longer pruned, and an event they
name stays with them.

## Related

How a coach uses these events in its daily and weekly loop, for any AI provider:
[COACH_PROTOCOL.md](COACH_PROTOCOL.md).
