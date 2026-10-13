# Redis Channel Contract — Mind Signal Data Engine

## Channel key convention

```
mind-signal:{groupId}:subject:{subjectIndex}
```

- `groupId`: MongoDB ObjectId string (backend passes it as `sys.argv[1]` at spawn).
- `subjectIndex`: **1-based**, only `1` or `2` (backend validates with `z.number().int().min(1).max(2)` in `engine.routes.ts`; passed as `sys.argv[2]`).
- No fixed/global channel names — `mind-signal-live` must not come back.
- No PC/host info embedded in the channel key — no IP/hostname.

## Message types published on this channel

### `brain_sync_all` — periodic wave/metrics payload

```json
{
  "type": "brain_sync_all",
  "groupId": "<string>",
  "subjectIndex": 1,
  "waves": {
    "delta": 0.0,
    "theta": 0.0,
    "alpha": 0.0,
    "beta": 0.0,
    "gamma": 0.0
  },
  "metrics": {
    "focus": 0.0,
    "engagement": 0.0,
    "interest": 0.0,
    "excitement": 0.0,
    "stress": 0.0,
    "relaxation": 0.0
  },
  "time": "<YYYY-MM-DD HH:MM:SS.ffffff>"
}
```

Source: the publish call in `core/streamer.py` (search for the `"type": "brain_sync_all"` payload dict, not a line number — line numbers here go stale fast).

### `headset_status` — connection health alert

Also published on the same channel, by `_publish_health()` and `on_headset_disconnected()` in `core/streamer.py`.

Watchdog-triggered alerts (`no_data`, `metrics_stale`) include `silentSeconds` — seconds of no signal, published by `_publish_health()`:

```json
{
  "type": "headset_status",
  "status": "no_data | metrics_stale",
  "subjectIndex": 1,
  "groupId": "<string>",
  "silentSeconds": 0
}
```

`disconnected` is published separately by `on_headset_disconnected()` and does not carry `silentSeconds` — the field is optional on this message type, present only for the two watchdog statuses:

```json
{
  "type": "headset_status",
  "status": "disconnected",
  "subjectIndex": 1,
  "groupId": "<string>"
}
```

The backend normalizes `status` — it keeps `disconnected` as-is and folds the others into `stale` (see `stream-health.service.ts` on the backend side).

## `MESSAGE_BUS=memory` — no Redis

`MESSAGE_BUS` selects the transport: `redis` (default, everything above) or `memory`. `core.main` reads it from the environment and passes it to the streamer like the other proxy settings.

With `memory` the streamer never calls `redis.from_url` and nothing is published on the channel above:

- Samples go to the proxy only (`POST {PROXY_URL}/ingest/sample`, unchanged). This requires `ALIGNMENT_LOCATION=proxy` and a non-empty `PROXY_URL`; otherwise `validate_message_bus()` raises `MessageBusConfigError` before Cortex is opened and `core.main` exits 1 with the cause.
- `headset_status` alerts go to `POST {PROXY_URL}/status` with header `X-Engine-Secret` and body:

```json
{
  "group_id": "<string>",
  "subject_idx": 1,
  "status": "no_data | metrics_stale | invalid_data | disconnected",
  "silent_seconds": 0
}
```

`silent_seconds` is omitted for `disconnected`, mirroring `silentSeconds` above. The proxy forwards the same four fields to the backend as Socket.io event `proxy:status`. One attempt, 2 s timeout (`STATUS_POST_TIMEOUT_SEC`), no retry; a failure is logged as a warning and never stops the measurement.

Solo measurement (neither `DUAL_2PC_GROUP_ID` nor `DUAL_2PC_SUBJECT_INDEX` set) under `memory`:

- `server/app.py` lifespan takes the 1PC legacy backend registration branch even though `ALIGNMENT_LOCATION=proxy`, because the backend refuses to start a measurement without that slot.
- The streamer runs with `proxy_fail_closed=False`: a failed sample POST logs a warning and CSV recording continues, and the proxy `/health` monitor is not started. This matches what the `redis` solo path does when a publish fails. DUAL_2PC keeps fail-closed.

## Entry point

Current form only: `python -m core.main <groupId> <subjectIndex>`.

**Why not `core.streamer` directly**: `core/streamer.py` has no `if __name__ == "__main__"` block — running it as a module exits immediately. It exists to support concurrent sessions: each session gets its own spawned process and its own dynamic channel key, so a single long-running process handling all sessions (the old model) is no longer possible.
