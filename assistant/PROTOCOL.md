# 김비서 — component contracts

Every service listens only on its host's Tailscale address. Every request carries
`Authorization: Bearer <token>` (WebSocket: `?token=<token>` query, since iOS
`URLSessionWebSocketTask` can set headers but browsers can't). One shared token for
now, kept in a 0600 file (`~/.config/biseo/token` on the Mac), never in code or logs.

| Component | Host | Address | Owner dir |
|---|---|---|---|
| Voice server (STT, router, Qwen, TTS, calls) | vd-spark | `http://100.64.0.11:8891` | `spark/` |
| Qwen3-TTS | vd-spark | `http://100.64.0.11:8890` | `spark/tts/` |
| Claude gateway (Claude, settings, usage, devices, push, scheduler) | Mac now, deneb later | `http://100.64.0.8:8892` | `gateway/` |
| Agent daemon | Mac (later every host) | `http://100.64.0.8:8893/mcp` | `daemon/` |
| iPhone app | iPhone | — | `ios/` |

## 1. Live call — `WS /v1/call` on the voice server

Query: `token`, `session` (stable per conversation), optional `call_id` (set when
answering a call the assistant placed; the server then speaks that call's opening first).

- Client → server **binary**: mono PCM16 little-endian, 16 kHz, any frame size
  (20 ms = 640 bytes recommended). Send continuously while the call is up; the server
  runs VAD, so the client never decides when a turn ends.
- Server → client **binary**: mono PCM16 little-endian, **24 kHz**, play in order.
- Server → client **text** (JSON, one object per message):
  - `{"type":"state","value":"listening"|"thinking"|"speaking"}`
  - `{"type":"transcript","text":"…"}` — what the user said
  - `{"type":"reply","text":"…","route":"qwen"|"claude"}` — sentence being spoken
  - `{"type":"interrupt"}` — the user talked over the assistant: **drop all queued
    playback now**
  - `{"type":"turn_done","timings":{…}}`
  - `{"type":"error","message":"…"}`
- Client → server **text**: `{"type":"hangup"}` (or just close).

## 1b. Gateway link — `WS /v1/gateway-link` on the voice server

The **gateway dials the voice server**, never the reverse: under the Headscale ACL the
Spark (`tag:vujade`) may not open connections to jh hosts, and the Spark is shared team
hardware that must not hold personal Claude credentials. Auth: `Authorization: Bearer`
header (not query). One link at a time; a new one replaces the old. JSON text frames:

- gateway → voice: `{"type":"settings","value":{…}}` (on connect and every change),
  `{"type":"call","call_id","opening"}` (sent before the VoIP push),
  `{"id","type":"text"|"done"|"error",…}` (answers to an ask, same shapes as `/v1/ask`).
- voice → gateway: `{"type":"ask","id","text","session","model","task"}`,
  `{"type":"cancel","id"}` (barge-in or timeout: the gateway kills that Claude run).

## 2. One-shot turn — `POST /v1/turn` on the voice server (testing, benchmark)

Body: 16 kHz mono WAV. Response: NDJSON (`transcript`, `audio` with base64 `pcm16`,
`done`). Kept for the benchmark; apps use `/v1/call`.

## 3. Gateway API (`http://100.64.0.8:8892`)

- `POST /v1/ask` `{"text","session","model"?,"path"?,"task"?}` → NDJSON stream of
  `{"type":"text","delta":"…"}` … `{"type":"done","model","path","usage":{…}}`.
  `model`: `haiku|sonnet|opus|auto`; `path`: `api|claude_code|auto`
  (auto = API when a key is configured, else `claude -p`). Omitted → settings defaults.
- `GET /v1/settings` / `PUT /v1/settings` — whole JSON document:
  ```json
  {"default_model":"auto","claude_path":"auto",
   "task_defaults":{"chat":{"model":"qwen"},"schedule":{"model":"sonnet"},
                    "code":{"model":"opus","path":"claude_code"},"mac":{"model":"sonnet"}},
   "monthly_budget_usd":500,"budget_warn_pct":80,
   "proactive":{"morning_briefing":"08:00","quiet_hours":["23:30","07:30"],
                "triggers":{"deadline":true,"server_down":true}},
   "default_device":"맥북","device_fallback":["알비레오","스파크"]}
  ```
- `GET /v1/usage` → `{"month":"2026-11","by_model":{…},"by_path":{…},"cost_usd":…}`
- `GET /v1/status` → `{"spark":{"ok","ms"},"gateway":{…},"devices":[{"name","ok","ms"}]}`
- `POST /v1/devices/register` `{"name","url","harness":["claude_code",…]}` — daemons call
  this on start and every 60 s. `GET /v1/devices` lists them with `last_seen`.
- `POST /v1/push/register` `{"voip_token","bundle_id","env":"sandbox"|"production"}` —
  the app sends its PushKit token.
- `POST /v1/calls` `{"reason","opening"?}` → places a call (VoIP push). Returns
  `{"call_id"}`. `GET /v1/calls/{id}` → `{"opening":"…"}` (voice server reads it).
- `POST /v1/alerts` — Alertmanager webhook; critical + firing → a call.
- `GET /v1/approvals` / `POST /v1/approvals/{id}` `{"approve":true|false}` — risky
  daemon actions waiting for the user (the app shows them).

## 4. Agent daemon (MCP, streamable HTTP at `/mcp`)

Registration body: `{"id":"macbook","name":"맥북","url","harness":[…]}` — `id` is an ASCII
slug used as the MCP server name on the Claude side.

Approval flow: an approval-required `run_command` posts
`{"device","tool":"run_command","command","cwd"}` to `POST /v1/approvals` and returns
`pending_approval`. After the user approves in the app, Claude calls `run_command` again
with `approval_id`; the daemon checks `GET /v1/approvals/{id}` → `{"status":"approved",
"command","cwd"}`, requires an exact command/cwd match, and accepts each id once.

Tools: `list_dir(path)`, `read_file(path, max_bytes?)`, `search(query, path?)`,
`run_command(command, cwd?, approval_id?)`, `delegate(task, cwd, harness?, model?)` →
`{job_id}`, `job_status(job_id)`, `list_jobs()`.
`browse(url, selector?, wait_for?, max_chars?)` → `{url, title, text}`: headless Chromium visible text; http(s) only, private/Tailscale hosts refused unless in `browse_allow_private`.
Config `~/.config/biseo/daemon.toml`: `name`, `allowed_roots`, `allowed_commands`
(prefix match), `approval_required` patterns (rm, git push, …). Approval-required
calls return `{"status":"pending_approval","approval_id"}` and are posted to the
gateway's `/v1/approvals`. Every call is logged as a JSON line.

## 5. Memory service (`http://100.64.0.10:8894`, ruchba, `memory/`)

Shared by every device. Long-term facts (preferences, people, projects, decisions) stay in
Cygnus MCP memory; this service holds task context, raw records and contacts, in Postgres
database `biseo` on ruchba (role `biseo_app`). Same bearer token as the other services.
Runs in Docker (`memory/compose.yaml`, container `biseo-memory`, restart unless-stopped).

- `GET /health` (no auth) → `{"ok":true}`
- `POST /v1/records` `{"kind":"transcript"|"task_log"|"daemon_result"|"note","text","session"?,
  "device"?,"meta"?:{…}}` → `{"id","created"}`. The text is embedded on write
  (`multilingual-e5-small`, int8 ONNX, CPU; only the first 512 tokens are embedded).
- `POST /v1/search` `{"query","kinds"?:[…],"limit"?:10 (≤100),"since"?:ISO-8601}` →
  `[{"id","kind","session","device","text","meta","created","score","vec","lex","fresh"}]`,
  best first. `score = 0.6·vec + 0.3·lex + 0.1·fresh`: `vec` is cosine similarity, `lex` the
  larger of trigram word similarity and `simple` full-text rank, `fresh = exp(-age_days/30)`.
- `PUT /v1/context/{task_id}` `{"summary","open_questions":["…"]}` → the stored row
  `{"task_id","summary","open_questions","updated"}` (upsert, replaces both fields).
  `GET /v1/context/{task_id}` → that row or 404. `GET /v1/context?recent=N` (default 10,
  ≤100) → rows, most recently updated first.
- Contacts `{"id","name","phone","email","notes","updated"}`: `POST /v1/contacts`
  `{"name","phone"?,"email"?,"notes"?}`, `GET /v1/contacts?q=…` (substring over all
  fields), `GET|PUT|DELETE /v1/contacts/{id}` (PUT replaces every field; 404 if missing).
