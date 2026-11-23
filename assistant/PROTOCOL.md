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
  - `{"type":"hello","caps":["text","played","mode","secret"]}` — always the very first message: the client messages this
    server understands beyond audio, `hangup` and `interrupt`. The app shows the keyboard button only when `text` is
    listed (older servers send no `hello`).
  - `{"type":"state","value":"listening"|"thinking"|"speaking"}` — `speaking` lasts until the client has
    played every byte sent (the server tracks that from the audio length, pauses included) plus 300 ms
    (`PLAY_TAIL_MS`), not just until generation ends; only then come `turn_done` and `listening`. Speech in
    that window is a barge-in (`pause`/check), never a new turn.
  - `{"type":"context","context":{"kind":"task"|"schedule","label":"<short purpose>","id":"<task or schedule id>"}}` —
    the first message after `hello` of a call the assistant placed for a task (its finish or approval) or a reminder;
    nothing else comes between them. The same object rides in that call's VoIP push as `context`. For the call screen's
    "맡긴 작업: …" / "예약: …" chip. Absent on other calls.
  - `{"type":"transcript","text":"…"}` — what the user said
  - `{"type":"reply_start","reply_id":N}` — sent right before the first audio frame of each reply (an answer, a
    briefing, a placed call's opening); `N` increases per reply. Binary frames carry no id, so the app treats every
    binary frame after a `reply_start` as belonging to that reply: on a `reply_start` (or an `interrupt`) it drops
    whatever is still queued from an older reply. The server guarantees no frame of an interrupted or superseded
    reply is sent after its `interrupt` (each reply has a generation; an interrupt bumps it and cancels and awaits
    the old reply's TTS, Qwen and Claude work before sending `interrupt`).
  - `{"type":"reply","text":"…","route":"qwen"|"claude"}` — text of the audio chunk that follows: one or
    more whole sentences (or a clause ending in ", "), never under 5 syllables unless it is the whole reply,
    never cut mid-word. Chinese characters, kana, Cyrillic and Thai are dropped from it (and from the audio
    and history). Model reasoning (`<think>…</think>`, a stray `</think>` with everything before it, an unclosed
    `<think>`, any `reasoning_content` field) is never spoken, captioned or stored, from Qwen, Claude or NIM alike;
    thinking stays on, and the call stays in `thinking` meanwhile. A reply that was only reasoning is answered with a
    short "생각이 길어져서 답을 못 정했어요. 한 번만 더 말씀해 주시겠어요?".
  - `{"type":"pause"}` — the user may be talking over the assistant: **pause playback, keep the queue**
  - `{"type":"resume"}` — it was noise or not a real interruption: continue the paused queue where it stopped
  - `{"type":"duck"}` / `{"type":"unduck"}` — only on a call opened with `?duck=1`, instead of `pause` / `resume`: the user may
    be talking over the assistant, so **keep playing at about 20 % volume**; `unduck` restores it (noise or echo),
    `interrupt` (a real cut-in) drops the queue and restores it too. Apps without `duck=1` get `pause` / `resume`.
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
    For a spoken turn, `timings.turn` is how the turn's speech arrived (diagnostic, also printed as a `turn-end`
    line in the voice server's log): `{"by": "quiet"|"drop"|"interrupt"|"hangup", "audio_s", "wall_s", "max_gap_s",
    "gap_at_s", "voiced_s"}`. `audio_s` is the audio received after speech began, `wall_s` the time from speech onset
    to the turn's end, `max_gap_s` the longest gap between inbound audio messages, and `gap_at_s` when that gap began
    (seconds into the call). A turn ends only after 500 ms of quiet frames, so a late end with a long `max_gap_s`
    means audio stopped arriving.
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
- iPhone data (PHONE_DATA.md §3a), iPhone only. Client → server `{"type":"phone_hello","kinds":[…]}` right after
  connecting: the categories the phone answers in this call (`health`, `sleep`, `workouts`, `calendar`, `reminders`,
  `contacts`, `location`, `now_playing`); `[]` in a secret call. Server → client `{"type":"phone_query","id","kind",
  "args"}` (a gateway's question, relayed); client → server `{"type":"phone_result","id","ok","data"?,"error"?}`
  (`error`: `off` = the category is off, `denied` = no iOS permission, `failed`). The voice server keeps none of it.
- Client → server **text**: `{"type":"hangup"}` (or just close); `{"type":"interrupt"}` (tap-to-interrupt):
  handled like a voice barge-in, so the current reply and its Claude run are cancelled and the server
  answers `{"type":"interrupt"}` then `{"type":"state","value":"listening"}` (every interrupt, voice or tap, ends
  with exactly that pair; a cancelled reply's own `listening` is dropped, and an interrupt overtaken by a newer one or
  by a new reply sends nothing). During a placed call's
  opening the first sentence still finishes first.
- Client → server `{"type":"text","text":"…","speak":true|false}` — the user typed during the call. It is a user
  turn exactly like a transcribed utterance (same routing, tools, persona, history and call log, the log's turn
  marked `"input":"text"`), without STT or VAD, and the server sends no `transcript` for it (the app shows the
  typed line itself). Trimmed; empty is ignored; longer than 1000 characters, or within 1 s of the previous typed
  message, is answered `{"type":"text_rejected","reason":"too_long"|"too_fast"}` and nothing else happens. While
  the assistant speaks or thinks it interrupts first, like a tap (`interrupt`, then the new reply). `speak:false`
  (default true): the reply comes as `reply` captions and `turn_done` with no audio and no filler lines. The mic
  stays live: speech during a typed turn is handled as usual.
- **The assistant hangs up**: server → client `{"type":"hangup","reason":"<short text, may be empty>"}`, then the server
  closes the socket. It comes after the goodbye the assistant just said has finished playing ("네, 끝나면 전화드릴게요."),
  when the user said "끊어", "이제 됐어 끊어", "끊을게" or "맡겨 놓고 끊어" (the model's `end_call` action; never while a
  confirm/clarify question waits for an answer, never from a task). The app ends the call as if the user hung up; the
  reason is for logs, the app need not show it.
- **Continuing a task from its finish call**: an instruction given in a task's finish call (within 3 h, e.g. "그거 이렇게
  고쳐서 다시 해 줘") creates a continuation task: `parent_task` = the finished task, the same device, notify inherited, and
  when the parent delegated, `resume` = that device's Claude Code session (`session_id`, `cwd` from the job), so the
  device's Claude Code keeps its own context (`<device>__delegate` with `resume`, same `cwd`). Questions in the call are
  answered from the result, as before. A task that delegates waits for the job (`job_status`) before it counts as done.
- **Approval pushes** carry `aps.category = "APPROVAL"` (허용 / 거부 / 항상 허용 buttons) with `approval_id`, and
  `forced_once: true` when "always" can't hold (a command that writes, or a secret call): hide that button.
- **Administrator (sudo) approvals** (user decision 2026-11-23): a `run_command` whose whole command is `sudo <command>`
  can run, but only after the user taps 허용 on that one request, every time. The daemon runs it as `sudo -n …` (it never
  types or stores a password: it works only where the user set NOPASSWD for that command; otherwise the result carries
  `error`: "이 기기는 sudo에 비밀번호가 필요해서 …"). Other sudo options (`-u`, `-s`, `-i`, `-E` …), sudo inside another
  command (`env sudo`, `xargs sudo`, `sudo sudo`), su/doas/pkexec, and what the command under sudo would be refused for
  (shells with `-c`, secret paths, `rm -rf ~` …) stay refused before any approval. A delegated `claude -p` never gets sudo.
  - Never approved without the tap: not by `approval_mode: "auto"`, the classifier, an auto-approve grant (call or task)
    or a stored "always" rule. Always once: "항상 허용" becomes once with `forced_reason` "관리자 권한(sudo) 명령은 …".
  - Push: title "관리자 권한(sudo) 승인 요청", `aps.category = "APPROVAL_RISKY"`, with `type: "approval"`,
    `approval_id`, `sudo: true`, `tier: "sudo"`, `forced_once: true`.
  - `GET /v1/approvals` and `GET /v1/approvals/{id}` items carry `tier: "sudo"`, `sudo: true`, `forced_once: true` and
    that `forced_reason`. The daemon's `needs_approval` (team devices) carries `sudo: true`; the spoken line is
    "관리자 권한(sudo) 명령이에요. 앱에서 명령을 확인하고 직접 허용해 주세요."
  - **App (next install):** register `APPROVAL_RISKY` with two actions, 허용 (`.authenticationRequired`,
    `.destructive`) and 거부, and no 항상 허용. Show a sudo item highlighted on iPhone, Mac, the Live Activity and
    the watch notification: warning colour, a "관리자 권한" label, the full command in monospace, no 항상 허용. Use
    `tier == "sudo"` (or `sudo == true`); older gateways never send it.
- Team devices (vd-spark, vd-core) may be handed a `delegate` (their own Claude Code); only `sadr` never is.
- **Auto-approve** (like Claude Code's auto mode, only when the user grants it): while a grant is live, a device
  command whose run (this call, or a task started in it, or a task granted directly) asks for approval runs at once
  when it classifies **safe**; **risky** ones still need a tap, and the daemons' hard_deny is unchanged. Browser-tool
  approvals (`tool: "browser"`: a committing click, a first print) are never auto-approved.
  - Grant from the call: client → `{"type":"auto_approve","value":true|false,"minutes"?,"devices"?}`; the server answers
    `{"type":"auto_approve","value","expires_at"}` when the gateway confirms, and every `state` frame carries
    `"auto": true|false`. `hello.caps` lists `"auto_approve"`. The grant ends at hangup, after `minutes` (≤ 120), or on
    `value:false`. By voice: the model's `auto_approve` action, only for the call's own ask (never a task), refused
    right after a web, mail, Cygnus or memory read in the same run, and always read back once ("이번 통화 동안 위험하지
    않은 명령은 묻지 않고 바로 실행할게요. …맞죠?").
  - Gateway REST (app): `POST /v1/auto-approve` `{"session"|"task","devices"?,"minutes"?}` → `{"id","scope","devices",
    "by","created","expires_at","ran"}`; `GET /v1/auto-approve` (live grants); `GET /v1/auto-approve/{id}` (with `log`:
    device, command, at, stage, also after it ended); `DELETE /v1/auto-approve/{id}` → 204. A task's grant ends with
    the task.
  - Tiers (`gateway/risk.py`): deterministic rules first (reads, status, a single named service or container restart,
    files inside the working directory, `open -a` → safe; any delete (rm, rmdir, unlink, trash), recursive or mass delete, service stop/disable/reboot,
    installs, users and permissions, sending data off-host, secrets-adjacent paths, git push/force, docker rm/prune/
    volume rm, kill -9, osascript and the like → risky), then the local Qwen, then Haiku (never in local brain mode),
    then risky. Cached per command+cwd+device; the gateway log says which stage decided.
  - Audit: the approval's meta has `decision: "auto"`, `grant_id`, `stage`; the grant keeps the list. When a grant with
    runs ends, a push "자동으로 실행한 명령 N개가 있어요" (type `auto_summary`, `grant_id`; no command text on the lock
    screen). Secret calls: the grant works but lives in memory only, is never listed, and sends no summary.
- **Secret calls**: `/v1/call?secret=1`, or `{"type":"secret","value":true}` during a call (the server answers
  `{"type":"secret","value":true}`; turning it off is refused with the same frame: a secret call stays secret). Every
  `state` frame carries `"secret": true|false`. In a secret call:
  - nothing is stored: no call log (`/v1/history`), turns, transcript or memory records, no post-call facts, no brain
    call events; the call's turns live under a throwaway session key and are dropped at hangup, never joining the
    persona+session history, and no task or call links back to it (no `origin_session`);
  - Claude on the `claude -p` path runs cold with `--no-session-persistence`, no `--resume`, and no stored
    `claude_session`; the API and NIM paths keep no state anyway;
  - reading is allowed (memory_recall, people_lookup, web, device reads, Cygnus/Google reads, music); anything that
    stores is refused with "시크릿 모드라서 저장하는 일은 못 해요. 시크릿을 끄고 할까요?": remember, forget,
    schedule_*, create_task, call_user with a time or rule, the voice fast paths for scheduling and tasks. A device
    command's approval still works once; "always" becomes once (`forced_reason`). Limit: the approval row itself
    (device, command, cwd) is stored like any other, because the user must see the command to approve it;
  - on the `claude -p` path a secret run loads no claude.ai or custom connectors (only the device daemons and azel),
    so nothing can write through Cygnus, Gmail or Calendar;
  - usage rows record cost only (model, path, tokens, cost; never text), as they always do;
  - a call made secret midway keeps what was already logged; from then on nothing more, except the call log's end
    time and the brain's end event, under the original session.
  Limit: text sent to Anthropic (Claude) or NVIDIA (NIM) is processed under their own retention policies; the local
  Qwen and Whisper keep nothing.
- Client → server `{"type":"played","reply_id":N}` — the phone finished playing reply N's last buffer (none for an
  interrupted reply). For the reply whose audio went out last, playback counts as ended now; once a client has sent
  one, the server waits for it after each reply, up to 2 s past its own estimate.

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
  `{"type":"call","call_id","opening","reason","persona","origin_session"?}` (sent before the VoIP push; accepted
  from any full-mode link, ignored without a string `call_id`; the voice server synthesizes the opening on arrival).
  Every placed call's opening starts with the user's title in effect ("정훈님, …"), whoever wrote it.
  `context` `{"kind","label","id"}` (task and reminder calls): passed on to the app as the call's first message.
  `origin_session` (`persona:session`, `[A-Za-z0-9_:.-]{1,120}`): the conversation this call continues. A task's
  finish/approval call and a reminder made during a call carry it, and so does `call_user` now from a call. When the
  call connects, its history starts with that conversation's last 8 turns, then the opening (which holds the task
  result), so Qwen and Claude both answer with that context. (In memory on the voice server: a reminder that fires
  after a voice-server restart starts without it.)
  `detail` (task finish/approval calls, ≤ 4000 chars): the task's goal and whole result. The opening is only a short
  spoken briefing (title once, "맡기신 ○○ 끝났어요", the key findings, one line on what is missing, a closing; made by
  the local Qwen, else the result's first sentences), so the call's history also gets the whole result, marked as
  such, and follow-up questions are answered from it. A placed call whose opening never arrived still speaks a
  fallback line, never silence. During a placed call's opening (from connect until it has played), "여보세요", "네",
  "어 왜", "누구세요", the assistant's name and the like are never a turn and never cut it; only a stop word or a real
  question interrupts, anything else is answered after the opening.
  `{"type":"announce","id","session","opening","detail"?,"context"?}` (full links only): a task finished while its
  user is on the call `session` (`persona:session`). The voice server speaks `opening` there as its own turn once the
  call is quiet (captions route `task`, the context chip first) and answers `{"type":"announce_result","id","ok"}`;
  `ok:false` (no such live call) and the gateway rings instead. `ok:true` only means taken: once the announcement has
  actually played the voice server sends `{"type":"announced","id","ok":true}`; cut off, hung up first, or still
  queued at hangup → `ok:false`, and the gateway rings (it waits up to 10 minutes for the ack). While the user is on
  another call, the gateway holds the finish call until the line is free, rings held calls one at a time (20 s
  apart), and rings anyway after 3 minutes. A call counts as in progress from its call_log start to its end, at most
  2 hours, and none do while the voice link is down. A task records `notified_at` once its user was told (in the call,
  by a call or by a push); at startup a gateway tells again its own finished notify=call/push tasks from the last 6
  hours that have none. Every finish outcome is logged on the gateway (`task notify: …`).
  A follow-up "그 작업 끝나면 전화해 줘" changes the notify of the newest unfinished task from the same call only;
  otherwise 409 "어떤 작업 말씀이세요?". `task_set_notify` for a task running on another gateway updates its state row
  only (its runner keeps the old choice); fine while only the Mac runs tasks.
- Requests the voice server sends over the link to create something (`tool_call`, `schedule`, `task`) carry
  `origin_session` = the conversation being answered; the gateway keeps it on the task (`origin_session`) or on the
  reminder (kv `origin-<schedule id>`).
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
- gateway → voice: `{"type":"phone_query","id","kind","args"}` (full links only; PHONE_DATA.md §3a). The voice server
  passes it to the latest call whose `phone_hello` lists `kind`, and the phone's `phone_result` back to the link that
  asked as `{"id","type":"phone_result","ok","data"?,"error"?}`; with no such call it answers at once with
  `"ok":false,"error":"offline"`. Nothing is stored or logged.
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
  `POST /v1/approvals/{id}`, `GET /v1/approvals/rules`, `DELETE /v1/approvals/rules/{id}`, `GET /v1/approvals/audit`,
  `POST /v1/push/register`, `GET|PUT /v1/connectors`, `PUT /v1/connectors/secret/{name}`,
  `GET|DELETE /v1/history`, `GET|DELETE /v1/history/{id}`, `GET|POST /v1/schedule`, `DELETE /v1/schedule/{id}`,
  `GET /v1/proactive/log`, `GET|POST /v1/personas`, `GET|PUT|DELETE /v1/personas/{id}`,
  `POST /v1/personas/{id}/primary`, `POST /v1/personas/voice-sample`, `GET /v1/voices`,
  `GET|POST|DELETE /v1/wake/enroll`, `GET /v1/providers/nim/models`, `PUT /v1/providers/nim/key`,
  `GET|POST /v1/tasks`, `GET /v1/tasks/{id}`, `POST /v1/tasks/{id}/cancel` (bearer header);
  `GET /v1/tv` (the TV token; needs adding to the proxy's list).

Never exposed: `/v1/gateway-link` (also refused by the voice server, section 1b), `/v1/ask`,
`/v1/devices/*`, `/v1/calls*`, `/v1/alerts`, the daemons, the memory service.

## 2. One-shot turn — `POST /v1/turn` on the voice server (testing, benchmark)

Body: 16 kHz mono WAV. Response: NDJSON (`transcript`, `audio` with base64 `pcm16`,
`done`). Kept for the benchmark; apps use `/v1/call`.

## 3. Gateway API (`http://<host>:8892`, every device)

Everything the assistant *does* (calls, schedules, tasks, approvals' status, device and read tools) is an action defined
once in the gateway's registry, with the same contract on every surface: see [ACTIONS.md](ACTIONS.md). The schedule,
task and call routes below are thin adapters onto those actions.

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
   "monthly_budget_usd":500,"budget_warn_pct":80,"budget_paths":["api","nim"],
   "proactive":{"enabled":true,
                "morning_briefing":{"enabled":true,"time":"08:00"},
                "deadline_check":{"enabled":true,"time":"20:00"},
                "server_alerts":true,
                "autonomous":{"enabled":false,"max_per_day":3,"min_gap_minutes":120,
                              "window":["08:00","23:00"],"checkpoints":["08:30","13:00","18:00"]}},
   "default_device":"macbook","device_fallback":["albireo","spark"],"assistant_name":"아젤",
   "history_days":365,"reply_length":"normal","noise_mode":"normal","personality":"","user_title":""}
  ```
  `user_title`: how the assistant addresses the user, 0–12 Hangul/Latin letters or spaces (whitespace collapsed,
  null → `""`); `""` = no fixed title, address the user naturally. A persona's own `user_title` overrides it. The
  title in effect goes into Claude's persona block and Qwen's prompt as "사용자를 '<title>'(이)라고 부른다." (left out
  when empty). A spoken briefing's first sentence also starts with the title.
  Speech style: every prompt says "말투(존댓말/반말)는 비서 성격을 따르고, 성격에 정해져 있지 않으면 존댓말로 말해." instead of a
  fixed 존댓말, so a persona whose personality asks for 반말 speaks 반말 (Qwen, Claude, NIM, openings).
  Late at night (00:00–05:00 KST) every model prompt (Qwen's, Claude's voice style, the API/NIM paths, tasks) carries
  `late_night.prompt_rule`: "내일" means today until 05:00, with a read-back for afternoon times and a question for
  morning times, the same rule `when_from_words` applies to scheduling and spoken cancels. Persona and settings changes reach the voice server at once over the link (every change pushes the
  whole settings), and each call takes its persona snapshot when it starts. Because the app reuses one session id per
  day, a change of the persona's name or title also starts that conversation fresh: the voice server drops the
  persona+session history it built under the old name/title, and the gateway keys the resumed Claude session by
  session + a hash of name and title.
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
  `holdings` (the TV's 내 종목 and the `portfolio` tool): send only this key, `PUT {"holdings": [...]}`, the whole list
  in display order (add, remove, rename and reorder are all just a new list). Each entry is `"이름 (CODE)"` or a bare
  name, 1–40 characters, no control characters, at most 12; otherwise 400. An entry already saved, or one with a
  `(CODE)` (a rename that keeps its code), is stored as sent and never looked up again. A new bare name is resolved
  like the voice tool: one listing → saved as `"이름 (CODE)"`; none → saved as typed (비상장); several → saved bare and
  listed in the response's `candidates` (`{"삼성": ["삼성전자 (005930)", "삼성SDI (006400)"]}`, `{}` when none), so the
  app asks which one and PUTs again with `"이름 (CODE)"`. The response's `holdings` is what was saved; the TV and
  `portfolio` show it at once.
- Connectors: which MCP servers a full gateway's Claude runs load (warm or one-shot). Shared setting `connectors`
  (also writable as part of `PUT /v1/settings`):
  ```json
  {"claude_ai": {"Gmail": true, "Notion": false},
   "custom": [{"name": "wiki", "url": "https://wiki.example/mcp", "transport": "http", "enabled": true, "auth_ref": "wiki", "allowed_tools": ["get_*"]}]}
  ```
  `claude_ai` keys are claude.ai connector display names without the `claude.ai ` prefix; a name not listed takes its
  default (Cygnus MCP, Google Calendar, Gmail on; everything else off). `custom`: ≤ 20 servers, `name` a unique ASCII
  slug (`[A-Za-z0-9_-]{1,40}`, also the tool prefix `mcp__<name>`), `url` https (plain http only to a tailnet
  address, 100.64.0.0/10), `transport` `http`|`sse` (default http), `enabled` (default true), optional `auth_ref`,
  which must equal the server's own `name` (a server never uses another's secret), and optional `allowed_tools` (≤ 50
  tool names or globs over `[A-Za-z0-9_.*?-]`; empty or missing = every tool): a run is allowed only those tools of the
  server (`mcp__<name>__<pattern>`), the rest are refused. A `PUT` entry without `allowed_tools` keeps the list already stored for that name (`[]` clears it). Secrets never go into settings. Each
  run also loads the device daemons, no plugin MCP servers, and every tool up front (no ToolSearch). A custom server
  whose secret file is missing or not 0600 is skipped. Full gateways only; limited → 403 on every route below.
  - `GET /v1/connectors[?refresh=true]` → `{"claude_ai":[{"name","enabled","status":"connected"|"needs_auth"}],
    "custom":[{"name","url","transport","enabled","has_secret","allowed_tools"}]}` (the app's pinned shape plus `allowed_tools`). The claude.ai list is
    this gateway's login as `claude mcp list` sees it (cached from startup; `refresh=true` re-reads, ~15 s); anything
    not connected is `needs_auth`.
  - `PUT /v1/connectors` with the same shape without `status` / `has_secret` → the GET shape (400 with a reason on a
    bad shape). Replaces both lists; stored as the setting above, a custom server's secret named after the server.
  - `PUT /v1/connectors/secret/{name}` `{"value": "<token>"}` (a raw token is sent as `Authorization: Bearer <token>`)
    or `{"value": "{\"X-Api-Key\": \"…\"}"}` (a JSON object of headers) → **204**. Write-only: stored in
    `~/.config/biseo/mcp/<name>` (0600) on **this** gateway only, never returned by any route; restarts this gateway's warm
    Claude processes. Each full gateway that should use the server needs its own copy.
  - **OAuth connectors** (`gateway/oauth.py`, PKCE S256): built-in providers (`spotify`) and custom servers marked
    `"oauth": true` in `custom[]` (endpoints discovered from the server's 401: RFC 9728 protected-resource metadata,
    then RFC 8414/OIDC metadata; client id pre-registered, else RFC 7591 dynamic registration). The GET shape adds
    `"oauth": {"<name>": "needs_consent"|"connected"|"expired"}` and `"oauth": true` on such custom servers.
    - `POST /v1/connectors/{name}/authorize` `{"client_id"?, "redirect_uri"?}` → `{"url", "state", "redirect_uri",
      "expires_in": 600}`; the app opens `url`. `redirect_uri`: `azel://oauth/callback` (default), a loopback
      `http://127.0.0.1:<port>/…`, or this gateway's `GET /v1/oauth/callback` (tailnet, or `BISEO_PUBLIC_URL`'s HTTPS
      address through the proxy). A client id, once given, is stored with the grant. 404 unknown name, 400 bad input
      (e.g. no client id for a provider without registration).
    - `POST /v1/connectors/{name}/authorize/finish` `{"code", "state"}` → `{"name", "status"}`: what the app sends
      after its scheme or loopback redirect. The state is single-use, 10 minutes.
    - `GET /v1/oauth/callback?code&state` (no token: the pending state is the check) → a short HTML page.
    - `GET /v1/connectors/{name}/oauth` → `{"name", "status"}`; `DELETE /v1/connectors/{name}/oauth` → 204
      (forgets the grant).
    Tokens live in `~/.config/biseo/oauth/<name>.json` (0600) on this gateway, are refreshed each minute when close to
    expiry (under a lock), and are never returned, logged or sent anywhere but the provider. A custom OAuth server
    with no live token is skipped like one with a missing secret. Spotify setup: `SPOTIFY.md`.
- NVIDIA NIM provider (full gateways only; `gateway/nim.py`). The hosted, OpenAI-compatible API, never a local
  container. Setting `"providers":{"nim":{"enabled":false,"base_url":"https://integrate.api.nvidia.com/v1",
  "model":"nvidia/nemotron-3-ultra-550b-a55b","prices":{}}}`; GET adds the read-only `"has_key"`. A partial
  `providers.nim` merges into the current one; `base_url` must be https on `integrate.api.nvidia.com` or another
  `*.api.nvidia.com` host (port 443, no credentials); changing it deletes the stored key (enter it again); `model` is free text;
  `prices` `{model: [usd_in, usd_out] per MTok}` (absent → cost 0). `default_model`, `task_defaults.*.model`, a
  persona's `default_model` and `/v1/ask`'s `model` accept `"nim"`; the voice server sends `model:"nim"` for
  "엔비디아로 …". The NIM path streams like Claude (text deltas, `activity` thinking/tool_start/tool_end, `done`) with
  the brain's read-only Cygnus tools (when the host has `brain/cygnus.py` and a PAT) and the devices' MCP tools as function
  tools; a model that answers 400 to a request with tools is asked again without
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
  `budget_paths` (a list of `api|nim|claude_code`, default `["api","nim"]`) are the usage paths that count toward
  `monthly_budget_usd` and its warning; `claude_code` runs on the subscription and costs no money, so by default it
  does not count. The budget gate applies after the route is resolved and only when the resolved path is in
  `budget_paths`: a spent paid budget never stops a subscription (`claude_code`) ask. Tasks run on `claude -p` and
  are recorded as path `claude_code` with `task:"task"` and the task's model. `cost_usd` is the counted (paid) total; `by_*` still cover every row. Also
  `"paid":{"cost_usd","by_path"}` and `"subscription":{"claude_code":{"turns","input_tokens","output_tokens",
  "equivalent_usd","quota"}}`, where `quota` is null until a `claude -p` run has reported the subscription windows
  (`rate_limit_event`), else `{"window":"5h"|"weekly" (the fullest),"utilization_pct","resets_at" (epoch s),
  "windows":{"5h":{…},"weekly":{…}}}`; kept in memory per gateway, windows past their reset are dropped.
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
  - `GET /v1/tasks?limit=20` → newest first, each with its last 30 steps (the app's task sheet renders the list
    item). `GET /v1/tasks/{id}` → `{"id","goal","status","device",
    "persona","notify","budget_usd","max_minutes","created","started","finished","result","error","cost_usd","owner",
    "note",
    "steps":[{"at","kind":"tool","tool","label"} | {"kind":"approval"|"approval_approved"|"approval_rejected",
    "approval_id"} | {"kind":"killed","reason":"time"|"budget"|"error"}]}`. Steps never carry tool arguments or results.
    `status`: `queued|running|waiting_approval|done|failed|cancelled`.
  - Deploy order: the memory service before the gateways (an older memory service drops a schedule item's `cron`).
  - Tasks started by claude -p (`mcp__azel__create_task`) get the call's `origin_session` too: an ask's run sends
    `X-Azel-Origin` and `X-Azel-Requester` on the azel MCP server entry (accepted on the full scope only).
  - The external MCP token (`~/.config/biseo/mcp_external_token`, ACTIONS.md) cannot create, read or cancel tasks:
    a task runs with the whole bridge and its results hold personal data. That scope is call_user and schedule_*.
  - `PATCH /v1/tasks/{id}` `{"notify":"call"|"push"|"none"}` → the task without steps; only `notify` may change, only
    while `queued|running|waiting_approval` (409 once finished, 400 for anything else). Takes effect for the finish of
    a task already running on this gateway.
  - `POST /v1/tasks/{id}/cancel` → the task without steps (`cancelled`; 409 once finished). Another gateway's task is
    flagged and stops within about 5 s.
  - Approvals: when a device tool answers `pending_approval`, the run stops, the task shows `waiting_approval` with
    the existing approval (`GET|POST /v1/approvals`), and resumes the same Claude session with the decision.
  - `notify` (default `"push"`) fires when the task finishes, fails, or needs an approval: `"call"` rings as the
    task's persona with the result as the opening (reason `task`; `task_approval` with "맡기신 작업에 승인이 필요해요…");
    `"push"` sends an alert push (not VoIP) to the `alert_token`s from `/v1/push/register`, titled "작업 완료" /
    "작업 실패" / "승인 필요", body "작업이 끝났어요: <goal, 40 chars>" / "작업이 실패했어요: …" / "승인이 필요해요: …"
    (never model output), with `"type":"task","task_id":"<id>"` at the payload's top level next to `aps` (the app
    opens `azel://task/<id>`); `"none"` sends nothing. A cancelled task sends nothing. A task resumes only on approvals
    its own run created (never on an id that merely appears in tool output). Usage: path `claude_code`, `task:"task"`. Stored in ruchba's kv (`task-<id>`, index `tasks-index`, newest 200); a gateway restarting marks its own
    unfinished running tasks `failed`, and picks up its own `queued` tasks that have no runner (at start, then every
    minute). `note`: why a queued task has not started ("다른 작업 2개가 끝나길 기다리는 중"), else null. A runner
    failure is logged and marks the task `failed` ("작업이 실패했어요.").
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
- `POST /v1/push/register` `{"voip_token"?,"bundle_id","env":"sandbox"|"production","alert_token"?}` —
  the app sends its PushKit token and/or its ordinary APNs token (missed-call notices, task pushes). macOS sends
  only `alert_token`. `bundle_id` must be `xyz.scian.biseo` and at least one token is required (400 otherwise). At
  most 10 tokens per kind are kept; registering an 11th evicts the oldest.
- `PUT /v1/phone/days` `{"days":[…]}` (1–14 day summaries, fields in PHONE_DATA.md §3b; an unknown field or a value out
  of range → 400) merges by date into kv `phone-days` and keeps the newest 30 days → `{"stored","updated"}`.
  `GET /v1/phone/days` → `{"days":[…],"updated","keep_days":30}`. `DELETE /v1/phone/days` deletes them all → `{"deleted":true}`; with `?kind=health|sleep|workouts|cycle` only that category's fields (the app sends it when that toggle is turned off; a day left empty goes).
  Full gateways only (403 on limited). The `phone_*` actions read these when no phone answers live.
- `POST /v1/calls` `{"reason","opening"?}` → places a call (VoIP push). Returns
  `{"call_id"}`. `GET /v1/calls/{id}` → `{"opening":"…"}` (voice server reads it).
- Personas (full gateways only; limited → 403): several assistants, exactly one `primary`. Persona:
  `{"id" (slug a-z0-9_-),"name" (1–12 Hangul/Latin/space, unique),"voice" (a TTS speaker),"voice_style"
  (optional TTS instruction, ≤200),"personality" (≤600, "" = the default),"color" ("#rrggbb" or a tint name celadon|indigo|amber|azalea|sprout|sky|plum|ash; default celadon),
  "default_model" (null|haiku|sonnet|opus|nim),"user_title" (same rules as the setting; `""` = use the shared
  `user_title`; null on create or edit → `""`),"share_memory" (bool, default false),"created_at","primary",
  "deleted_at"}`. Stored in the shared settings as `personas`; settings without it migrate to one persona
  `azel` (primary) holding `assistant_name` and `personality`, which stay readable (and writable) as the
  primary's for older clients. `GET /v1/personas` (`?include=deleted`), `POST /v1/personas` → 201
  (`name`, `voice` required; max 8 live personas), `GET|PUT /v1/personas/{id}` (PUT: any field but id,
  primary, created_at), `DELETE /v1/personas/{id}` (marks `deleted_at`: its calls, history and schedule
  keep `persona_id` and the name stays resolvable; the primary cannot be deleted → 400),
  `POST /v1/personas/{id}/primary`. Voices are checked against the TTS server's `/v1/audio/voices` (all nine
  Qwen3-TTS speakers; fallback `BISEO_TTS_VOICES`; neither reachable → 503); anything else → 400. `DELETE /v1/personas/{id}?delete_history=true|false`
  (default false): the persona's own notes and call summaries (memory records with `meta.persona_id`) and
  its calls' follow-up contexts are always deleted; its call history is deleted with `delete_history=true`,
  otherwise it stays with `"persona_deleted": true` on each item (the app shows "삭제된 비서"). If ruchba
  cannot do that (503), the persona is not deleted either. `GET /v1/voices` →
  `[{"id","label","desc","lang"}]`, every speaker the TTS server offers, with Korean labels and a description of
  the voice's character (never a language or nationality); `lang` is kept for compatibility and is not shown (e.g. `{"id":"sohee","label":"소희",
  "desc":"따뜻하고 감정이 풍부한 여성","lang":"ko"}`). `POST /v1/personas/voice-sample` `{"voice","voice_style"?,"text"}`
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
    open questions, the situation note, calls of the last 12 h with what the user said and autonomous calls
    that rang unanswered, today's Google Calendar + Gistra agenda) and asks the local Qwen for `{"call","urgency","reason","opening"}`.
  - Fail closed: if the decision log cannot be read (ruchba down, 404, error) nothing rings, and a call is
    rung only after its log row (with the pre-generated `call_id`) is written.
  - Guardrails, all required to ring: `call` is `true`, `urgency` an integer ≥ 4, now inside `window`,
    and no autonomous call about the same reason (hash of the normalized reason) in the last 2 h. The call opens
    with Qwen's `opening`. There is no count or gap cap: `max_per_day` and `min_gap_minutes` are still
    accepted in settings but unused; the judge paces itself from the recent calls and the agenda.
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
- Approvals: risky or (on `approve_unlisted` devices) unlisted daemon commands waiting for the user's tap.
  - `POST /v1/approvals` `{"device","tool","command","cwd","requester"?}` (daemons) → `{"approval_id","status":"pending",
    "expires_at"}` and an alert push (`"type":"approval","approval_id"`, body "<device>에서 명령 실행 승인이 필요해요";
    the command is shown only in the app). If an `always` rule matches (same device, exact command and cwd) →
    `{"approval_id","status":"approved","rule_id"}` at once, no push (still audited when it runs), except when the
    request comes from a task: a task's commands always need a tap.
  - `GET /v1/approvals` → pending ones only: `[{"id","device","tool","command","cwd","status","created","decided",
    "expires_at","decision","requester","rule_id"}]`. `requester`: the run that asked, per ask or task (never "whoever
    ran last"): each `claude -p` run and each API/NIM daemon call carries `X-Azel-Requester: ask:<persona:session>` or
    `task:<id>` to the daemon, which passes it back as `requester`; shown as `{"session","persona"}` or `{"task_id"}`,
    null when missing or malformed. A task resumes only on approvals whose requester is that task. `GET /v1/approvals/{id}` → the
    same object; `status` `pending|approved|used|rejected|expired` (a pending one expires 5 minutes after it was
    made; an approved one that has not run within 10 minutes of the tap expires too; `used` once it ran).
  - `POST /v1/approvals/{id}` `{"decision":"once"|"always"|"deny"}` (older apps: `{"approve":bool}` = once / deny)
    → `{"id","status","decision","rule_id"?,"forced_once"?,"forced_reason"?}`; 409 when it is no longer pending.
    `always` stores a rule `{"id","device","command","cwd","created"}` only for read-only programs: `open` with only
    `-a <App name>` and http(s)/mailto URLs (no files, `--args`, `-n`, `--env`, `-b`), `ls`, `cat`, `head`, `tail`,
    `wc`, `du`, `df`, `pwd`, `git status|log|diff|show` (no `--ext-diff`, `--textconv`, `--output`), `git branch` to
    list only (no name, delete, move, copy or upstream), `rg`, `grep`, `find` without `-exec`/`-delete`, `sw_vers`,
    `uptime`, `pmset -g`, `system_profiler`. Anything else is approved once, with `"forced_once":true` and a Korean
    `forced_reason` for the app to show. Full gateways only (403 on limited).
  - `GET /v1/approvals/rules` → the rules; `DELETE /v1/approvals/rules/{id}` revokes one (404 if unknown).
  - `POST /v1/approvals/{id}/use` `{"device","command","cwd"}` (daemons, right before an approved run) spends the
    approval in ruchba atomically and once: approved, for that device, exact command and cwd, within 10 minutes of
    the tap → `{"id","status":"used"}`, else 409.
  - `POST /v1/approvals/{id}/ran` `{"exit_code","status"}` (daemons, after an approved run) appends to the audit log;
    `GET /v1/approvals/audit?limit=50` → `[{"approval_id","device","command","cwd","exit_code","status","at",
    "decision":"once"|"always"|"rule"}]` (newest 500 kept; never output).
  - Kept in ruchba's approvals table plus kv (`approval-<id>`, `approval-rules`, `approval-audit`): no schema change.
    A limited gateway has no kv access: it only creates and reads the approval rows (no rules, push, audit or
    decisions), as before.
  - In a call, Claude says "앱에서 허용해 주세요", then calls `run_command` again with the `approval_id`, which waits for
    the decision (below); rejected or expired → it tells the user it was not allowed. Tasks wait as described under
    Tasks.
- TV dashboard (`gateway/tv.py`, the Roku channel in `roku/`): `GET /v1/tv` → one compact, read-only JSON, cached 30 s
  (full gateways only):
  ```json
  {"at": "2026-11-22T20:15:00+09:00",
   "infra": {"health": ["…"], "problems": ["…"], "alerts": ["…"]} | null,
   "tasks": {"running": [{"goal","status"}], "queued": 1, "recent": [{"goal","status"}], "approvals": 0} | null,
   "todo": {"today": ["…"], "assignments": ["…"]} | null,
   "calls": [{"at": "21:00", "what": "약 먹기"}] | null,
   "calendar": ["…"] | null,
   "assistant": {"name": "아젤", "state": "idle"|"call"} | null}
  ```
  Each part is `null` when its source is missing or fails (the rest still answers). Lists are short display lines
  (≤ 10 lines of ≤ 90 characters). No command, approval detail, task result or message body is ever included.
  **`GET /v1/tv?v=2`** (the TV design, `design/roku/SPEC.md` 2.2; same auth, same 30 s cache, `v=1` or no `v` = the shape
  above, any other `v` → 400): the same sources, parsed into fields (a part is `null` when its source fails; times
  without a date are today in KST, timestamps are ISO 8601 with offset; at most 10 items per list):
  ```json
  {"at": "…", "assistant": {"state": "idle"|"call", "persona": {"name": "세레나", "tint": "plum"}},
   "infra": {"asof": "17:04"|null,
             "hosts": [{"name","group","status": "ok"|"warn"|"problem"|"off","up","cpu","mem","disk","temp"|null,
                        "load": "0.08/2"|null,"uptime": "142d"|null,"seen"|null}],
             "alerts": [{"text","head","level": "warn"|"crit","at"|null,"group"|null}],
             "down": [{"service","node","group","type","state": "down"|"degraded","note"|null,"since"|null}],
             "notes": ["a line no parser claimed"]},
   "tasks": {"running": [{"goal","since"}], "queued": {"count","next"|null},
             "approvals": {"count","oldest"|null}, "overnight": {"count","starts": "01:00"},
             "recent": [{"goal","status": "done"|"failed","at"}]},
   "gistra": {"asof": "15:41"|null, "classes": [{"start","end","name","room"}],
              "assignments": [{"title","course","due"|null}], "todos": [{"text","title","kind","due"|null}],
              "sections": [{"title": "7일 안 마감", "items": [{"when": "11/23 23:59"|"마감 없음"|null, "due"|null,
                            "date"|null, "kind": "하우스 문서", "title": "문서 부착 공지", "note", "tag"|null}]}]},
   "calendar": [{"start","end"|null,"what","cal","where"}], "calls": [{"at","what"}],
   "next_deadline": {"title","due","course","source": "gistra"|"calendar"}|null}
  ```
  `calendar` is read from every calendar the user has, timed events only (an imported feed whose name is a URL is
  left out, the same title at the same minute shows once, and the `MyTT` timetable calendar wins a tie).
  The Cygnus texts (`infra_health`, `infra_status`, `infra_alerts`, `gistra_today`, `gistra_assignments`) are parsed in
  `tv.py`: markdown headers are group labels or section titles (never cards), `스냅샷:` and `As of …` become `asof`,
  `없음` under a header is an empty section, and a line no parser understands is kept as a plain item or note.
  Approvals are a count and the oldest time only, never a command. `tint` is the primary persona's `color` (a tint name
  or `#rrggbb`).
  **`?detail=stock&symbol=<code>&range=1d|7d|1mo|3mo|1y`** (the Roku stock page, same auth): one holding's quote and chart.
  `{at, symbol, name, range, currency ("index" for ^GSPC…), index, state: open|closed|pre|post, price, prev, change, pct,
  high, low, volume (null for an index), base (the line's baseline: previous close on 1d, else the first close), points:
  [[unix, close] …, ≤ 200]}`. Yahoo chart data; a six-digit KRX code is tried as `.KS` then `.KQ`. Cached 30 s (1d) or
  5–10 min (other ranges). 400 for a bad symbol or range, 404 for an unknown symbol, 502 when Yahoo is unreachable.
  **Auth**: the gateway token, or the TV token in `~/.config/biseo/tv_token` (0600, ≥ 32 characters; read on every
  request, so deleting or rotating it revokes at once; ignored unless 0600). The TV token opens **only** `GET /v1/tv`
  (anything else → 401).

## 4. Agent daemon (MCP, streamable HTTP at `/mcp`)

Registration body: `{"id":"macbook","name":"macbook","url","harness":[…]}` — `id` and `name` are the same English device name (macbook, albireo, gienah, deneb, spark); `id` is an ASCII
slug used as the MCP server name on the Claude side.

Approval flow: an approval-required `run_command` (or, with `approve_unlisted = true`, any command not in
`allowed_commands`) posts `{"device","tool":"run_command","command","cwd"}` to `POST /v1/approvals` and returns
`pending_approval` (an `always` rule answers `approved` and it runs at once). Claude calls `run_command` again with
`approval_id`; the daemon waits up to `approval_wait_s` (120) for the decision (`pending_approval` again after that),
then checks `GET /v1/approvals/{id}` → `{"status":"approved","device","command","cwd"}`, requires its own device id
and an exact command/cwd match, spends it with `POST /v1/approvals/{id}/use` (central, atomic, once, within 10 min of
the tap; also kept in the local used-ids file), runs it, and reports `POST /v1/approvals/{id}/ran`
`{"exit_code","status"}` (no output).
Grant flow (`approval_via = "gateway-grant"`, vd-spark and vd-core, whose daemons cannot reach the gateway): the daemon
contacts nobody and answers `{"status":"needs_approval","command","cwd","device","nonce"}` (the nonce is remembered in
memory for 15 min). The gateway's device-tool path files the approval itself (`create_approval`: push, expiry, rules,
requester), waits for the tap, spends it (`/use`), and calls `run_command` again with
`grant = {approval_id, device, command, cwd, nonce, exp, mac}`: `mac` is HMAC-SHA256 (hex) of
`"azel-grant-v1\n" + json([approval_id, device, command, cwd, nonce, exp])` (compact, UTF-8) keyed by the daemon's
bearer token; `exp` is at most 10 minutes away (the gateway uses 5). The daemon runs `hard_deny` first, then checks the
signature, exact device/command/cwd, expiry, and that the nonce is one it issued and has not used (persisted in
`used_nonces.txt`, 0600), and runs. A grant never bypasses `hard_deny`; an approval id is refused in this mode. The
gateway records the audit row (`/ran`). `claude -p` runs reach daemons directly, not through the gateway's device-tool
path, so they receive the `needs_approval` answer and cannot continue it.
Approved runs may use any existing directory under `$HOME` as cwd, except one that is, is inside, or contains a
protected path (so never `~` itself); allowlisted runs without approval stay inside `allowed_roots` with `check_args`.
`approve_unlisted`: true on jh-only devices (macbook, albireo, gienah, deneb, ruchba, fawaris), false on vd-spark,
vd-core and sadr (unlisted commands are refused there, approval id or not).
Never runnable, refused before any approval exists (and for approved runs). Names compare case-insensitively and
every argv position counts, so wrappers (env incl. `-S`/`--split-string`, xargs, `find -exec`, nice, nohup, time,
timeout, caffeinate, command) hide nothing:
- `sudo`/`su`/`doas`/`pkexec`; `security`; `osascript`; `awk`/`gawk`/`nawk`, `expect`, `sqlite3`, `vim`/`vi`/`nvim`,
  `emacs`, `tmux`, `screen`, `bun`, `deno`, `php`, `make`, `xcrun`, `launchctl`, `crontab`, `at`, `periodic`;
- inline code: `sh|bash|zsh|… -c`, `python* -c`, `perl -e/-E`, `ruby -e`, `node -e/-p/--eval`, `lua -e`;
- options that run commands: `rg --pre*|--hostname-bin` (also for allowlisted runs without approval);
  `git -c|--config*|--exec-path|--upload-pack|--receive-pack` and `core.sshCommand` (`git -C <dir>` is fine);
  `git config` except read-only (`--get*`, `--list`/`-l`); `tar -P`/`--absolute-paths` (also bundled, `-xPf`,
  `xPf`) and `pax -r`; `plutil -replace|-insert|-remove`;
  `ssh|scp|sftp -o|-F|-S|-e` and ProxyCommand/LocalCommand/ProxyJump; `rsync -e|--rsh`; `zip -TT|--unzip-command`;
  `tar -I|--use-compress-program|--to-command|--checkpoint-action`; `less`/`more` with `!`; `defaults … write|import|
  delete|rename` (anywhere, `-currentHost` too); variables such as `GIT_SSH_COMMAND`, `PAGER`, `EDITOR`, `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`;
- `open -a|-b` of Terminal or iTerm; `open` of `.command|.tool|.sh|.scpt|.applescript|.workflow|.terminal|
  .fileloc|.app|.action|.inetloc` paths or of any existing file with an exec bit;
- `rm -r` or `find … -delete` on `/` or the home directory (or above it); any `diskutil` subcommand except `list` and
  `info`; `mkfs*`/`newfs*`; `dd` with an `of=` that resolves under `/dev` (`//dev/disk2` too);
- `ln -s` whose target, resolved from the link's own directory (and from the cwd), is or covers a protected path;
- any path (argument, `--opt=value`, every attached short-option value such as `-Cx/..`, cwd; relative paths also
  resolved against each `-C`/`--directory`; symlinks resolved first) that is, is inside, or is an ancestor of a
  protected path: `~/.config/biseo`, `~/.ssh`, `~/.claude`, `~/Library/Keychains`, `~/.zshrc`, `~/.zprofile`,
  `~/.zshenv`, `~/.zlogin`, `~/.zlogout`, `~/.bashrc`, `~/.bash_profile`, `~/.profile`, `~/.zsh_history`,
  `~/Library/LaunchAgents`, `~/.gitconfig`, `~/.config/git`, `~/.config/fish`, `~/.config/gh`, `~/.local/bin`,
  `~/.aws`, `~/.netrc` (so `~`, `~/.config`, `~/.local`, `~/Library`, `/Users`, `/` too);
- any repo's `.git` directory itself, anything in a `.git/hooks`, any `.git/config`, and a cwd inside a `.git`.
Still no shell: argv only.

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
- `PUT /v1/state/push_tokens/{token}` `{"bundle_id","env","kind":"voip"|"alert"}`; `GET /v1/state/push_tokens`
  (newest first, with `ts`); `DELETE /v1/state/push_tokens/{token}` → `{"deleted"}`.
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
  `POST /v1/state/approvals/{id}` `{"approve":bool}` → `{"id","status"}`, 409 unless pending;
  `POST /v1/state/approvals/{id}/use` *limited* `{"device","command","cwd","window_s"?:600}` → `{"id","status":"used"}`
  (atomic: approved, matching, decided within `window_s`; 409 otherwise).
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
  The `tools` frame (answer to `tools_list`) adds `"read_only": true|false` to each definition: true is a read with no
  side effect, safe to call speculatively and in parallel; device tools are always false.
  A `tool_result` for agenda, stock_quote, portfolio, weather, gistra_assignments, gistra_today, planit_get_day or
  infra_health/infra_status/infra_alerts also carries `"card": {"kind","title",…}`, built by the gateway
  (gateway/cards.py) from the tool's own data, under 32 KB, fields without data left out. Kinds and fields: the cards
  contract in the shared session notes (cards-contract.md). No card when the tool failed or its output can't be read.
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
