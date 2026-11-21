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
| iPhone / Mac app | iPhone, Mac | — | `app/` |

## 1. Live call — `WS /v1/call` on the voice server

Auth: `Authorization: Bearer <client token>` header. The `?token=` query is a **deprecated** fallback for
old clients (it can end up in proxy logs); the server logs a one-line warning without the value each time
it is used, and it will be removed once every shipped app sends the header. Both are checked against the
`client` hashes. The voice server never logs query strings or headers: its container runs uvicorn with
`--no-access-log --log-level warning`, and query strings in uvicorn's log lines are redacted.
Query: `session` (stable per conversation), optional `client` (`iphone`|`mac`|`terminal`, for the call
history), optional `intent=briefing` (the app's "오늘 브리핑": the server speaks a briefing first, see below),
optional `call_id` (set when
answering a call the assistant placed; the server then speaks that call's opening first).

- Client → server **binary**: mono PCM16 little-endian, 16 kHz, any frame size
  (20 ms = 640 bytes recommended). Send continuously while the call is up; the server
  runs VAD, so the client never decides when a turn ends.
- Server → client **binary**: mono PCM16 little-endian, **24 kHz**, play in order.
- Server → client **text** (JSON, one object per message):
  - `{"type":"state","value":"listening"|"thinking"|"speaking"}` — `speaking` lasts until the client has
    played every byte sent (the server tracks that from the audio length, pauses included) plus 300 ms
    (`PLAY_TAIL_MS`), not just until generation ends; only then come `turn_done` and `listening`. Speech in
    that window is a barge-in (`pause`/check), never a new turn.
  - `{"type":"transcript","text":"…"}` — what the user said
  - `{"type":"reply","text":"…","route":"qwen"|"claude"}` — text of the audio chunk that follows: one or
    more whole sentences (or a clause ending in ", "), never under 5 syllables unless it is the whole reply,
    never cut mid-word. Chinese characters, kana, Cyrillic and Thai are dropped from it (and from the audio
    and history).
  - `{"type":"pause"}` — the user may be talking over the assistant: **pause playback, keep the queue**
  - `{"type":"resume"}` — it was noise or not a real interruption: continue the paused queue where it stopped
  - `{"type":"interrupt"}` — the user talked over the assistant: **drop all queued
    playback now** (after a `pause`, or after a tap-to-interrupt). An interrupt while paused is followed by
    `resume`, so the (now empty) queue is never left paused; a barge-in check whose STT fails also resumes.
  - `{"type":"activity","phase","label","tool","kind","route":"claude"}` — what the assistant is doing, for
    the app's status line, icon and sounds. Only Claude turns send any; a Qwen turn sends none. `kind`:
    `calendar|mail|memory|schedule|server|files|web|terminal|notes|generic` (gistra/planit → schedule,
    Google Calendar → calendar, Gmail → mail, memory → memory, infra → server, list_dir/read_file/search →
    files, browse → web, run_command/delegate → terminal, Notion → notes, anything else generic). `phase`:
    `routing` (the turn goes to Claude; sent before any filler, label "Claude에게 물어보는 중", kind generic), `thinking` (API path, label "생각 중"), `tool_start` / `tool_end` (`tool` = the raw tool
    name, e.g. `mcp__claude_ai_Gmail__search_threads`; `label` in Korean, e.g. "메일 확인 중",
    "macbook에서 명령 실행 중"). Never carries tool arguments or results. `/v1/turn` NDJSON carries the
    same objects between its `audio` lines.
  - `{"type":"turn_done","timings":{…},"audio_ends_in_ms":N}` — sent once playback has finished (see
    `state`); `audio_ends_in_ms` is how much audio was still queued when generation ended (diagnostic).
  - `{"type":"error","message":"…"}`
- Briefing (`intent=briefing`, without `call_id`): the server first asks a full gateway for the situation
  (`briefing_context` over the link, 2 s timeout). With a situation note, the local Qwen composes a
  30–40 s briefing from it right away (today's date, classes/meetings, deadlines, todos, server health,
  pending approvals, the next scheduled call), spoken sentence by sentence as `reply` frames with
  `"route":"briefing"`. With no note yet it says the filler, sends `activity` `routing`, and streams a
  Claude briefing over the link (the morning-briefing prompt) with its activity events; at most one such
  Claude briefing per 10 min, within which the last briefing text is spoken again. Barge-in works
  as for any reply. The call log records `reason: "briefing"`; what was said becomes the call's
  `opening`.
- Barge-in and noise. Voice activity needs Silero's probability (0.5; noisy 0.7) **and** a margin over the
  call's own noise floor (10 dB; noisy 15 dB). The floor follows the level down fast and up about 1 % per
  32 ms frame, so steady background chatter becomes the floor within seconds. While the assistant is
  speaking, speech onset only sends `pause`. The interrupt is committed when that utterance ends, only if
  it had at least 500 ms of voiced frames and its transcript has real content: two or more Hangul syllables
  outside filler sounds (음, 어…), and not a Whisper silence hallucination. Otherwise the server sends
  `resume`. Shorter blips (a cough, a click) end in `resume` without STT. The commit comes about 0.5 s after
  the utterance ends, plus STT; playback is paused from the onset, so the assistant goes quiet as fast as
  before. While the assistant is thinking (STT, Qwen or Claude running), voice activity never cancels: the
  new utterance is transcribed, noise is ignored, and stop words (아니, 그만, 잠깐, 취소…) or a new question
  replace the current answer (`interrupt`, then the new one). Anything else is answered after the current
  one. Shared setting `noise_mode` `normal`|`noisy`: noisy raises the thresholds (min voiced 400 ms) and
  turns voice barge-in off while the assistant speaks; tap-to-interrupt still works. Tunable by env:
  `VAD_THRESHOLD`, `NOISE_MARGIN_DB`, `MIN_VOICED_MS`, `BARGE_MIN_VOICED_MS` and their `NOISY_` variants.
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

- gateway → voice: `{"type":"hello","device","mode":"full"|"limited","priority","claude"?,"role"?}` first on every
  connect (`role: "brain"`: the resident brain's link, section 6; never picked for asks, logs or schedules;
  `claude: false` = a gateway without Claude, e.g. an always-on deneb: it never gets asks but
  takes schedule requests, call logs and answered reports; `priority`: lower is preferred; a link that never says hello counts as full, priority 50;
  a new hello from the same `device` replaces that device's older link of the same mode; a hello that
  would replace a link of the other mode is ignored),
  `{"type":"settings","value":{…}}` (on connect and every change; from any full-mode link, the last
  one wins, since all gateways read the same shared settings; ignored from limited links),
  `{"type":"call","call_id","opening","reason"}` (sent before the VoIP push; accepted from any full-mode link,
  ignored without a string `call_id`; the voice server synthesizes the opening on arrival),
  `{"id","type":"text"|"activity"|"done"|"error",…}` (answers to an ask, same shapes as `/v1/ask`).
- voice → gateway: `{"type":"call_log","event":"start"|"turn"|"end","log_id",…}` — call history, sent
  only to a full-mode link (dropped when none is up; the voice server stores no history):
  start `{"session","call_id","direction":"incoming"|"outgoing","reason","client","opening","started_at"}`,
  where `direction` is from the USER's point of view, the same as a phone's call log:
  `"direction":"incoming"`: the assistant called the user (VoIP push / `call_id` present).
  `"direction":"outgoing"`: the user called the assistant (app call button, Siri, Mac push-to-talk).
  (Voice servers before this was pinned sent the opposite; `memory_service.py --fix-call-directions` corrects
  stored rows by `call_id` presence, idempotently.)
  turn `{"user","assistant":["sentence",…],"route","activity":[{"label","kind","at"},…],"timings",
  "interrupted","at"}` (`at` in epoch seconds; activity = the turn's tool_start/thinking steps)
  (never for an empty transcript; fillers left out), end `{"ended_at","duration_s"}`. One `log_id` per
  `/v1/call` connection; a turn cut short by hang-up is sent before the end.
- voice → gateway: `{"type":"schedule","id","action":"create"|"list"|"cancel","at"?,"repeat"?,"kind"?,"note"?}`
  (to a full-mode link, Claude or not; `at` ISO 8601 with offset for create, epoch for cancel) → gateway →
  voice `{"id","type":"schedule_result","ok","say","item"?|"items"?}` (items in the `/v1/schedule` shape,
  `created_by: "voice"`); the voice server speaks `say` (e.g. "내일 오전 10시에 깨워 드릴게요.").
  `{"type":"call_answered","call_id"}` when `/v1/call` connects with a `call_id` (stops a scheduled call's
  retries). `{"type":"briefing_context","id"}` → gateway → voice `{"id","type":"briefing_context",
  "situation": "<azel-situation, or azel-checkpoint if that is stale; \"\" if neither is fresh>",
  "facts":["승인 대기 N건","다음 예약 전화: …","최근 24시간 서버 경보: …"]}`.
- voice → gateway: `{"type":"ask","id","text","session","model","task"}`,
  `{"type":"cancel","id"}` (barge-in or timeout: the gateway kills that Claude run; sent on the link
  that got the ask).

Scheduled-call utterances are handled before any routing, without Qwen's answer or Claude: a keyword
gate (깨워, 알람, 모닝콜; "전화해 줘" / "예약해 줘" plus a time and no third-person target; 예약 목록 / 취소)
and a deterministic Korean time parser (내일 10시, 내일 아침 7시 반, 30분 뒤, 모레 오후 3시, 매일 7시,
평일 6시 40분, 매주 수요일 9시, 다음 주 월요일 9시; a bare "10시" is today if still ahead, else tomorrow).
Phrasings the rules miss go to the local Qwen for strict JSON (given the current KST time), then
validation (ahead, within 30 days, known repeat, `weekly:N` on that weekday; a time without an offset
means KST). A weekday mismatch asks back ("무슨 요일 몇 시에 할까요?"). With no time the voice server asks back ("몇 시에 깨워
드릴까요?") and the next utterance with a time completes it.

No bare acknowledgements: when a question or request (not a greeting, thanks or yes/no confirmation) gets a
Qwen reply with no content (only "네", "알겠습니다", "확인해 볼게요" and the like), the voice server holds it back,
asks Qwen once more with a nudge, and hands the turn to Claude if the second reply is empty too. A leading
acknowledgement followed by content is spoken as usual.

Routing, per other ask: the full-mode link (with Claude) with the lowest `priority` among healthy links (3+ consecutive
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
  `POST /v1/approvals/{id}`, `POST /v1/push/register`, `GET|PUT /v1/connectors`, `PUT /v1/connectors/secret/{name}`,
  `GET|DELETE /v1/history`, `GET|DELETE /v1/history/{id}`, `GET|POST /v1/schedule`, `DELETE /v1/schedule/{id}`,
  `GET /v1/proactive/log`, `GET|POST /v1/personas`, `GET|PUT|DELETE /v1/personas/{id}`,
  `POST /v1/personas/{id}/primary`, `POST /v1/personas/voice-sample`, `GET /v1/voices`,
  `GET|POST|DELETE /v1/wake/enroll`, `GET /v1/providers/nim/models`, `PUT /v1/providers/nim/key`,
  `GET|POST /v1/tasks`, `GET /v1/tasks/{id}`, `POST /v1/tasks/{id}/cancel` (bearer header).

Never exposed: `/v1/gateway-link` (also refused by the voice server, section 1b), `/v1/ask`,
`/v1/devices/*`, `/v1/calls*`, `/v1/alerts`, the daemons, the memory service.

## 2. One-shot turn — `POST /v1/turn` on the voice server (testing, benchmark)

Body: 16 kHz mono WAV. Response: NDJSON (`transcript`, `audio` with base64 `pcm16`,
`done`). Kept for the benchmark; apps use `/v1/call`.

## 3. Gateway API (`http://<host>:8892`, every device)

Config (env; Linux: `~/.config/biseo/gateway.env`, Mac: the launchd plist): `BISEO_DEVICE` (ASCII
slug, unique), `BISEO_HOST` (Tailscale IP), `BISEO_MODE` `full`|`limited`, `BISEO_PRIORITY`,
`BISEO_STATE_URL` (default `http://100.64.0.10:8894`), `BISEO_VOICE_URL`, `BISEO_NO_CLAUDE=1` (this
gateway has no Claude: asks answer `code: "no_claude"`, no briefings or deadline checks),
`BISEO_SCHEDULER_PRIORITY` (default `BISEO_PRIORITY`; lowest live value fires scheduled calls),
`BISEO_LLM_URL` / `BISEO_LLM_MODEL` (the local Qwen for autonomous-call decisions; default vd-spark
`http://100.64.0.11:8888/v1/chat/completions`, `Qwen3.8-Flash-Next`). Token files (0600, else the
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
  `{"type":"text","delta":"…"}` … `{"type":"done","model","path","usage":{…}}` (or, when a Claude run ends in an
  error with nothing said, `{"type":"error","code":"limit"|"claude_error","message":"<speakable>"}`: `limit` for the
  subscription's usage limit, with the reset time when Claude names it), with
  `{"type":"activity","phase","tool","label","kind","route":"claude"}` interleaved (kinds as in section 1): `tool_start`/`tool_end` per tool call on the
  `claude -p` path (labels mapped from the tool name only: gistra_* 학사 일정, planit_* 할 일, Google
  Calendar 캘린더, Gmail 메일, memory_* 기억, infra_* 서버 상태, Notion 노션, daemon run_command /
  list_dir·read_file·search / browse / delegate, anything else "도구 준비 중"), one `thinking` on the API path.
  `model`: `haiku|sonnet|opus|auto|nim`; `path`: `api|claude_code|auto`
  (auto = API when a key is configured, else `claude -p`). Omitted → settings defaults.
- `GET /v1/settings` / `PUT /v1/settings` — whole JSON document:
  ```json
  {"default_model":"auto","claude_path":"auto",
   "task_defaults":{"chat":{"model":"qwen"},"schedule":{"model":"sonnet"},
                    "code":{"model":"opus","path":"claude_code"},"mac":{"model":"sonnet"}},
   "monthly_budget_usd":500,"budget_warn_pct":80,
   "proactive":{"enabled":true,
                "morning_briefing":{"enabled":true,"time":"08:00"},
                "deadline_check":{"enabled":true,"time":"20:00"},
                "server_alerts":true,
                "autonomous":{"enabled":false,"max_per_day":3,"min_gap_minutes":120,
                              "window":["08:00","23:00"],"checkpoints":["08:30","13:00","18:00"]}},
   "default_device":"macbook","device_fallback":["albireo","spark"],"assistant_name":"아젤",
   "history_days":365,"reply_length":"normal","noise_mode":"normal","personality":""}
  ```
  `personality` (text up to 600 chars, else 400) is Azel's character: one persona block (`assistant_name` +
  personality) goes into Qwen's system prompt, Claude's voice style, briefings and autonomous-call openings,
  so both models sound like the same person. `""`, whitespace, `null` or a missing key mean the built-in
  default; the stored value is returned as is (`""` after "기본값으로 되돌리기"), and `GET /v1/settings`
  adds the read-only `personality_default` (the default text, for the app's placeholder; a `PUT` that sends
  it back is fine, it is never stored). The gateway also pushes it to the voice server, so the default text
  has one source.
  `reply_length` `brief`|`normal`|`detailed` (else 400) sets how long answers run, with the same wording for
  Qwen's system prompt and Claude's voice style: brief = one or two sentences; normal = brief for chit-chat,
  about four or five sentences for explanations, summaries, schedules and status reports; detailed = up to
  six to eight. The first clause stays short either way (latency). Qwen's `max_tokens` follows it (250 /
  600 / 1000).
  `proactive.enabled` is the master switch ("먼저 전화하기"): off, no proactive call fires (briefing,
  deadline check, server alerts, autonomous calls). User-created scheduled calls always fire. There are no
  quiet hours any more (scheduled calls replace them). A stored settings document in the old shape
  (`"morning_briefing":"08:00"`, `quiet_hours`, `triggers.deadline/server_down`) is migrated on read.
  `PUT` with `proactive` merges one level deep into the current values (e.g. `{"proactive":{"autonomous":
  {"enabled":true}}}`), then validates: times `HH:MM`, `window` start < end, `max_per_day` 0–10,
  `min_gap_minutes` 0–1440, at most 6 `checkpoints`; otherwise 400.
  `assistant_name` (default "아젤"; 1–12 Hangul/Latin letters or spaces, else 400) is the assistant's spoken
  name: the gateways use it for the VoIP push `caller` and Claude's prompts, and the voice server for
  Qwen's system prompt (built per turn from the settings pushed over the link), so a rename applies on
  the next turn or call everywhere.
- Connectors: which MCP servers a full gateway's Claude runs load (warm or one-shot). Shared setting `connectors`
  (also writable as part of `PUT /v1/settings`):
  ```json
  {"claude_ai": {"Gmail": true, "Notion": false},
   "custom": [{"name": "wiki", "url": "https://wiki.example/mcp", "transport": "http", "enabled": true, "auth_ref": "wiki"}]}
  ```
  `claude_ai` keys are claude.ai connector display names without the `claude.ai ` prefix; a name not listed takes its
  default (Cygnus MCP, Google Calendar, Gmail on; everything else off). `custom`: ≤ 20 servers, `name` a unique ASCII
  slug (`[A-Za-z0-9_-]{1,40}`, also the tool prefix `mcp__<name>`), `url` https (plain http only to a tailnet
  address, 100.64.0.0/10), `transport` `http`|`sse` (default http), `enabled` (default true), optional `auth_ref`,
  which must equal the server's own `name` (a server never uses another's secret). Secrets never go into settings. Each
  run also loads the device daemons, no plugin MCP servers, and every tool up front (no ToolSearch). A custom server
  whose secret file is missing or not 0600 is skipped. Full gateways only; limited → 403 on every route below.
  - `GET /v1/connectors[?refresh=true]` → `{"claude_ai":[{"name","enabled","status":"connected"|"needs_auth"}],
    "custom":[{"name","url","transport","enabled","has_secret"}]}` (the app's pinned shape). The claude.ai list is
    this gateway's login as `claude mcp list` sees it (cached from startup; `refresh=true` re-reads, ~15 s); anything
    not connected is `needs_auth`.
  - `PUT /v1/connectors` with the same shape without `status` / `has_secret` → the GET shape (400 with a reason on a
    bad shape). Replaces both lists; stored as the setting above, a custom server's secret named after the server.
  - `PUT /v1/connectors/secret/{name}` `{"value": "<token>"}` (a raw token is sent as `Authorization: Bearer <token>`)
    or `{"value": "{\"X-Api-Key\": \"…\"}"}` (a JSON object of headers) → **204**. Write-only: stored in
    `~/.config/biseo/mcp/<name>` (0600) on **this** gateway only, never returned by any route; restarts this gateway's warm
    Claude processes. Each full gateway that should use the server needs its own copy.
- NVIDIA NIM provider (full gateways only; `gateway/nim.py`). The hosted, OpenAI-compatible API, never a local
  container. Setting `"providers":{"nim":{"enabled":false,"base_url":"https://integrate.api.nvidia.com/v1",
  "model":"nvidia/nemotron-3-ultra-550b-a55b","prices":{}}}`; GET adds the read-only `"has_key"`. A partial
  `providers.nim` merges into the current one; `base_url` must be https without credentials; `model` is free text;
  `prices` `{model: [usd_in, usd_out] per MTok}` (absent → cost 0). `default_model`, `task_defaults.*.model`, a
  persona's `default_model` and `/v1/ask`'s `model` accept `"nim"`; the voice server sends `model:"nim"` for
  "엔비디아로 …". The NIM path streams like Claude (text deltas, `activity` thinking/tool_start/tool_end, `done`) with
  the devices' MCP tools as function tools; a model that answers 400 to a request with tools is asked again without
  them (remembered per model). Speakable errors: `nim_key` (no key, or the key refused), `nim_off`, `nim_error`.
  Usage rows: `path:"nim"`, `model` = the NIM model id. A limited gateway treats `nim` as its usual Claude path.
  - `GET /v1/providers/nim/models` → `{"models":[ids]}` from NIM's `/models` (cached 10 min; 409 without a key,
    502 when NIM refuses or is unreachable). A suggestion list only.
  - `PUT /v1/providers/nim/key` `{"value":"nvapi-…"}` → **204**; `""` deletes it. Write-only: stored in
    `~/.config/biseo/providers/nim_key` (0600, directory 0700, temp file + rename), sent only to `base_url`, never
    returned or logged.
- Wake-word enrollment (full gateways only; not mounted on limited ones; `gateway/wake_enroll.py`, owned by the
  wakeword session): `POST /v1/wake/enroll` multipart `clips[]` (16 kHz mono 16-bit WAV, 0.4–2.5 s) →
  `{"accepted","rejected":[{"index","reason"}],"training"}`; `GET /v1/wake/enroll` → `{"clips","trained_at",
  "status":"none"|"training"|"ready"|"failed"}`; `DELETE /v1/wake/enroll` → 204. Clips stay on that gateway's host.
- `GET /v1/usage` → `{"month":"2026-11","by_model":{…},"by_path":{…},"by_device":{…},"cost_usd":…}`
  (all gateways together; the monthly budget is shared). API-path cost uses list prices per MTok:
  haiku 0.10/0.50, sonnet 2/10, opus 4/20 USD in/out.
- `GET /v1/status` → `{"spark":{"ok","ms"},"tts":…,"state":…,"gateway":{"device","mode","link"},
  "devices":[{"name","ok","ms"}]}`
- `POST /v1/devices/register` `{"name","url","harness":["claude_code",…]}` — daemons call
  this on start and every 60 s. `GET /v1/devices` lists them with `last_seen`.
- Tasks (full gateways only; 403 on limited): background Claude Code runs on the devices, not voice turns.
  - `POST /v1/tasks` `{"goal":"…","device"?:"macbook","persona"?:"azel","notify":"call"|"push"|"none" (default push),
    "budget_usd"?:2,"max_minutes"?:30}` → **201** the task. Limits are hard: the run is killed when its estimated
    cost (token usage × list price, replaced by the CLI's own figure at each result) reaches `budget_usd` (≤ 50) or
    after `max_minutes` (≤ 240; time spent waiting for an approval does not count). At most 2 run at once per gateway,
    in their own `claude -p` process (never the 4-process warm voice pool); later ones stay `queued`.
  - `GET /v1/tasks?limit=20` → newest first, without steps. `GET /v1/tasks/{id}` → `{"id","goal","status","device",
    "persona","notify","budget_usd","max_minutes","created","started","finished","result","error","cost_usd","owner",
    "steps":[{"at","kind":"tool","tool","label"} | {"kind":"approval"|"approval_approved"|"approval_rejected",
    "approval_id"} | {"kind":"killed","reason":"time"|"budget"|"error"}]}`. Steps never carry tool arguments or results.
    `status`: `queued|running|waiting_approval|done|failed|cancelled`.
  - `POST /v1/tasks/{id}/cancel` → the task without steps (`cancelled`; 409 once finished). Another gateway's task is
    flagged and stops within about 5 s.
  - Approvals: when a device tool answers `pending_approval`, the run stops, the task shows `waiting_approval` with
    the existing approval (`GET|POST /v1/approvals`), and resumes the same Claude session with the decision.
  - On `done`/`failed`: `notify:"call"` rings as the task's persona with the result as the opening (reason `task`),
    `"push"` sends an alert ("작업 완료" / "작업 실패"); a cancelled task sends nothing. Cost goes into usage as path
    `task`. Stored in ruchba's kv (`task-<id>`, index `tasks-index`, newest 200); a gateway restarting marks its own
    unfinished tasks `failed`.
  - Voice: "…해 줘, 끝나면 알려 줘" (push when it says 알림/푸시) or a job on a named device ("맥북 다운로드 폴더 정리해
    줘") becomes a task over the gateway link (`{"type":"task","goal","notify","persona","device"?}` →
    `{"type":"task_result","ok","task_id","say"}`), and the assistant says e.g. "맥북에서 시작할게요. 끝나면 전화드릴게요."
- Static devices (full gateways only): team devices (`tag:vujade`: vd-spark, vd-core) cannot dial a jh gateway, so
  their daemons never register. Each full gateway lists them in `~/.config/biseo/static_devices.json` (or env
  `BISEO_STATIC_DEVICES`): `[{"id":"spark","name":"vd-spark","url":"http://100.64.0.11:8893/mcp","mode":"limited"}]`
  (`mode` must be `limited`; anything else refuses to start). Each needs that daemon's own bearer token in
  `~/.config/biseo/device_tokens/<id>` (0600); without it the entry is skipped (the shared token never goes to a team
  device). The gateway probes them every 60 s and lists them in `GET /v1/devices` / `GET /v1/status` with
  `"writer":"full","limited":true,"static":true` (only in that gateway's memory, not in the shared devices table).
  A limited device is never given `delegate`: dropped from the API/NIM tool list, refused by the tool runner, and
  denied (`permissions.deny`) in `claude -p` runs. Its daemon config must also allow only read/diagnostic tools.
- `POST /v1/push/register` `{"voip_token","bundle_id","env":"sandbox"|"production","alert_token"?}` —
  the app sends its PushKit token (and optionally its ordinary APNs token, for missed-call notices).
- `POST /v1/calls` `{"reason","opening"?}` → places a call (VoIP push). Returns
  `{"call_id"}`. `GET /v1/calls/{id}` → `{"opening":"…"}` (voice server reads it).
- Personas (full gateways only; limited → 403): several assistants, exactly one `primary`. Persona:
  `{"id" (slug a-z0-9_-),"name" (1–12 Hangul/Latin/space, unique),"voice" (a TTS speaker),"voice_style"
  (optional TTS instruction, ≤200),"personality" (≤600, "" = the default),"color" ("#rrggbb", orb tint),
  "default_model" (null|haiku|sonnet|opus|nim),"share_memory" (bool, default false),"created_at","primary",
  "deleted_at"}`. Stored in the shared settings as `personas`; settings without it migrate to one persona
  `azel` (primary) holding `assistant_name` and `personality`, which stay readable (and writable) as the
  primary's for older clients. `GET /v1/personas` (`?include=deleted`), `POST /v1/personas` → 201
  (`name`, `voice` required; max 8 live personas), `GET|PUT /v1/personas/{id}` (PUT: any field but id,
  primary, created_at), `DELETE /v1/personas/{id}` (marks `deleted_at`: its calls, history and schedule
  keep `persona_id` and the name stays resolvable; the primary cannot be deleted → 400),
  `POST /v1/personas/{id}/primary`. Voices must be one of the Korean-capable set `sohee`, `ryan`, `aiden`
  and offered by the TTS server's `/v1/audio/voices` (fallback `BISEO_TTS_VOICES`; neither reachable → 503);
  anything else → 400. A stored persona on another voice (the dropped Chinese/Japanese-native speakers
  vivian, serena, uncle_fu, dylan, eric, ono_anna) is read back with `voice: "sohee"`. `DELETE /v1/personas/{id}?delete_history=true|false`
  (default false): the persona's own notes and call summaries (memory records with `meta.persona_id`) and
  its calls' follow-up contexts are always deleted; its call history is deleted with `delete_history=true`,
  otherwise it stays with `"persona_deleted": true` on each item (the app shows "삭제된 비서"). If ruchba
  cannot do that (503), the persona is not deleted either. `GET /v1/voices` →
  `[{"id","label","desc","lang"}]`, only the Korean-capable speakers the TTS server offers, with Korean labels (e.g. `{"id":"sohee","label":"소희",
  "desc":"따뜻한 한국어 여성","lang":"ko"}`). `POST /v1/personas/voice-sample` `{"voice","voice_style"?,"text"}`
  (text 1–60 chars) → `audio/wav` for the editor's "들어보기"; at most one per 2 s per gateway (429);
  502 if the TTS server cannot be reached. `assistant_name` stays a read/write alias of the primary persona's
  name.
  Calls: `/v1/call?persona=<id>` (default the primary; a placed call keeps the persona it rang as) builds
  Qwen's system prompt from that persona (name, character, reply_length) and speaks with its voice and
  style. Conversation history and the Claude session map are keyed by (persona, session). Asks over the
  link carry `"persona"`, so Claude's voice style is that persona's. Push `caller` is the persona's name.
  Briefing, deadline, alert and autonomous calls come from the primary. Every call_log start, history
  item, schedule item and proactive_log row carries `persona_id`; `/v1/history` and `/v1/schedule` take
  `?persona=`. The brain gets `"persona"` on `call_event`, `tool` and `recall` frames and may send
  `context` frames with `"persona"` (no `session` = that persona's shared block; frames without it are the
  primary's).
- Scheduled calls (full gateways only; limited → 403). Item (the same shape on the voice path):
  `{"id","at","repeat","kind","note","status","attempts","created_by","persona_id"}` with `at` an ISO 8601 string with
  offset in KST (e.g. `"2026-11-22T10:00:00+09:00"`, accepted and returned), `repeat` null | `"daily"` |
  `"weekdays"` | `"weekly:<dow>"` (0 = Monday … 6 = Sunday; must match the weekday of `at`, else 400),
  `kind` `wake`|`reminder`, `status`
  `pending`|`fired`|`answered`|`missed`|`cancelled`, `created_by` `voice`|`app`.
  `GET /v1/schedule` → pending and fired items, soonest first; `?include=all` adds past ones (answered,
  missed, cancelled). `POST /v1/schedule` `{"at","repeat"?,"kind"?,"note"?}` → **201** with the created item;
  400 if `at` is not an ISO string with offset, is past, or is more than 30 days out (or repeat/kind/note
  invalid). `DELETE /v1/schedule/{id}` → cancels and returns the item (404 unless pending or fired). Firing: the full gateway holding lease
  `scheduler` (TTL 150 s, renewed every minute, priority = `BISEO_SCHEDULER_PRIORITY`, a lower value takes
  it over) places the call at `at` (always, even with `proactive.enabled` off: the user asked for it; opening "일어나실
  시간이에요." or the reminder note), retries every 2 min while unanswered up to 3 attempts, then marks it
  `missed` and sends an alert push. An item more than 10 min past `at` when the scheduler sees it (the
  holder was down) is never rung: it becomes `missed` (repeats move to their next occurrence) with an
  alert push ("…를 제때 걸지 못했어요"). Answered (voice `call_answered`) → `answered`; repeating items go back
  to `pending` at their next occurrence (also after a miss). Status changes are compare-and-set, so two
  gateways never fire the same attempt.
- Autonomous calls ("서버가 생각해서 알아서 부르기"; `proactive.enabled` and `proactive.autonomous.enabled`,
  the latter off by default):
  - Checkpoints: at each `autonomous.checkpoints` time (default 08:30, 13:00, 18:00) one full gateway
    with Claude (lease `job:checkpoint:<date>:<HH:MM>`) runs one `claude -p` that reads GISTRA, Planit,
    Calendar and Gmail and writes a compact note (what is due within 48 h, what needs the user, urgency 1–5
    per line) to the memory service's task context `azel-checkpoint`. About 3 runs a day. The brain
    (`assistant/brain`) merges it with other state into `azel-situation`; the decision reads
    `azel-situation`, or `azel-checkpoint` when that is missing or older than 6 h.
  - Decision: at :00 and :30 the scheduler lease holder gathers signals without Claude (critical alerts
    of the last 24 h, delegated device jobs finished or failed in the last 2 h via each trusted daemon's
    `list_jobs`, approvals pending > 30 min, scheduled calls missed in the last 24 h, task contexts with
    open questions, the situation note) and asks the local Qwen for `{"call","urgency","reason","opening"}`.
  - Fail closed: if the decision log cannot be read (ruchba down, 404, error) nothing rings, and a call is
    rung only after its log row (with the pre-generated `call_id`) is written.
  - Guardrails, all required to ring: `call` is `true`, `urgency` an integer ≥ 4, now inside `window`,
    fewer than `max_per_day` autonomous calls today, the last one at least `min_gap_minutes` ago, and no
    autonomous call about the same reason (hash of the normalized reason) in the last 24 h. The call opens
    with Qwen's `opening` (≤ 200 chars).
  - Every decision is logged, called or not: `GET /v1/proactive/log?limit=50&since=<epoch>` → newest first,
    `[{"id","at","call","urgency","reason","opening","reason_hash","called","blocked_by"
    (no_call|urgency|window|max_per_day|min_gap|duplicate|null),"signals","call_id"}]` (full only).
- Call history (full gateways only; limited → 403), stored in ruchba (section 5):
  `GET /v1/history?limit=20&before=<epoch>&q=` → `[{"id","session","title" (first user utterance),
  "last_reply" (last assistant reply, first 120 chars),"started_at","duration_s","direction","reason",
  "client","turns","routes":{"qwen":n,"claude":m},"kinds":["calendar",…]}]`, newest first. With `q`:
  full transcripts are searched with the memory records' hybrid vector + text ranking (calls still in
  progress by substring). Calls whose words match come first; meaning-only matches (vector, no word match)
  are listed only when no call matches the words, and need cosine ≥ 0.9. Each item adds `"match"`: a short
  snippet of the matching line;
  `GET /v1/history/{id}` → the call with `"turns":[{"at","user","assistant","route",
  "activity":[{"label","kind","at"}],"timings","interrupted"}]`, every `at` in seconds from the call's
  start, computed by the memory service; `DELETE /v1/history/{id}`; `DELETE /v1/history?older_than_days=N`. Setting
  `history_days` (default 365, 0 = forever): a full gateway deletes older calls daily at 04:00 (lease
  `job:retention:<date>`). Gateways write the voice server's `call_log` events through a restart-proof
  outbox (`pending_history.json`).
- `POST /v1/alerts` — Alertmanager webhook; critical + firing → a call (if `proactive.enabled` and
  `server_alerts`), about the alert names not called about in the last 2 h (lease `alert:<name>`, so a
  flapping alert rings once), and at most 3 server-down calls per rolling 24 h (kv `alert_calls`). If
  ruchba is down the gateway applies the same limits from its own memory and calls.
- Scheduled jobs (morning briefing, deadline check) run on every full gateway's clock; the one that
  takes lease `job:<name>:<date>` (24 h) fires it. Lease holder = `<device>-<random per process>`,
  so a restarted gateway cannot fire the same job twice. No lease service → the job is skipped.
- `GET /v1/approvals` / `POST /v1/approvals/{id}` `{"approve":true|false}` — risky
  daemon actions waiting for the user (the app shows them).

## 4. Agent daemon (MCP, streamable HTTP at `/mcp`)

Registration body: `{"id":"macbook","name":"macbook","url","harness":[…]}` — `id` and `name` are the same English device name (macbook, albireo, gienah, deneb, spark); `id` is an ASCII
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
  "cost_usd","ts"?,"import_key"?}` (a row whose `import_key` is already stored is skipped: replays from the
  old gateway.db are idempotent); `GET /v1/state/usage?since=<epoch>` *limited* → rows, oldest first.
- `PUT /v1/state/devices/{id}` *limited* `{"name","url","harness","gateway"}` (upsert, sets
  `last_seen` and `writer` = the caller's role, `full` or `limited`). A limited caller may not overwrite a
  row whose writer is `full` (403). `GET /v1/state/devices` *limited* →
  `[{"id","name","url","harness","gateway","writer","last_seen"}]`. Full gateways use (and send the shared
  token to) only rows with `writer: "full"`; limited gateways only rows with their own `gateway`.
- `PUT /v1/state/push_tokens/{token}` `{"bundle_id","env","kind":"voip"|"alert"}`; `GET /v1/state/push_tokens`.
- Autonomous calls (full callers only): `POST /v1/state/proactive_log` (one decision, fields as in
  section 3) → `{"id"}`; `GET /v1/state/proactive_log?since=<epoch>&called=true|false&limit=100` → newest
  first. `GET|PUT /v1/state/kv/{key}` (key `[a-z0-9_]{1,40}`, not `settings`): small shared JSON values,
  e.g. `alerts_recent` (critical alerts of the last 24 h, kept by the gateways for the decision).
- Scheduled calls (full callers only; internal, epoch seconds; gateways convert to the `/v1/schedule`
  shape): `POST /v1/state/schedule` `{"at": epoch,"repeat","kind","note","created_by":"voice"|"app"}` → the row; `GET /v1/state/schedule?status=pending,fired`; `POST /v1/state/schedule/{id}`
  `{"status"?,"attempts"?,"call_id"?,"at"?,"last_attempt"?,"expect_status"?}` (compare-and-set on
  `expect_status`: 409 if the row moved on); `DELETE /v1/state/schedule/{id}` → cancelled (404 unless
  pending or fired). Table `scheduled_calls` in the state schema.
- `POST /v1/state/calls` `{"id","reason","opening"}`; `POST /v1/state/calls/{id}/answer` → the row,
  marked answered (404 if missing).
- `POST /v1/state/approvals` *limited* `{"id"?,"device","tool","command","cwd"}` → `{"approval_id"}` (`id`,
  12 hex chars, only from full callers, for migrated approvals; an existing id is left unchanged);
  `GET /v1/state/approvals?status=pending`; `GET /v1/state/approvals/{id}` *limited*;
  `POST /v1/state/approvals/{id}` `{"approve":bool}` → `{"id","status"}`, 409 unless pending.
- `GET|PUT /v1/state/sessions/{voice_session}` `{"claude_session","device"}` (404 if unknown).
- `POST /v1/state/leases/{name}` `{"holder","ttl_s","priority"?}` (0 < ttl ≤ 7 days, priority default 100)
  → `{"held","holder","priority","expires"}`. Atomic: taken if free or expired, renewed if `holder`
  already holds it, taken over if `priority` is lower than the holder's, else `held:false` with the
  current holder. `DELETE /v1/state/leases/{name}?holder=` → `{"released":bool}`.
- Call history (full callers only; personal): `POST /v1/state/history/{id}/start` (body as the voice
  server's start event), `POST /v1/state/history/{id}/turns` (a turn; `user` must be non-empty),
  `POST /v1/state/history/{id}/end` `{"ended_at","duration_s"}` → `{"ok","turns"}`, which also files the
  whole transcript as a `transcript` record (`meta.call` = id) so `/v1/search` finds past calls;
  `GET /v1/state/history?limit&before&q` (q matches opening, user and assistant text),
  `GET /v1/state/history/{id}`, `DELETE /v1/state/history/{id}` and
  `DELETE /v1/state/history?older_than_days=N` (also delete those calls' transcript records). Tables
  `call_history` and `call_turns` (on delete cascade) in the state schema; the existing `calls` table
  stays the placed-call openings.

## 6. Brain — the resident assistant (`brain/`, deneb; the Mac for development)

One process on an always-on full (jh-only) host. It listens on nothing: it dials the voice server's gateway
link and talks to the memory service and the local Qwen (`http://100.64.0.11:8888/v1/chat/completions`). Tokens:
`token` (memory service, gateways' `/v1/ask`) and `link_token`, both 0600. One brain writes at a time (lease
`brain`, 120 s); a second brain only serves its link. Background Qwen requests are paced to one per
`BRAIN_BG_GAP_S` (default 4) seconds.

Link (section 1b), its own connection: hello `{"type":"hello","device":"brain-<host>","mode":"full","claude":false,
"role":"brain","priority":99}`. `role:"brain"` counts only on a full (`link`-token) link. A brain link never gets
asks, call_logs or schedule requests.
- brain → voice `{"type":"context","session"?,"value"}`: the Korean context block (≤ 2400 chars: profile,
  situation, recent calls), pushed on connect and whenever it changes (situation refresh every 5 min). With
  `session`: that call only (a placed call adds its reason). The voice server uses a block younger than 15 min, only
  in Qwen's SYSTEM; it is never sent to any link.
- voice → brain `{"type":"call_event","event":"start"|"end","session","reason"?,"opening"?}` (a placed call's); the brain answers a start with
  that session's context.
- voice → brain `{"type":"tool","id","name","args"}` → `{"id","type":"tool_result","result":"…"}` (Korean, for Qwen).
  Tools: `search_memory{query}` (records `note`+`transcript`), `add_todo{text}` / `done_todo{text}` (context
  `azel-todos`), `device_status{}`, `situation{}`. The voice server runs `scheduled_calls{}` itself (section 1b
  schedule list). Timeout 3 s.
  Cygnus tools (when the brain has a Cygnus PAT, `~/.config/biseo/cygnus_pat`, 0600, read-only): the brain's hello adds
  `"tools": [names]` (the voice server then forwards exactly those names, else the five above), and each persona's
  block lists them. Curated read-only set, arguments validated by the brain: `gistra_today{date?}`,
  `gistra_schedule{from,to,kinds?}` (≤ 31 days), `gistra_assignments{course?}`, `planit_get_day{date?}`,
  `planit_search_tasks{query}`, `memory_recall{query}` (personas with share_memory only), `infra_health{}`,
  `infra_status{}` (problems only), `infra_alerts{}`. Results are trimmed to 1500 chars and cached for 60 s. With
  `infra_health` listed, a server *status* question is no longer pre-routed to Claude (an action on a server still is).
- voice → brain `{"type":"recall","id","text"}` → `{"id","type":"recall_result","hits":["…"]}` (≤ 2 memory hits);
  the voice server waits at most 400 ms, then answers without them.

Qwen with a brain context: personal questions are no longer pre-routed to Claude (live data, devices by name,
code and file work, and named models still are). Qwen answers from the context, or prints one line
`[TOOL] {"name":…,"args":{…}}` (the voice server runs it and asks Qwen again; at most two tools per turn), or
`[CLAUDE]` as before. Without a brain (no link, or no context younger than 15 min) routing and the prompt are as
before.

Memory written by the brain (no schema change; record kind `note`, contexts):
- after every ended call with ≥ 12 characters of user speech: a `note` record `"[통화 요약 <when>] <summary> 결정: …
  후속: …"` with `meta {"type":"call_summary","call","title","at","decisions","facts","followups"}`; follow-ups as
  context `call-followup-<call>` (`open_questions` = follow-ups, which the gateways' autonomous
  signals read); facts merged into `azel-profile` by Qwen.
- `azel-profile`: stable facts and preferences, seeded once from Cygnus memory through a full gateway's
  `/v1/ask` (Claude), then refined from calls.
- `azel-day`: Claude's day digest (today and tomorrow, deadlines within 7 days, today's todos, mail needing a
  reply) through `/v1/ask` at `BRAIN_DIGEST_AT` (default 07:30, 12:30, 17:30) unless one is younger than 2.5 h.
- `azel-situation`: composed without a model from `azel-day`, `azel-checkpoint` (the gateways' autonomous-call
  checkpoint note), scheduled calls, pending approvals, full-writer devices, open call follow-ups (3 days),
  `azel-todos`, and recent call summaries (3 days). First line `## 상황 (…)`; a note there without it is a gateway's
  checkpoint and is moved to `azel-checkpoint`.
- Persona data (full callers only): `DELETE /v1/state/personas/{id}/data?history=true|false` → deletes records
  with `meta.persona_id` = id, the `call-followup-<call>` contexts of its calls and its todos (`azel-todos-<id>`); with `history=true` also its
  calls (and their transcript records), otherwise flags them `persona_deleted` → `{"notes_deleted","calls",
  "history_deleted"}`. History items carry `persona_deleted`.
