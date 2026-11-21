# Azel — design spec

For the cross-platform SwiftUI app (iOS 17+, macOS 14+). Open `mockups.html` next to this
file to see every screen with these tokens. Nothing here needs a new server API; the few
places that would benefit from one are listed in §12.

## 0. Names

- **App name / wordmark: "Azel"** (bundle display name, menu-bar title, welcome screen,
  About). Fixed.
- **Assistant name: `assistant_name`, default "아젤"**, a user setting (§7.5). Every place
  this spec writes "아젤" means `assistant_name`: call-screen title, CallKit caller name
  (`CXHandle` value / `localizedCallerName`), call button "아젤에게 전화", menus, VoiceOver
  labels, error copy, the PTT panel label.
- Korean particles depend on the name's last syllable. Use a tiny josa helper: for the
  last Hangul scalar `u`, `hasBatchim = (u - 0xAC00) % 28 != 0` → 이/가, 은/는, 을/를, 과/와;
  "에게" never changes. Non-Hangul names: treat as no batchim. Prefer particle-free copy
  where it reads naturally.

## 1. Direction: 달항아리 (the moon jar)

Azel is a voice you call, so the app is mostly one object: a softly lit **moon jar**, the
slightly lopsided white porcelain jar of Joseon ceramics, floating on 먹 (ink) black. It's
round but never a perfect circle. Its glaze shifts with the call: **청자 celadon** while it
listens, **쪽빛 indigo** while it thinks, warm **달빛 porcelain** while it speaks. Everything
else (captions, controls, cards) stays quiet, typographic and Korean-first, so the jar and
the words carry the screen. Dark is the primary look; light (한지 paper) is a full peer, not
an afterthought.

What makes it ours and not another assistant's: the asymmetric jar silhouette (wider
shoulder, narrower foot), the celadon/indigo/porcelain state palette, and the water-line
reflection under the jar while it speaks. No sparkles, no rainbow gradients, no orange.

## 2. Color tokens

Put these in an asset catalog as color sets with Any/Dark appearances (names below), or as a
`Theme` enum of `Color(light:dark:)`. Contrast is measured against `bg` and `card` (WCAG 2.1).

| Token | Dark (primary) | Light | Use | Contrast dark / light (vs bg) |
|---|---|---|---|---|
| `bg` | `#0D1113` 먹 | `#F5F3EC` 한지 | Screen background | — |
| `bgRaised` | `#131A1D` | `#EFECE3` | Sheets, Mac popover | — |
| `card` | `#182025` | `#FFFFFF` | Grouped cards | — |
| `stroke` | `#26313A` | `#DCD8CC` | 1 pt card borders, dividers | — |
| `text` | `#ECF0EC` | `#14191B` | Primary text | 16.5 / 16.0 |
| `text2` | `#A9B4B0` | `#4E5A56` | Secondary text, user transcript | 8.9 / 6.5 |
| `text3` | `#8A9692` | `#5F6B67` | Captions, timestamps (never below 13 pt) | 6.2 / 5.0 |
| `celadon` | `#82CBB4` | `#2C7663` | Accent, call button, listening, qwen chip | 10.1 / 4.9 |
| `indigo` | `#9AAAF0` | `#4656B8` | Thinking, claude chip | 8.5 / 5.8 |
| `porcelain` | `#F3EBDC` | `#FFFFFF` | Jar body while speaking | decorative |
| `amber` | `#E9BC6E` | `#8F5E12` | Speaking glow, warnings, budget | 10.7 / 5.0 |
| `danger` | `#FF7A6E` | `#C2352B` | Errors, reject | 7.5 / 4.9 |
| `glazeCeladon` / `glazeIndigo` / `glazeAmber` | `#82CBB4` / `#9AAAF0` / `#E9BC6E` | `#A8DCCB` / `#B4BFF3` / `#F0CB86` | Jar and background glazes only (never text) | decorative |
| `hangup` | `#C8342C` | `#C8342C` | Hang-up button fill (white glyph 5.3:1) | — |
| `onAccent` | `#0D1113` | `#FFFFFF` | Text on a celadon fill | 10.1 / 5.4 |

Rules:
- Status color is never the only signal: every state also has a word (듣고 있어요 / 생각
  중 / 말하는 중) and an SF Symbol.
- Route chips: `qwen` → celadon outline + "로컬" ; `claude` → indigo outline + "Claude".
  Outline (1 pt, color at 60%) with text in the full color, background the color at 12%.
- The jar and background glazes use the `glaze*` tokens (lighter in light mode so the jar
  stays porcelain-like); the plain accents are for text, chips and fills.
- Glaze gradients may use the state colors at any opacity; text never sits on a glaze
  without a scrim (`bg` at 55% under captions).
- Increase Contrast (`colorSchemeContrast == .increased`): strokes go to `text3`, chip
  backgrounds to 20%, text2 → text.

## 3. Typography

System fonts only. On Apple platforms `.font(.system(...))` already renders Hangul with
Apple SD Gothic Neo and Latin/digits with SF Pro, so no custom font files. Always use text
styles so Dynamic Type works; the sizes below are the default (Large) sizes.

| Role | SwiftUI | Size / weight | Line spacing |
|---|---|---|---|
| Wordmark "Azel" | `.largeTitle.weight(.semibold)` | 34 / semibold | — |
| Greeting | `.title.weight(.semibold)` | 28 | `.lineSpacing(4)` |
| Live reply caption | `.title2.weight(.medium)` | 22 / medium | `.lineSpacing(6)` |
| User transcript caption | `.body` in `text2` | 17 | `.lineSpacing(4)` |
| State label | `.headline` | 17 / semibold | — |
| Card title | `.headline` | 17 / semibold | — |
| Body / rows | `.body` | 17 | — |
| Secondary | `.subheadline` | 15 | — |
| Chips, timestamps | `.footnote.weight(.semibold)` | 13 | — |
| Code (approvals) | `.callout.monospaced()` | 16 | — |
| Numbers (cost, ms, timer) | add `.monospacedDigit()` | — | — |

- Korean text gets no negative tracking; keep `.tracking(0)`.
- Captions: `lineLimit(nil)`, `.multilineTextAlignment(.center)` on iPhone,
  `.leading` on Mac. Never truncate a reply; the caption area scrolls.
- At accessibility sizes (`dynamicTypeSize >= .accessibility1`) the jar shrinks to 40% of
  its height and captions get the room; control dock labels move under icons.

## 4. Spacing, radii, elevation

- Spacing scale (pt): `xs 4 · s 8 · m 12 · l 16 · xl 24 · xxl 32 · hero 48`. Screen
  side margin 20 on iPhone, 16 inside the Mac popover.
- Radii (all `.continuous`): chip = capsule, control button = circle, call button = 28,
  card = 22, inner row / code block = 12, Mac popover content = 14, PTT panel = 30.
- Elevation: no drop shadows on cards in dark; separation is `card` on `bg` plus 1 pt
  `stroke`. Light mode cards get `shadow(color: .black.opacity(0.05), radius: 12, y: 4)`.
- Controls over the call background use `.ultraThinMaterial` + `stroke` hairline.
- Minimum hit target 44×44 pt (iOS) / 28×28 pt (Mac).

## 5. Motion

| Token | Value | Use |
|---|---|---|
| `quick` | `.snappy(duration: 0.18)` | Button presses, chip changes |
| `state` | `.smooth(duration: 0.45)` | Jar color/shape between call states |
| `enter` | `.spring(duration: 0.5, bounce: 0.15)` | Call screen in, cards in |
| `caption` | `.easeOut(duration: 0.22)` + `.opacity` + 6 pt rise | New caption line |
| Level smoothing | attack 40 ms, release 180 ms (one-pole) | Jar reacting to audio |

- Call screen opens as a matched-geometry zoom from the home jar (`matchedGeometryEffect`
  on iOS 17; `.navigationTransition(.zoom)` may replace it on iOS 18+).
- `interrupt` event: playback is flushed, the caption being spoken gets a dimmed
  "…" tail and fades to `text3` over 220 ms, the jar snaps to listening with a single 120 ms
  ring ripple. This tells the user "I stopped, go ahead".
- Haptics (iOS): `.sensoryFeedback(.impact(weight: .light), trigger: state)` on
  listening→thinking only; `.success` on approval sent; `.warning` on error. Nothing else.
- **Reduce Motion** (`accessibilityReduceMotion`): the jar keeps its silhouette but stops
  undulating; listening/speaking show level as a uniform scale of at most ±4% with no
  noise; thinking shows a static indigo glaze with a slow (2 s) opacity change only
  between 0.85 and 1.0; zoom transitions become crossfades (0.25 s); caption rise is off.
- **Reduce Transparency**: materials become `bgRaised` solid.
- Pause the visualizer `TimelineView` when the app is in background or the call screen is
  not visible (`paused:` parameter) to save battery.

## 6. Hero visualizer: the moon jar

### 6.1 Inputs

`AudioIO` publishes two levels in 0…1, sampled at ~30 Hz:

- `micLevel`: RMS of the input tap buffer (already there for the converter).
- `outLevel`: RMS of each PCM16 chunk at the moment it is scheduled on the player node,
  delayed by the player's latency; simpler and good enough: install a tap on
  `engine.mainMixerNode` (bufferSize 1024) and take RMS there.

Map RMS to a level: `db = 20*log10(max(rms, 1e-5))`, `level = clamp((db + 50) / 44, 0, 1)`
(so −50 dBFS → 0, −6 dBFS → 1). Smooth with a one-pole filter, attack 40 ms, release
180 ms. Don't publish these through `@Published` (60 Hz SwiftUI invalidation); store them in
a small `final class Levels: @unchecked Sendable` with atomic floats (or an
`OSAllocatedUnfairLock`) that the `Canvas` reads every frame.

### 6.2 Shapes per state

The jar is a closed path of 120 points around a center. Base radius `R` (iPhone: 0.32 ×
screen width; Mac panel: 34 pt). For angle θ (0 = right, measured clockwise, y down):

```
jar(θ)   = 1 + 0.06·sin(θ)                   // wider at the bottom half → "foot" heavier…
         − 0.035·cos(2θ)                      // …and a soft shoulder
         + 0.015·sin(3θ + 0.6)                // the hand-thrown asymmetry
r(θ, t)  = R · jar(θ) · (1 + wobble(θ, t))
```

| State | Driver | `wobble(θ,t)` | Glaze | Extras |
|---|---|---|---|---|
| idle (home) | none | `0.008·sin(θ·2 + t·0.6)` | celadon 25% → porcelain 70% | slow 6 s breathing of glow (off with Reduce Motion) |
| listening | `micLevel` | `L·(0.05·sin(3θ + 2.1t) + 0.03·sin(5θ − 3.3t))` | celadon core, porcelain rim | 1 pt celadon outer ring at `R·(1.12 + 0.1L)`, opacity `0.25 + 0.5L` |
| thinking | time only | `0.012·sin(2θ + 1.2t)` | indigo inner swirl: conic gradient rotating at 0.25 rev/s, masked to the jar | glow pulses 1.4 s period, 0.6↔1.0 |
| speaking | `outLevel` | `L·(0.07·sin(2θ + 2.8t) + 0.025·sin(7θ + 5t))` | porcelain body, amber glow `radius R·(0.3 + 0.5L)` | **water line**: below the jar, a mirrored, vertically squashed (×0.25), blurred (12) copy at 30% opacity, offset `R·1.25`; its width tracks `L` |
| error / offline | none | 0 | desaturated `text3` at 40% | no glow |

State changes interpolate the glaze colors and wobble amplitudes over `state` (0.45 s);
keep the previous state's parameters and blend `mix = smoothstep(elapsed/0.45)`.

### 6.3 Building it in SwiftUI (no dependencies)

```swift
struct MoonJar: View {
    var state: JarState            // .idle .listening .thinking .speaking .offline
    let levels: Levels             // reads micLevel / outLevel without invalidating SwiftUI
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var phase

    var body: some View {
        TimelineView(.animation(minimumInterval: 1/60, paused: phase != .active)) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            Canvas { ctx, size in
                let level = state == .speaking ? levels.out : levels.mic
                let path = JarPath.make(in: size, t: reduceMotion ? 0 : t,
                                        level: level, state: state, reduceMotion: reduceMotion)
                ctx.drawLayer { glow in                      // 1. soft glow
                    glow.addFilter(.blur(radius: 28))
                    glow.fill(path, with: .color(state.glow.opacity(0.55)))
                }
                ctx.fill(path, with: .radialGradient(       // 2. glazed body
                    Gradient(colors: state.glaze), center: path.boundingRect.center.offset(y: -0.25),
                    startRadius: 0, endRadius: path.boundingRect.width * 0.7))
                if state == .thinking {                      // 3. inner swirl
                    ctx.clip(to: path)
                    ctx.fill(path, with: .conicGradient(Gradient(colors: state.swirl),
                             center: path.boundingRect.center, angle: .radians(t * .pi / 2)))
                }
                // 4. speaking water line: draw `path` again with scaleBy(1, -0.25), blur 12, opacity 0.3
            }
        }
        .accessibilityElement()
        .accessibilityLabel("아젤")
        .accessibilityValue(state.spokenLabel)     // "듣고 있어요" …
        .accessibilityAddTraits(.updatesFrequently)
    }
}
```

- `MeshGradient` needs iOS 18 / macOS 15. Use it only for the full-bleed **background
  glaze** behind the call screen, behind `if #available(iOS 18, macOS 15, *)`; fall back to
  two stacked `RadialGradient`s with the same colors. A 3×3 mesh whose center point drifts
  `±0.04` with `sin(t·0.3)` is enough; with Reduce Motion the points stay fixed.
- Use `Canvas(rendersAsynchronously: false)` (default) and `.drawingGroup()` on the
  container if profiling shows overdraw. Target < 3 ms/frame on an iPhone 12.
- Unit-testable bit: `JarPath.make` is a pure function → snapshot the radius at θ = 0, π/2
  for each state.

## 7. iPhone screens

### 7.1 Structure

Three tabs instead of five: **홈** (`circle.circle`), **승인** (`checkmark.shield`, badge =
pending count), **설정** (`gearshape`). Usage and server status live at the top of 설정 as
cards (they're glanced at, not used daily). The call screen is a `fullScreenCover` over the
tab view whenever `CallManager.inCall` is true, so it also appears when CallKit answers an
incoming call from the lock screen.

### 7.2 Home (idle)

Top to bottom, on `bg` with a faint celadon glaze top-center:

1. **Status line** (footnote, text3): dot + "연결됨 · 스파크 42 ms" from `/v1/status`; if any
   service is down it becomes the red error banner (§10).
2. **Greeting** (title, semibold): time-based "좋은 아침이에요" / "좋은 오후예요" / "좋은 저녁이에요",
   date below in subheadline text2 ("11월 21일 금요일").
3. **The jar**, idle state, 0.5 × screen width, centered. Tapping it starts a call too.
4. **Call button**: full-width capsule, 64 pt tall, celadon fill, `onAccent` label
   "아젤에게 전화" with `phone.fill`. Primary action; the only filled element on screen.
5. **Quick actions** (horizontal row of 3 bordered capsules, 44 pt):
   "오늘 브리핑" (`sun.max`, `startCall(prefix: "briefing")`), "새 대화"
   (`square.and.pencil`, `Session.rotate()`, shows a toast "새 대화를 시작해요"),
   "승인 2건" (`checkmark.shield`, only when > 0, switches to the 승인 tab).
6. **최근 대화**: up to 3 cards (`card`, radius 22). Each: time ("오전 9:12"), route chip of the
   last reply, the user's first line (body, text2, 1 line) and the assistant's last reply
   (body, text, 2 lines). Tap → read-only transcript sheet for that session. Source:
   local store (§12 Q1). Empty: "아직 대화가 없어요" + "첫 전화를 걸어 보세요" (text3).

### 7.3 Call screen (listening / thinking / speaking)

Full-bleed, no navigation bar, status bar light content.

- **Background**: `bg` + state glaze (mesh/radial) at 35% opacity, colors cross-fading over
  `state`.
- **Top bar** (safe-area inset, 20 pt margins): left — elapsed time `03:12`
  (footnote, monospacedDigit, text2) with a red dot when recording; center — "아젤"
  (headline); right — route chip of the latest reply (hidden until the first reply).
- **State label** under the top bar (headline, state color + symbol):
  `ear` 듣고 있어요 · `ellipsis` 생각 중 · `waveform` 말하는 중 · `pause.fill` 대기 중 (hold).
- **Jar**: centered in the upper half, `R = 0.32 × width`.
- **Captions** (lower half, above the dock, max 40% of height, scrolls, fades top edge
  with a mask):
  - Last user transcript: body, text2, prefixed by nothing (alignment tells who spoke).
  - Current reply sentence(s): title2 medium, text. Replies of one turn accumulate into a
    paragraph; earlier turns fade to text3 and scroll up.
  - Tap the caption area → sheet with the whole transcript of this call (chat layout:
    user right-aligned text2, assistant left-aligned text, route chip per reply).
- **Control dock** (bottom, safe-area aware): a capsule of `.ultraThinMaterial`, 5 circular
  buttons 56 pt, 12 pt gaps, each with a 13 pt label under it:
  | Control | Symbol off / on | Label | Action |
  |---|---|---|---|
  | Mute | `mic.fill` / `mic.slash.fill` (amber when on) | 음소거 | `toggleMute()` (already through CallKit) |
  | Speaker | `speaker.wave.2` / `speaker.wave.3.fill` | 스피커 | `overrideOutputAudioPort(.speaker / .none)` |
  | Audio route | `airplayaudio` | 출력 | `AVRoutePickerView` wrapped in `UIViewRepresentable`; tint `text` |
  | Record | `record.circle` / `stop.circle.fill` (danger) | 녹음 | local recording, §7.4 |
  | Hold | `pause` / `play.fill` | 대기 | `CXSetHeldCallAction`, §7.4 |
- **Hang up**: 72 pt circle, `hangup` fill, `phone.down.fill` white, centered under the
  dock, 16 pt gap. Long-press not required; single tap hangs up.
- **Interrupt**: see §5. No button needed: talking over 아젤 interrupts it (server VAD).
- **Error during call** (`error` event): a small banner slides under the top bar,
  danger color, 4 s auto-dismiss, VoiceOver announcement.

### 7.4 Record and hold (client-only)

- **Record** writes both sides to `Documents/Recordings/<session>-<date>.m4a` using a tap on
  `mainMixerNode` plus the input node into an `AVAudioFile`. A red dot + "녹음 중" shows in
  the top bar. First use shows a one-line alert: "이 통화를 이 기기에만 녹음해요". Recordings
  are listed in 설정 → 녹음.
- **Hold** = mute mic + pause playback queue (`player.pause()`) + jar goes to a dimmed
  porcelain at 50% with `pause.fill` label "대기 중". The WebSocket stays open and keeps
  receiving; queued audio resumes on unhold. Through `CXSetHeldCallAction` so the system
  UI agrees.

### 7.5 Settings, usage, status (설정 tab)

`ScrollView` of grouped cards on `bg` (not `Form`), 16 pt between cards, card padding 16.
Card header: headline + optional trailing text button.

1. **비서 card** (top): row "이름" with the current `assistant_name` ("아젤") as a trailing
   text field (max 12 characters, trimmed, empty → back to "아젤"). Footer helper (footnote,
   text3): "호출어는 다시 학습하기 전까지 '아젤'로 유지돼요." Saved like the other settings; the
   new name shows on the call screen, CallKit and menus from the next call.
2. **사용량 card**: month ("11월"), big cost `$123.40` (title, monospacedDigit) with
   "/ $500" (text2), a 6 pt capsule progress bar (celadon; amber past `budget_warn_pct`;
   danger past 100%) with a tick at the warn %. Below, `by_model` rows as thin bars
   (label · value). `by_path` collapsed under "경로별 보기".
3. **상태 card**: rows "스파크", "게이트웨이", then devices; each with a 8 pt dot (celadon ok /
   danger down), name, latency right-aligned ("42 ms", monospacedDigit, text2). Pull to refresh.
4. **모델 card**: 기본 모델, Claude 경로 as menu pickers (`Picker` `.menu`), options with
   friendly names (로컬 Qwen, Haiku, Sonnet, Opus, 자동).
5. **작업별 card**: four rows (잡담, 일정, 코드, 맥북 작업), each a row with the model menu and a
   small path menu on the right. No indented "  경로" pseudo-rows.
6. **예산 card**: 월 예산 field (decimal pad, trailing), 경고 기준 stepper "80%".
7. **먼저 전화하기 card**: 아침 브리핑 time, 방해 금지 start–end shown as one row "23:30 – 07:30"
   that opens two compact DatePickers, trigger toggles (마감 임박, 서버 장애).
8. **기기 card**: 기본 기기 (menu from `/v1/devices` names when available, else text field),
   대체 순서 as reorderable chips.
9. **연결 card**: 음성 서버, 게이트웨이 (read-only, monospaced footnote), "토큰 바꾸기",
   "연결 다시 확인".
10. **녹음 card** (only if recordings exist).

Saving: auto-save 600 ms after the last change (debounced `PUT /v1/settings`) with a tiny
"저장됨" checkmark in the card header; on failure the card shows an inline danger line and a
"다시 시도" button. Removes the separate 저장 toolbar button.

### 7.6 Approvals (승인 tab)

One card per pending approval (newest first):
- Header row: device name (headline) + tool chip (`run_command`) + relative time (text3).
- **Command block**: `callout.monospaced`, text, on `bg` inset radius 12, horizontally
  scrollable, never truncated; `cwd` below in footnote text2 with `folder` symbol.
- Buttons, full-width row: **거절** (bordered, danger text) and **승인** (filled celadon). Both
  44 pt tall. Approve asks for confirmation only when the command contains a pattern from
  the risky list (`rm`, `git push`, `sudo`, `>`): a confirmation dialog repeats the
  command. Success → card collapses with a `checkmark` and "승인했어요", haptic success.
- Empty state: small celadon jar outline + "모두 처리했어요" + "승인이 필요하면 알려 드릴게요".
- Unknown extra fields from the server are shown as label: value rows under the command
  (keeps today's forward compatibility).

## 8. macOS

### 8.1 Menu-bar extra (popover)

`MenuBarExtra("Azel", systemImage: …) { … }.menuBarExtraStyle(.window)`, width 340.
Menu-bar icon: a template image of the jar silhouette (16 pt), filled when a call is up,
with a dot when approvals are pending.

Content (`bgRaised`, 16 pt padding):
1. Header: "Azel" headline + status dot and text ("연결됨") on the right.
2. Mini jar (64 pt) + "⌥ Space를 누른 채 말하세요" (subheadline text2) + a celadon
   "전화" button (`phone.fill`) for a hands-free call.
3. Quick actions as a 2-column grid of small bordered buttons: 오늘 브리핑, 새 대화.
4. Approvals row (if any): "승인 2건" → opens the main window on 승인.
5. 최근 대화: 3 compact rows (time · one line of the last reply).
6. Footer: 설정… (⌘,), 종료 (⌘Q), small text buttons.

### 8.2 Floating push-to-talk panel

A non-activating `NSPanel` (`.nonactivatingPanel`, `.hudWindow` not used; level
`.floating`, `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`) hosting
SwiftUI, 460 × 112 pt, radius 30, `.ultraThinMaterial` (Reduce Transparency → `bgRaised`),
bottom-center of the active screen, 80 pt above the Dock.

Layout: jar (68 pt, full state behaviour) left · captions middle (latest reply in title3
medium, user transcript above in footnote text2, both 2 lines, newest wins) · right column:
route chip and a small hang-up button (`xmark`, 28 pt).
- Appears on hotkey down with `enter` motion (scale 0.96 → 1, opacity), listens while the
  key is held, shows thinking/speaking after release, fades out 4 s after `turn_done`
  unless the pointer is over it. Esc or the `xmark` ends the call.
- Hotkey default ⌥Space, configurable in Settings. While held, the panel border is celadon.

### 8.3 Main window

Same tabs as iPhone as a `NavigationSplitView` sidebar (홈, 승인, 설정); content max width 640,
cards in two columns at ≥ 900 pt window width. The call screen on Mac is the home view with
the jar enlarged in place (no full-screen cover); the dock is the same five controls minus
the audio route picker (use the system output) and speaker.

## 9. First-run setup

Three pages in a `TabView(.page)` on iOS / a stepped sheet on Mac, progress dots at the bottom.

1. **환영**: idle jar large, "Azel" wordmark, "아젤에게 전화하듯 말을 걸면 돼요." (title3, text2),
   button "시작하기".
2. **연결**: 음성 서버 and 게이트웨이 URL fields (prefilled from `Config`), 토큰 `SecureField`
   ("키체인에만 저장돼요" footnote). Button "연결 확인" calls `/v1/status`; results appear as
   three check rows (스파크, 게이트웨이, 토큰) with spinner → `checkmark.circle.fill` celadon /
   `xmark.circle.fill` danger + one-line reason. "다음" enabled only when all pass.
3. **권한**: microphone (required, "말을 들으려면 필요해요"), notifications/VoIP calls ("아젤이
   먼저 전화하려면 필요해요"); on Mac also Accessibility/Input Monitoring for the global hotkey.
   Each is a row with a "허용" button that turns into a checkmark. "완료" → home.

## 10. Empty and error states

All use the same pattern: the jar in `offline` style (small, 96 pt), a title (headline),
one sentence (subheadline text2), one primary action. Never raw error strings as the
title; the raw `localizedDescription` goes in a disclosure "자세히" (footnote, selectable).

| Case | Detect | Title | Body | Action |
|---|---|---|---|---|
| Server unreachable | URLError timeout/cannotConnect, or status `ok:false` | 아젤에게 닿지 않아요 | 스파크(또는 게이트웨이)에 연결할 수 없어요. Tailscale이 켜져 있는지 확인해 주세요. | 다시 시도 |
| Wrong token | HTTP 401/403, WS close 4401/1008 | 토큰이 맞지 않아요 | 설정의 토큰을 다시 입력해 주세요. | 토큰 다시 입력 |
| Mic denied | `AVAudioApplication.recordPermission == .denied` | 마이크 권한이 필요해요 | 아젤이 말을 들으려면 마이크를 허용해 주세요. | 설정 열기 (`UIApplication.openSettingsURLString` / x-apple.systempreferences privacy URL) |
| Budget warn | `cost_usd ≥ budget × warn%` | (banner) 이번 달 예산의 85%를 썼어요 | — | 사용량 보기 |
| Call dropped | WS closed unexpectedly | 통화가 끊겼어요 | 네트워크가 잠깐 끊긴 것 같아요. | 다시 걸기 |
| No approvals | empty list | 모두 처리했어요 | 승인이 필요하면 알려 드릴게요. | — |
| No recents | empty store | 아직 대화가 없어요 | 첫 전화를 걸어 보세요. | — |

On home, a server-unreachable state replaces the status line with a danger banner card and
disables the call button (with the reason as its accessibility hint), instead of letting the
call fail.

## 11. Accessibility checklist

- **Dynamic Type**: text styles everywhere; test at AX3. The dock switches to two rows
  of 3 at `dynamicTypeSize >= .accessibility1`; cards' label/value rows stack vertically
  (`ViewThatFits` horizontal → vertical).
- **Contrast**: every text token ≥ 4.5:1 on `bg` and `card` in both modes (table §2).
- **VoiceOver labels** (Korean; `accessibilityLabel` + `accessibilityHint` where useful):

| Element | Label | Value / hint |
|---|---|---|
| Call button | 아젤에게 전화 | hint: 통화를 시작합니다 |
| Home jar | 아젤 | hint: 두 번 탭하면 전화를 겁니다 |
| Call jar | 아젤 | value: 듣고 있어요 / 생각 중 / 말하는 중 / 대기 중 |
| 오늘 브리핑 | 오늘 브리핑 듣기 | — |
| 새 대화 | 새 대화 시작 | hint: 이전 대화 맥락을 비웁니다 |
| Recent card | "오전 9시 12분, 나: …, 아젤: …" (combine children) | hint: 전체 대화 보기 |
| Route chip | 로컬 모델 응답 / Claude 응답 | — |
| Timer | 통화 시간 3분 12초 | — |
| Mute | 음소거 | value: 켜짐/꺼짐 (`.isToggle` trait on iOS 17) |
| Speaker | 스피커 | value: 켜짐/꺼짐 |
| Audio route | 오디오 출력 선택 | — |
| Record | 녹음 | value: 녹음 중/꺼짐 |
| Hold | 통화 대기 | value: 대기 중/통화 중 |
| Hang up | 통화 끊기 | — |
| Caption area | 대화 내용 | hint: 두 번 탭하면 전체 대화 보기 |
| Approval card | "맥북, run_command, 명령: …, 위치: …" | actions: 승인, 거절 (`accessibilityAction(named:)`) |
| 승인 / 거절 | 승인 / 거절 | — |
| Name field | 비서 이름 | value: 아젤; hint: 호출어는 바뀌지 않습니다 |
| Status row | "스파크, 정상, 42밀리초" | — |
| Usage bar | 이번 달 사용량 | value: "500달러 중 123달러, 25퍼센트" |
| PTT panel (Mac) | 아젤 말하기 패널 | — |

- **Announcements**: VoiceOver users already hear the TTS, so don't announce every state
  change. Post `AccessibilityNotification.Announcement` only for errors, hold, and
  interrupts ("말씀하세요"). New user transcript is not announced (they just said it).
  Captions container is `.accessibilityElement(children: .contain)` and not live-updating,
  to avoid talking over the TTS.
- **Reduce Motion / Transparency / Increase Contrast**: §5 and §2.
- **Keyboard (Mac)**: every control reachable with Tab; ⌘N new conversation, ⌘, settings,
  Esc ends PTT call, Space toggles mute in the call view.

## 12. Data and open questions

Everything above uses existing endpoints and local state, except:

1. **Recent conversations on home**. No endpoint lists past calls. Proposal: the app keeps
   its own last 20 calls (session id, start time, lines with route) in a local JSON file /
   SwiftData; no server change. Alternative: `POST /v1/search` on the memory service with
   `kinds:["transcript"]`, but the app doesn't talk to the memory service today and
   transcripts may not be written per call. → **PM decision**: local store for now?
2. **Tap-to-interrupt**. Today interrupt is voice only (server VAD). A "그만" tap on the
   jar while speaking would need a client→server `{"type":"interrupt"}` text message. The
   design works without it; nice to have.
3. **Usage breakdown shape**. `by_model` / `by_path` values are open in PROTOCOL §3. The
   cards assume each value is a number (USD) or an object with `cost_usd`; anything else is
   shown as text.
4. **Mac push-to-talk** with server-side VAD: while the key is up, the client sends
   silence (or nothing) and the server keeps the socket. Confirm the server tolerates gaps
   in the PCM stream (or send zeros).
5. **Approval timestamps**. The card shows relative time if the item has `created`;
   PROTOCOL doesn't define it. Hidden when absent.
6. **Where `assistant_name` lives**. The spec assumes it is a field in `GET/PUT
   /v1/settings` (so the voice server can use it in openings and every device agrees). If it
   is app-local instead, the 비서 card just saves to `UserDefaults`; the UI is the same.
