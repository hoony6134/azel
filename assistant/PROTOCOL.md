# Azel — component contracts

Every service listens only on its host's Tailscale address. Every request carries
`Authorization: Bearer <token>` (WebSocket: `?token=<token>` query, since iOS
`URLSessionWebSocketTask` can set headers but browsers can't). Tokens live in 0600 files under
`~/.config/biseo/`, never in code or logs:

| File | Holds | Where |
|---|---|---|
| `token` | shared token: apps → voice server (`client`), gateways' own API, daemons, memory service | jh devices, ruchba; team devices hold a device-local token here instead (their gateway ↔ daemon only) |
| `link_token` | full gateways → voice server `/v1/gateway-link` (`link`) | full gateway hosts |
| `link_token` (team copy) | limited gateways → `/v1/gateway-link` (`link_limited`) | vd-spark, vd-core |
| `state_token_limited` | limited gateways → memory service state API (limited routes only) | vd-spark, vd-core, ruchba |
| `token_hashes` | `<label> <sha256 hex>` per line for `client`, `link`, `link_limited` | vd-spark (voice server, at `/run/secrets/biseo_token_hashes`) |

The voice server stores **no plaintext token**: it compares SHA-256 digests (`hmac.compare_digest`).

| Component | Host | Address | Owner dir |
|---|---|---|---|
| Voice server (STT, router, Qwen, TTS, calls) | vd-spark | `http://100.64.0.11:8891` | `spark/` |
| Qwen3-TTS | vd-spark | `http://100.64.0.11:8890` | `spark/tts/` |
| Claude gateway (Claude, settings, usage, devices, push, scheduler) | every device: Mac, albireo, gienah, deneb (full); vd-spark, vd-core (limited) | `http://<host tailnet IP>:8892` (Mac `100.64.0.8`) | `gateway/` |
| Agent daemon | Mac (later every host) | `http://100.64.0.8:8893/mcp` | `daemon/` |
| iPhone app | iPhone | — | `ios/` |

## 1. Live call — `WS /v1/call` on the voice server

Auth: `Authorization: Bearer <client token>` header (preferred); the `?token=` query is a fallback
for old clients only (it can end up in proxy logs). Both are checked against the `client` hashes.
Query: `session` (stable per conversation), optional `call_id` (set when
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
  - `{"type":"activity","phase","label","tool"}` — what the assistant is doing, for the app's status line
    and sounds. `phase`: `routing` (the turn goes to Claude; sent before any filler, label "Claude에게
    물어보는 중"), `thinking` (API path, label "생각 중"), `tool_start` / `tool_end` (`tool` = the raw tool
    name, e.g. `mcp__claude_ai_Gmail__search_threads`; `label` in Korean, e.g. "메일 확인 중",
    "macbook에서 명령 실행 중"). Never carries tool arguments or results. `/v1/turn` NDJSON carries the
    same objects between its `audio` lines.
  - `{"type":"turn_done","timings":{…}}`
  - `{"type":"error","message":"…"}`
- Client → server **text**: `{"type":"hangup"}` (or just close); `{"type":"interrupt"}` (tap-to-interrupt):
  handled like a voice barge-in, so the current reply and its Claude run are cancelled and the server
  answers `{"type":"interrupt"}` then `{"type":"state","value":"listening"}`. During a placed call's
  opening the first sentence still finishes first.

## 1b. Gateway link — `WS /v1/gateway-link` on the voice server

The **gateway dials the voice server**, never the reverse: under the Headscale ACL the
Spark (`tag:vujade`) may not open connections to jh hosts, and the Spark is shared team
hardware that must not hold personal Claude credentials. Auth: `Authorization: Bearer <link token>`
header only (never the query): a `link` token, or a `link_limited` token, whose link is always limited
whatever its hello says. Tailnet only, never proxied: the voice server closes the socket (4403) when the
peer is not in 100.64.0.0/10 or the request carries `X-Forwarded-For`, `Forwarded` or `X-Real-IP`
(the public proxy is itself a tailnet peer, so the header check is what stops it). **Every gateway keeps its own link**; the voice server holds all of them at once.
JSON text frames:

- gateway → voice: `{"type":"hello","device","mode":"full"|"limited","priority"}` first on every
  connect (`priority`: lower is preferred; a link that never says hello counts as full, priority 50;
  a new hello from the same `device` replaces that device's older link of the same mode; a hello that
  would replace a link of the other mode is ignored),
  `{"type":"settings","value":{…}}` (on connect and every change; from any full-mode link, the last
  one wins, since all gateways read the same shared settings; ignored from limited links),
  `{"type":"call","call_id","opening"}` (sent before the VoIP push; accepted from any full-mode link,
  ignored without a string `call_id`; the voice server synthesizes the opening on arrival),
  `{"id","type":"text"|"activity"|"done"|"error",…}` (answers to an ask, same shapes as `/v1/ask`).
- voice → gateway: `{"type":"ask","id","text","session","model","task"}`,
  `{"type":"cancel","id"}` (barge-in or timeout: the gateway kills that Claude run; sent on the link
  that got the ask).

Routing, per ask: the full-mode link with the lowest `priority` among healthy links (3+ consecutive
failed asks — timeouts or errors without a `code` — make a link unhealthy until it answers again; it
is then tried last). With no full link, limited links take the ask only when the utterance is not
personal (calendar, mail, memory, contacts, GISTRA courses/assignments, Planit, briefings; task
`schedule`); a personal ask is answered "개인 비서 기능은 지금 쓸 수 없어요." With no link at all:
"지금 Claude 게이트웨이에 연결이 안 돼요." A dropped link fails only its own pending asks. Limited links
get only the current utterance, never the call's earlier turns (which may be personal).
`GET /health` lists the connected gateways.

## 1c. Public entry — `https://azel.scian.xyz` (Caddy on deneb → tailnet)

For the phone without Tailscale. Exposed paths only; everything else is blocked at the proxy:
- voice server: `WS /v1/call`, `POST /v1/turn` (client token, header preferred);
- gateway: `GET|PUT /v1/settings`, `GET /v1/usage`, `GET /v1/status`, `GET /v1/approvals`,
  `POST /v1/approvals/{id}`, `POST /v1/push/register` (bearer header).

Never exposed: `/v1/gateway-link` (also refused by the voice server, section 1b), `/v1/ask`,
`/v1/devices/*`, `/v1/calls*`, `/v1/alerts`, the daemons, the memory service.

## 2. One-shot turn — `POST /v1/turn` on the voice server (testing, benchmark)

Body: 16 kHz mono WAV. Response: NDJSON (`transcript`, `audio` with base64 `pcm16`,
`done`). Kept for the benchmark; apps use `/v1/call`.

## 3. Gateway API (`http://<host>:8892`, every device)

Config (env; Linux: `~/.config/biseo/gateway.env`, Mac: the launchd plist): `BISEO_DEVICE` (ASCII
slug, unique), `BISEO_HOST` (Tailscale IP), `BISEO_MODE` `full`|`limited`, `BISEO_PRIORITY`,
`BISEO_STATE_URL` (default `http://100.64.0.10:8894`), `BISEO_VOICE_URL`. Token files (0600, else the
gateway refuses to start): `token`, `link_token` (falls back to `token` until link tokens are rolled
out), and in limited mode `state_token_limited` (required).

- **full** (jh-only devices): Claude via `claude -p` with the user's login and claude.ai connectors
  (Cygnus MCP, Calendar, Gmail) plus every fresh device daemon. Runs the scheduler.
- **limited** (team devices): Messages API only, key from `~/.config/biseo/anthropic_api_key`
  (0600, else the gateway refuses to start); tools = MCP tools of the daemons registered *through
  this gateway* only; personal asks get `{"type":"error","code":"personal","message":"개인 비서
  기능은 지금 쓸 수 없어요."}`. No scheduler; `POST /v1/calls` → 403.

All state below (settings, usage, devices, push tokens, calls, approvals, voice→Claude session map)
lives in the shared state API (section 5), so every gateway answers the same. If ruchba is
unreachable a gateway keeps answering from its cache: settings read-only (`PUT` → 503, last copy in
`~/.config/biseo/settings.json`), usage queued in `pending_usage.json` and flushed later (a row ruchba
refuses with 4xx goes to `usage_rejected.jsonl` instead of blocking the queue), push tokens served from
`push_tokens.json`, daemons that registered through this gateway stay usable, new approvals → 503. All
these files are 0600 and survive a restart. Only full gateways seed empty shared settings from a local
`settings.json`. `claude -p` sessions resume only on the
device that created them.

- `POST /v1/ask` `{"text","session","model"?,"path"?,"task"?}` → NDJSON stream of
  `{"type":"text","delta":"…"}` … `{"type":"done","model","path","usage":{…}}`, with
  `{"type":"activity","phase","tool","label"}` interleaved: `tool_start`/`tool_end` per tool call on the
  `claude -p` path (labels mapped from the tool name only: gistra_* 학사 일정, planit_* 할 일, Google
  Calendar 캘린더, Gmail 메일, memory_* 기억, infra_* 서버 상태, Notion 노션, daemon run_command /
  list_dir·read_file·search / browse / delegate, anything else "도구 준비 중"), one `thinking` on the API path.
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
   "default_device":"macbook","device_fallback":["albireo","spark"],"assistant_name":"아젤"}
  ```
  `assistant_name` (default "아젤"; 1–12 Hangul/Latin letters or spaces, else 400) is the assistant's spoken
  name: the gateways use it for the VoIP push `caller` and Claude's prompts, and the voice server for
  Qwen's system prompt (built per turn from the settings pushed over the link), so a rename applies on
  the next turn or call everywhere.
- `GET /v1/usage` → `{"month":"2026-11","by_model":{…},"by_path":{…},"by_device":{…},"cost_usd":…}`
  (all gateways together; the monthly budget is shared). API-path cost uses list prices per MTok:
  haiku 0.10/0.50, sonnet 2/10, opus 4/20 USD in/out.
- `GET /v1/status` → `{"spark":{"ok","ms"},"tts":…,"state":…,"gateway":{"device","mode","link"},
  "devices":[{"name","ok","ms"}]}`
- `POST /v1/devices/register` `{"name","url","harness":["claude_code",…]}` — daemons call
  this on start and every 60 s. `GET /v1/devices` lists them with `last_seen`.
- `POST /v1/push/register` `{"voip_token","bundle_id","env":"sandbox"|"production"}` —
  the app sends its PushKit token.
- `POST /v1/calls` `{"reason","opening"?}` → places a call (VoIP push). Returns
  `{"call_id"}`. `GET /v1/calls/{id}` → `{"opening":"…"}` (voice server reads it).
- `POST /v1/alerts` — Alertmanager webhook; critical + firing → a call. One call per alert set per
  10 min across gateways (lease `alert:<names>`); if ruchba is down, the gateway calls anyway.
- Scheduled jobs (morning briefing, deadline check) run on every full gateway's clock; the one that
  takes lease `job:<name>:<date>` (24 h) fires it. Lease holder = `<device>-<random per process>`,
  so a restarted gateway cannot fire the same job twice. No lease service → the job is skipped.
- `GET /v1/approvals` / `POST /v1/approvals/{id}` `{"approve":true|false}` — risky
  daemon actions waiting for the user (the app shows them).

## 4. Agent daemon (MCP, streamable HTTP at `/mcp`)

Registration body: `{"id":"macbook","name":"MacBook","url","harness":[…]}` — `id` is an ASCII
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

## 5. Memory service and shared state (`http://100.64.0.10:8894`, ruchba, `memory/`)

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

### State (gateways' shared state)

Tables in their own schema (`state`, env `BISEO_STATE_SCHEMA`), created on start. Timestamps are
epoch seconds. Gateways reach them only through these routes, never the DB, so DB credentials stay
on ruchba. A request is **limited** (only the routes marked *limited* below; everything else, including
records, search, contacts and context, answers 403) when either:
- it carries the scoped token from `/config/state_token_limited` (optional file; the service refuses to
  start if it equals the shared token), or
- `BISEO_LIMITED_IPS` is set (comma list: vd-spark, vd-core) and the peer is in it, **or the peer is not
  a tailnet address** (100.64.0.0/10). Fail closed: Docker's userland proxy rewrites peers to bridge
  addresses (172.x), so the container runs with host networking and binds 100.64.0.10 itself.

- `GET /v1/state/settings` *limited* → the settings document (`{}` if none); `PUT` replaces it.
- `POST /v1/state/usage` *limited* `{"device","model","path","task","input_tokens","output_tokens",
  "cost_usd","ts"?}`; `GET /v1/state/usage?since=<epoch>` *limited* → rows, oldest first.
- `PUT /v1/state/devices/{id}` *limited* `{"name","url","harness","gateway"}` (upsert, sets
  `last_seen` and `writer` = the caller's role, `full` or `limited`). A limited caller may not overwrite a
  row whose writer is `full` (403). `GET /v1/state/devices` *limited* →
  `[{"id","name","url","harness","gateway","writer","last_seen"}]`. Full gateways use (and send the shared
  token to) only rows with `writer: "full"`; limited gateways only rows with their own `gateway`.
- `PUT /v1/state/push_tokens/{token}` `{"bundle_id","env"}`; `GET /v1/state/push_tokens`.
- `POST /v1/state/calls` `{"id","reason","opening"}`; `POST /v1/state/calls/{id}/answer` → the row,
  marked answered (404 if missing).
- `POST /v1/state/approvals` *limited* `{"device","tool","command","cwd"}` → `{"approval_id"}`;
  `GET /v1/state/approvals?status=pending`; `GET /v1/state/approvals/{id}` *limited*;
  `POST /v1/state/approvals/{id}` `{"approve":bool}` → `{"id","status"}`, 409 unless pending.
- `GET|PUT /v1/state/sessions/{voice_session}` `{"claude_session","device"}` (404 if unknown).
- `POST /v1/state/leases/{name}` `{"holder","ttl_s"}` (0 < ttl ≤ 7 days) → `{"held","holder","expires"}`.
  Atomic: taken if free or expired, renewed if `holder` already holds it, else `held:false` with the
  current holder. `DELETE /v1/state/leases/{name}?holder=` → `{"released":bool}`.
