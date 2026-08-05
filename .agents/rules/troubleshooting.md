# Troubleshooting — Mind Signal Data Engine

## Cortex connection error

- Confirm the Emotiv App is running.
- Confirm `CLIENT_ID` and `CLIENT_SECRET` are set in `.env.local`.
- Confirm the Emotiv App itself is logged into an Emotiv account.

## Redis connection error

```bash
# confirm Docker Redis is running (from the backend folder)
cd ../mind-signal-backend
docker-compose up -d
```

## Package error

```bash
conda activate mind-signal
pip install -r requirements.txt
```

`--break-system-packages` is not needed here — that flag exists for pip fighting an externally-managed *system* Python; inside an activated conda env it's the wrong tool and can mask a "you forgot to activate the env" mistake instead of fixing it.

`requirements.txt:23` currently pins `packaging` to a local file path (`file:///C:/miniconda3/conda-bld/packaging_.../work`) left over from the machine that generated the lock file. This fails to resolve on any other machine. Until it's regenerated (`pip freeze` or a separate `requirements.lock.txt`), a fresh clone needs that line edited or dropped manually before install succeeds.

## Python path (common mistake)

- If you skip `conda activate mind-signal`, you get the system Python (3.13/3.9) and dependencies break.
- Verify with `python --version` after activating — don't assume the shell already has the right interpreter selected.

## Cortex TLS certificate — `certificates/rootCA.pem` required locally

`sdk/cortex.py` reads `certificates/rootCA.pem` (repo root) as the TLS CA for the Cortex `wss://` connection. This file is gitignored, so it does not travel with a clone — it must be copied onto every new machine by hand (copy it from an existing Emotiv install).

**Symptom if missing**: `core.main` prints `SSL CA certificate loading failed: [Errno 2] No such file or directory` and exits with return code 0 after roughly 0 seconds — the headset is still discovered normally, but the measurement session is 0 seconds long. This looks like a headset problem; it isn't. Confirmed as the root cause of a "session ends instantly" failure during live cross-machine validation (2026-06-28).

## LAN_IP / Tailscale resolution priority

The engine advertises its own IP to the proxy so the operator machine can reach it. Resolution order:

1. Explicit `LAN_IP` env var (the launcher script injects this via `tailscale ip -4` when available).
2. Auto-detected Tailscale address (100.64.0.0/10 range) via `_detect_tailscale_ip` in `server/app.py`.
3. `socket.gethostbyname` as a last-resort fallback.

Tailscale is the primary transport (a LAN-hotspot-primary setup was tried earlier and abandoned). The `socket.gethostbyname` fallback has a known failure mode: on machines with Docker or WSL or multiple Wi-Fi adapters, it can pick the wrong interface and advertise an address the other machine can't reach (observed: a Wi-Fi LAN address like `10.26.x.x` instead of the Tailscale address, causing the operator's assign-group call to time out).

**If setting `LAN_IP` manually, it must be that machine's Tailscale address (100.x.x.x)** — never a LAN/Wi-Fi IP.

Two related env vars gate proxy mode: `REGISTRATION_MODE` (must be `local` when a proxy is in play — an `ngrok` value left over from an earlier setup will route the registration URL through the proxy's `/register` incorrectly) and `ALIGNMENT_LOCATION` + `PROXY_URL` (the pair that activates proxy mode; both default empty in `.env.example` so proxy mode is off unless explicitly configured).

When migrating between phases/setups, audit `.env.local` and `.env.example` for stale values left over from a previous configuration — they're easy to leave behind and can silently break the next environment that inherits them.
