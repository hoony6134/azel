# iPhone and Apple data (아이폰 데이터)

Lets the assistant answer "어제 몇 시간 잤어?", "이번 주 운동 얼마나 했어?", "오늘 걸음 수", "심박수 어땠어?",
"미리 알림에 뭐 있어?", "지금 어디야?" from the user's own iPhone. Contracts: PROTOCOL.md §1 (call socket frames), §1b
(link frames), §3 (`/v1/phone/days`); ACTIONS.md §7 (the `phone_*` actions).

## 1. Sources

| category (toggle) | Apple API | what we read | default |
|---|---|---|---|
| `health` 건강 | HealthKit | steps, walking+running distance, active energy, exercise minutes, heart rate (avg/min/max), resting HR, HRV (SDNN), mindful minutes, body mass | off |
| `sleep` 수면 | HealthKit `sleepAnalysis` | per night: bedtime, wake time, asleep, in bed, core/deep/REM, awake | off |
| `workouts` 운동 | HealthKit `HKWorkout` | type, start, minutes, kcal, distance | off |
| `cycle` 생리 주기 | HealthKit `menstrualFlow` | flow level that day; only when this is on | off, separate opt-in |
| `calendar` 캘린더 | EventKit events | title, start, end, all-day, calendar name, location (all accounts on the phone, besides Google via the gateway) | off |
| `reminders` 미리 알림 | EventKit reminders | open reminders: title, due, list, priority | off |
| `contacts` 연락처 | Contacts | lookup by name: name, phones, emails, birthday, organization (max 5) | off |
| `location` 위치 | CoreLocation (one fix, `requestLocation`) + reverse geocode | place name and address, accuracy, time. On demand only, when-in-use, never tracked, never uploaded | off |
| `motion` 움직임 | CoreMotion `CMMotionActivityManager` | current activity (stationary, walking, running, driving, cycling), with the location answer | off |
| `music` 음악 | MediaPlayer (`systemMusicPlayer`, `MPMediaQuery` by last played) | now playing (title, artist, album, playing), last 5 played from the library | off |
| `sync` 요약 올리기 | — | upload daily summaries of health/sleep/workouts/cycle (§3b) | on (does nothing until one of those is on) |

Everything is off until turned on. On first launch a skippable sheet lists the eight main categories (건강·수면·운동·
캘린더·미리 알림·연락처·위치·음악) with one line each on what Azel reads, plus 모두 켜기 / 모두 끄기; 생리 주기 and 움직임 are
separate opt-ins in Settings → "아이폰 데이터", where every toggle stays available. Turning a category on triggers its iOS
prompt at that moment, never at launch. Each row shows the real iOS state: 켜짐 / iOS에서 거부됨 / 아직 안 물어봄 (HealthKit
never reveals a read denial, so an asked HealthKit category shows 켜짐); a denied row offers "설정 앱에서 허용", which opens
the app's page in the Settings app, since iOS never asks twice. Turning a category off stops it at once and deletes its
synced fields (`DELETE /v1/phone/days?kind=…`).

**Not possible** (no public API, or no read access): Screen Time usage numbers (DeviceActivity only hands opaque
tokens to an extension, never readable numbers), Messages/iMessage, the call log, Mail, Notes, Safari history, other
apps' notifications, Wallet transactions (FinanceKit needs an Apple-granted entitlement), Apple Music's cloud
"recently played" without a MusicKit App ID service (we use the on-device library instead), stand hours / ring goals
(HKActivitySummary is readable but left out until asked). Battery and Focus on/off are possible later if wanted.

## 2. Actions (gateway registry, `phone_data.py`)

Read only, no confirmation, scopes `internal` + `qwen` (every model path: `/v1/tools`, the link, `/v1/mcp` for
`claude -p`). Not `external` (personal) and not `app` (the app reads its own phone directly; `rest: None`).

| action | input | source |
|---|---|---|
| `phone_health` | `metric`: steps, distance, active_energy, exercise, heart_rate, resting_heart_rate, hrv, mindful, weight, cycle, summary; `range`: today, yesterday, this_week, last_week, last_7_days, this_month, or `YYYY-MM-DD` (default today) | live, else synced days |
| `phone_sleep` | `date`: the wake-up date `YYYY-MM-DD`; empty = the latest night ("어제 몇 시간 잤어" / "어젯밤") | live, else synced |
| `phone_workouts` | `range` as above (default this_week) | live, else synced |
| `phone_calendar` | `range`: today, tomorrow, this_week, next_week, or `YYYY-MM-DD` (default today) | live only |
| `phone_reminders` | `list` (optional list name) | live only |
| `phone_contacts` | `name` (required) | live only |
| `phone_location` | — | live only |
| `phone_now_playing` | — | live only |

Each returns `spoken` (1–2 Korean sentences) and compact `data`. Weeks start on Monday, KST. When nothing is
available, `spoken` says why, with `error.code: "unavailable"`:
- the category is off or never synced: "아이폰 데이터에서 건강을 켜 주시면 알 수 있어요.";
- a live-only action with no phone reachable: "지금은 아이폰에 연결돼 있지 않아서 못 봐요. 앱으로 통화 중일 때 물어봐 주세요.";
- the phone answered but has no samples: "어제는 수면 기록이 없어요."

The gateway does all the Korean formatting (one formatter for live and synced data); the app returns numbers.

## 3. Transport

### 3a. Live (during a call)
```
model tool call → gateway bridge → link {"type":"phone_query","id","kind","args"} → voice server
  → the app's /v1/call socket {"type":"phone_query","id","kind","args"} → app queries on the phone
  → {"type":"phone_result","id","ok","data"|"error"} → voice server → link → gateway → spoken result
```
- After connecting, the app sends `{"type":"phone_hello","kinds":[…]}` listing the categories that are on. Secret calls
  may read like any call (as memory recall does); nothing from a live answer is stored anywhere, in any call.
- The voice server forwards a query only to the latest call socket whose hello lists that kind; otherwise it answers
  at once `{"ok":false,"error":"offline"}`. The gateway waits at most 8 s (location: 12 s).
- `kind` ∈ health, sleep, workouts, calendar, reminders, contacts, location, now_playing. `args`: dates already resolved
  by the gateway (`from`, `to` as `YYYY-MM-DD`, inclusive), `list`, `name`.
- Live results are never stored by the gateway or the voice server (only the action audit row: name, ok, no content).
- Outside a call, live-only actions answer "not reachable". A dedicated app↔gateway channel (or a silent push that
  wakes the app) is a possible later step; not built (background wake-ups are unreliable and need the public proxy).

### 3b. Sync (in the background)
- The app uploads one compact summary per day, never raw samples:
  `PUT /v1/phone/days {"days":[{date, steps, distance_m, active_kcal, exercise_min, mindful_min, hr_avg, hr_min,
  hr_max, resting_hr, hrv_ms, weight_kg, cycle, sleep:{start, end, asleep_min, in_bed_min, core_min, deep_min,
  rem_min, awake_min}, workouts:[{type, start, minutes, kcal, distance_m}]}]}`. Keys of categories that are off are
  absent. The last 7 days are sent each time (late Watch syncs fix older days).
- When: on app foreground, on HealthKit background delivery (observer queries on steps, sleep, workouts; hourly at
  most), and a `BGAppRefreshTask` (`xyz.scian.biseo.phone-sync`) asking for every ~4 hours. Off when `sync` (its own
  toggle, separate from live reads) is off; not tied to calls.
- The gateway validates every field (known keys and ranges only), merges by date into kv `phone-days`, and keeps the
  newest **30 days**. `GET /v1/phone/days` shows what is stored; `DELETE /v1/phone/days` deletes it all (Settings →
  아이폰 데이터 → "모든 데이터 삭제"), `?kind=health|sleep|workouts|cycle` one category. Turning `sync` off also deletes.
- `/v1/phone/days` (GET/PUT/DELETE) goes on the public proxy allowlist at deploy (approved 2026-11-22).

## 4. Privacy rules
- Per-category toggles; permission prompts only when a toggle is turned on.
- Secret calls: live reads allowed, nothing stored or logged (the same as every call: live answers are never kept).
- Location: one fix per request, when-in-use permission only, never uploaded, no background location.
- Raw samples, events, reminders, contacts, location and music never leave the phone except inside one live answer.
- Summaries: 30 days on the server, delete-all in the app; only on full (jh-only) gateways, never limited ones.

## 5. App structure (`assistant/app/Biseo/iOS/PhoneData/`)
- `Shared/PhoneDataCore.swift` — categories and toggles, `PhoneAuth` (row states), the `PhoneStores` protocol, pure
  aggregation (samples → day summary) and `PhoneResponder` (query → `phone_result`). Unit tested with fake stores.
- `iOS/PhoneData/AppleStores.swift` — the real stores (HealthKit, EventKit, Contacts, CoreLocation, CoreMotion, MediaPlayer).
- `iOS/PhoneData/PhoneDataSettings.swift` — permission requests and status mapping, the settings screen, the first-run sheet.
- `iOS/PhoneData/PhoneDataSync.swift` — call-socket hooks, HealthKit observers, background delivery, BGTask, upload, delete.
- Shared edits: `CallClient` gets two static hooks (`phoneHello` sent on start, `phoneQuery` answers `phone_query`
  frames); `BiseoApp` calls `PhoneDataSync.start()`, uploads on foreground, adds `.phoneDataFirstRun()` and the settings row. Entitlements: `com.apple.developer.healthkit`, `…healthkit.background-delivery`; Info.plist
  usage strings (Korean) and `BGTaskSchedulerPermittedIdentifiers`, background mode `fetch`.
