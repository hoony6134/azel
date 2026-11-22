# Azel actions — one definition, every surface

Every action is defined once, in the gateway's registry (`gateway/tools_bridge.py`), and runs through one handler.
Voice, the app, push buttons, `/v1/mcp`, the API/NIM/Qwen tool loops and `claude -p` are thin adapters around it.
They map their input to the action's input and its result to their own output, and do nothing else: no validation,
no limits, no business logic of their own. The same input gives the same checks, limits, confirmation, audit row and
result on every surface.

**The one exception:** approving or denying an approval (`POST /v1/approvals/{id}`) is a human decision in the app. It is
never an action, never a tool, and no model or token scope can reach it.

`gateway/tests/test_actions.py` checks that everything below holds for every registry entry. A new action that skips a
surface fails CI.

## 1. A registry entry

| field | meaning |
|---|---|
| `name` | snake_case, the tool name everywhere (`<device>__<tool>` for device tools) |
| `description` | Korean, written for a model: when to use it, what to pass |
| `input_schema` | JSON Schema (draft 2020-12 subset that the Claude API, OpenAI-style tools and MCP all accept; never `{}`) |
| `read_only` | true if it changes nothing (MCP `readOnlyHint`; no audit of inputs needed beyond the name) |
| `scopes` | which callers see it: `internal`, `qwen`, `app`, `external` (see 3) |
| `confirm` | the confirmation rules that apply (see 4) |
| `limits` | the limits it enforces (see 5) |
| `audit` | what the audit row records (see 6) |
| `rest` | the dedicated REST route, or `None` with a reason; every action is also reachable through `POST /v1/tools/call` on the tailnet |
| `examples` | one valid and one invalid input, used by the conformance test and the eval |

## 2. The result (every action, every surface)

```json
{"ok": true, "spoken": "오늘(11월 23일) 오후 6시로 바꿨어요.", "data": {"item": {"id": "a1b2c3", "at": "…"}},
 "needs": null, "error": null}
```

- `spoken`: always present. One or two plain Korean sentences with no markdown or emoji, safe to speak or push as is.
- `data`: the structured result (the item, the list, the task), or null.
- `needs`: set when the action did nothing because the user must answer first. `ok` is then false.
  - `{"kind": "confirm", "question": "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요. 맞죠?", "args": {…}}`:
    once the user says yes, resend `args` exactly. They are the resolved rule with `confirmed: true`, so nothing is
    guessed a second time.
  - `{"kind": "clarify", "question": "11월 24일 아침 7시요, 저녁 7시요?"}`: resend with the answer (`when_text`, `scope`, …).
  - `{"kind": "choose", "question": "어느 것인지 골라 주세요.", "options": [{"id": "…", "spoken": "오늘 오후 10시 약 먹기"}]}`:
    resend with `id`.
- `error`: `{"code": "invalid" | "not_found" | "limit" | "conflict" | "unavailable", "message": "<Korean>"}`. `spoken` repeats
  the message.

How each surface renders it:

| surface | ok | needs | error |
|---|---|---|---|
| model tool loops (API, NIM, Qwen), `claude -p`, `/v1/mcp` | `spoken` (+ compact `data` JSON) as the tool result | `spoken` with the question and how to resend; `is_error: true` | `spoken`; `is_error: true` |
| REST (app), with the request header `X-Azel-Result: 2` | 200/201, the whole JSON | 200, the whole JSON (the app shows the question, then resends) | 400 invalid, 404 not_found, 409 conflict, 429 limit, 503 unavailable, 502 failed; body `{"detail": message, …result}` |
| REST without that header (app builds before batch 5) | 200/201, `data` alone (the old body) | 409 `{"detail": question, "needs": …}` | the same codes, `{"detail": message}` |
| voice fast path (link `tool_result` frame: `result` for Qwen, plus `spoken` and `needs`) | speaks `spoken` | speaks the question and holds the request for the answer | speaks `spoken` |
| push action | silent on ok; a local notification with `spoken` | opens the app on the question | a local notification with `spoken` |

## 3. Scopes and callers

| scope | who | token |
|---|---|---|
| `internal` | `claude -p` via the gateway, API/NIM task loops | master `~/.config/biseo/token` |
| `qwen` | the voice server's Qwen over the gateway link | link token |
| `app` | the app's REST calls, push actions | master token (through the public proxy only for the routes on its allowlist) |
| `external` | the user's own Claude Code (later claude.ai) via `/v1/mcp`: call_user and schedule_* only (no tasks: a task runs with the whole bridge, and task results hold personal data) | `~/.config/biseo/mcp_external_token` (0600, re-read on every request; delete or rotate the file to revoke) |

One input rule depends on the caller, and it is part of the contract: **only `app` may send an exact `when`** (a time a
person picked in the UI). Every model scope sends the user's own words in `when_text`, and the gateway resolves them
(`late_night.when_from_words`). A model that sends a bare `when` gets `error.invalid`: "시각은 … when_text로 주세요".
The conformance test asserts this asymmetry explicitly.

## 4. Confirmation rules (in the handler, never in a surface)

- **Spoken times** (`when_text`, `late_night.when_from_words`, shared with the voice fast path):
  - A bare hour (`6시`) on `schedule_update` keeps the item's own 오전/오후 and day: 오후 10시 → `6시` = 오후 6시. If that time
    has passed → `needs.clarify`.
  - On `call_user`, a bare hour takes the nearest future plausible reading: 1–5시 = afternoon; `wake` = morning.
  - A named day or weekday with two plausible readings (`내일 7시`) → `needs.clarify` "아침 7시요, 저녁 7시요?". A reading
    already past on that day drops out.
  - Late night (00:00–04:59): `내일`/`모레`/`글피` count from the day just ended. An afternoon time is created, with `spoken`
    saying so and the interpretation stored on the item. A morning time → `needs.clarify` "오늘 아침 7시요, 아니면 24일 아침
    7시요?".
- **Recurring rules** (`cron.py`): a rule that is not one time of day on every day or on some weekdays, or that has a
  start or end date → `needs.confirm` with the read-back, until `confirmed: true`.
- **Ambiguous matches** (cancel, update): several items match → `needs.choose`. Nothing changes.
- **Recurring item without `scope`** (update) → `needs.clarify`. The model is told: a named day means `occurrence`,
  otherwise `rule`. Cancel the same: a rule with no scope → `needs.clarify` "이번 한 번만 취소할까요, 매번 취소할까요?";
  the app's `DELETE /v1/schedule/{id}` sends `scope: rule` (or `occurrence` with `?occurrence=`). Cancel with nothing
  named → the only item, else `needs.choose` (never "the soonest").
- **Identical rule** (create): the same cron, kind, purpose and persona as an active item → 409. The daily cap (20)
  counts over the rule's own first 14 days (from `start_date`, never before now), the other items' fires included.
- **Prompt-injection guard:** after a `web_search`, `web_fetch`, `memory_recall`, `people_lookup`, Cygnus or Google read
  in the same run (`ask:<persona:session>` or `task:<id>`, from the gateway's requester; within 10 minutes), a model's
  side-effect action (call_user, schedule_create/update/cancel, create_task, remember) → `needs.confirm` with a read-back,
  until it is resent with `confirmed: true`. The app (`caller: app`) is the user's own tap and never asks.
- **No task chains:** a task cannot start a task (`create_task` is not in agent_loop's tools; claude -p task runs deny
  `mcp__azel__create_task`).
- **Purpose and opening:** the raw request is stored but never spoken; `purpose` is normalized (`openings.py`).

## 5. Limits (in the handler)

| limit | value | applies to |
|---|---|---|
| user-requested calls | 20 per KST day, counting rules' fires over the next 14 days | call_user, schedule_update |
| gap within a rule | ≥ 30 min between fires | call_user (rules), schedule_update |
| one-off horizon | 30 s to 30 days ahead | call_user, schedule_update |
| repeat guard | the same time twice within 60 s creates one item | call_user |
| tasks | per-task budget and minutes (settings), 2 running per device | create_task |
| external scope | 60 tool calls per hour per token | every action in `external` |

## 6. Audit

Every call writes one row to kv `actions-<KST date>`:
`{at, action, surface, scope, persona, ok, needs_kind, error_code, item_id|task_id}`. Inputs are not recorded,
apart from the action name and target id (no message bodies, no tokens). External-scope calls also log a stdout line with
the tool name only.

## 7. The actions

| action | purpose | dedicated REST route (app) | tools (internal, qwen) | external | voice fast path | push action |
|---|---|---|---|---|---|---|
| `call_user` | ring now, at a time, or on a rule | `POST /v1/calls` (now) | ✓ | ✓ | "N시에 전화해줘", "매일 …" | — |
| `schedule_create` | `call_user` that must have a time (the app's form); the same handler and checks | `POST /v1/schedule` | internal only (models use call_user) | ✓ | — | — |
| `schedule_list` | scheduled calls with rule text and next fire | `GET /v1/schedule` | ✓ | ✓ | "예약 뭐 있어?" | — |
| `schedule_update` | change time, purpose, notify, rule, dates; one fire or the rule | `PUT /v1/schedule/{id}[?occurrence=ISO]` | ✓ | ✓ | "10시 알림 6시로 바꿔줘" | `SNOOZE_10` → occurrence, `when_text: "10분 뒤"` |
| `schedule_cancel` | cancel an item, a rule, or skip one fire | `DELETE /v1/schedule/{id}[?occurrence=ISO]` | ✓ | ✓ | "취소해줘", "내일 거만 빼줘" | `SKIP` → occurrence |
| `create_task` | background work, notifying by call/push/none | `POST /v1/tasks` | ✓ | — | (models) | — |
| `task_status` | one task or the recent ones | `GET /v1/tasks`, `GET /v1/tasks/{id}` | ✓ | — | "맡긴 거 어떻게 됐어?" | — |
| `task_cancel` | stop a running task | `POST /v1/tasks/{id}/cancel` | ✓ (new tool) | — | "그 작업 그만해" | `STOP` on a running-task push |
| `approval_status` | pending approvals and their state (read only) | `GET /v1/approvals`, `GET /v1/approvals/{id}` | ✓ | — (personal device commands) | "승인 기다리는 거 있어?" | — |
| `end_call` | hang up the call after a short goodbye, when the user says so (never mid-question, never from a task) | — (in-call only; the app hangs up itself) | ✓ | — | "이제 됐어 끊어" | — |
| `auto_approve` | the user's own "알아서 해": safe device commands run without a tap for this call (≤ 2 h), risky ones still ask; read back once, refused after a web/mail/memory read and in tasks | the app's toggle is `POST /v1/auto-approve` (and the call's `auto_approve` frame) | ✓ | — | "권한 줄 테니 알아서 해" | — |
| `music_play`, `music_control` | Apple Music on the user's Mac through the daemon's `music` tool (fixed AppleScript templates; no approval) | not on REST: no app screen | ✓ (no approval for qwen) | — | (models) | — |
| `portfolio` | the user's portfolio (the `holdings` setting) quoted in one call: price, change %, date per holding, the same rows as the TV's 내 종목 (5-min cache) | not on REST: the app and TV read `holdings` in `GET /v1/tv?v=2` | ✓ | — | "내 주식 어때?" | — |
| `holdings_update` | add or remove names in the `holdings` setting (the TV's 내 종목; `"name (TICKER)"` pins a listing); voice keeps the TV in sync, memory is saved separately with `remember` | not on REST: the app edits `holdings` with `PUT /v1/settings` | ✓ | — | "보유 목록에 OO 추가해줘" | — |
| `home_scene` | run an allowlisted HomeKit scene (a Shortcut named in the daemon's `home_shortcuts`); after a web/mail/memory read it needs `confirmed` | not on REST | ✓ | — | (models) | — |
| `<device>__<tool>` | daemon tools (list_dir, read_file, run_command, …) | not on REST: the app hands device work to `create_task`; tailnet `POST /v1/tools/call` | internal ✓; qwen read tools, run_command only after an approval, no delegate | — | — | — |
| Cygnus reads (`gistra_*`, `infra_*`, `planit_*`) | read-only school, infra, planner data | not on REST: the app has no view for them yet; tailnet `POST /v1/tools/call` | ✓ | — (personal; opt-in later) | (models) | — |
| Google reads (`calendar_events`, `gmail_search`, `gmail_read`) | read-only, only with a live grant | not on REST, same reason | ✓ | — (personal) | (models) | — |
| Spotify (`spotify_play`, `spotify_control`, `spotify_now_playing`, `spotify_devices`) | music by voice, only with a live grant (`SPOTIFY.md`) | not on REST: music is spoken; the app only connects it (`/v1/connectors/spotify/authorize`) | ✓ (also served to `claude -p` over `/v1/mcp`) | — | (models) | — |
| iPhone reads (`phone_health`, `phone_sleep`, `phone_workouts`, `phone_calendar`, `phone_reminders`, `phone_contacts`, `phone_location`, `phone_now_playing`) | read-only iPhone data (PHONE_DATA.md): live from the phone in a call, health/sleep/workouts else from the synced daily summaries; `error.unavailable` with a spoken reason when neither | not on REST: the app reads its own phone (the summaries' own routes are `/v1/phone/days`) | ✓ | — (personal) | (models) | — |

Not actions: `POST /v1/approvals/{id}` (human only); `POST /v1/approvals` (daemons file requests); settings, personas,
history, connectors, push registration (configuration, not things the assistant does).

### Inputs (abridged; the registry holds the full schemas)
- `call_user` / `schedule_create`:
  - `reason` (required);
  - time: `when_text` from models, `when` from the app;
  - a rule: `repeat_cron`, or `repeat_text` (the user's words; turned into a checked rule by `cron.from_words`);
  - `kind`: reminder | wake;
  - `start_date`, `end_date`, `notify`: call | push;
  - `confirmed`, `persona_id`.
- `schedule_update`:
  - target: `id` | `at` | `description`;
  - `scope`: occurrence | rule;
  - changes: `when_text` / `when`, `purpose`, `notify`, `repeat_cron`, `start_date`, `end_date`;
  - `confirmed`.
- `schedule_cancel`: target as above; `scope`.
- `create_task`: `goal` (required), `device`, `notify`, `backend` (claude_api | claude_code | nim | qwen; the default
  comes from settings, with recorded fallbacks).
- `task_status`: `id`. `task_cancel`: `id` (required). `approval_status`: `id`.
- `phone_health`: `metric` (required: summary, steps, distance, active_energy, exercise, heart_rate, resting_heart_rate, hrv,
  mindful, weight, cycle), `range` (today, yesterday, this_week, last_week, last_7_days, this_month, or `YYYY-MM-DD`).
  `phone_sleep`: `date` (the wake-up date; empty = the latest night). `phone_workouts`: `range`. `phone_calendar`: `range`
  (today, tomorrow, this_week, next_week, `YYYY-MM-DD`). `phone_reminders`: `list`. `phone_contacts`: `name` (required).
  `phone_location`, `phone_now_playing`: none.
