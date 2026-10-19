# Notebook B (DE_B) Operations

## Network contract

Notebook B connects to the operator PC's backend, proxy, and Redis over
Tailscale. Keep cross-machine instructions on MagicDNS names instead of fixed
IP addresses because Tailscale addresses can change when the device or network
changes:

- operator PC: `operator`
- Notebook B: `notebook-b`
- peer check: confirm both names appear in `tailscale status`

## Starting the live-device engine

Start DE_B from the launcher folder (`mind-signal-launcher`) by double-clicking
`start-b.bat`, or from a terminal:

```bat
<launcher>\start-b.bat
```

Stop it with `<launcher>\stop-b.bat`.

`start-b.bat` runs `scripts\b-start.ps1 -Operator <name>`; `-Operator` is the
operator PC's MagicDNS name and defaults to `operator`. If that name resolves
to the wrong PC, correct the Tailscale device name instead of substituting a
temporary IP. The launcher runs the engine with the engine venv created by the
launcher's install, so it does not depend on `conda activate` in `cmd`.

The launcher injects these runtime values, which take precedence over stale
values in `.env.local`:

```env
LAN_IP=<Notebook B Tailscale IP>
PROXY_URL=http://operator:5050
BACKEND_URL=http://operator:5000
ALIGNMENT_LOCATION=proxy
REGISTRATION_MODE=local
DUAL_2PC_SUBJECT_INDEX=2
MESSAGE_BUS=memory   # or redis; must match the operator PC
DEV_RELOAD=0         # no uvicorn reload: .venv sits inside the watched folder
# ENGINE_SECRET_KEY is not injected; the engine reads it from .env.local
```

Only `CLIENT_ID`, `CLIENT_SECRET`, and `ENGINE_SECRET_KEY` need to be maintained
locally for Notebook B. Remove obsolete `HEADSET_ID_1` and `HEADSET_ID_2` values
when possible; dynamic headset binding takes precedence even if they remain.

## Connectivity verification

Run connectivity checks outside an agent sandbox. The success criteria are a
DE_B health response with `subject_index=2` and a successful proxy registration
log:

```powershell
curl.exe http://operator:5050/health
curl.exe http://operator:5000/api-docs
curl.exe http://localhost:5002/health
curl.exe http://notebook-b:5002/health
```

Expected DE_B health response:

```json
{"status":"ok","service":"mind-signal-data-engine","subject_index":2}
```

Expected registration log:

```text
proxy 등록 완료: subject_idx=2
```

## Measurement failure diagnosis

Inspect the newest subject-2 core log first:

```powershell
Get-ChildItem .\logs -Filter "core_*_2.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content .\logs\core_<groupId>_2.log
```

`connected`, `Authorize successfully`, and `subscribed successfully` confirm
that the Cortex/headset path is working. `SSL CA certificate loading failed`
means `certificates/rootCA.pem` is missing. `UnicodeEncodeError: cp949` requires
dev commit `8106663` or later. If the latest log is empty, compare its
`LastWriteTime` with the DE_B service log before diagnosing the headset.

For shared Cortex certificate and `LAN_IP` resolution details, also read
`.agents/rules/troubleshooting.md`.
