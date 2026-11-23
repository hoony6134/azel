import AVKit
import SwiftUI

/// DESIGN §7.3: full-bleed call screen over the tabs.
struct CallScreen: View {
    @ObservedObject private var call = CallManager.shared
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showTranscript = false
    @State private var following = true  // captions follow the reply until the user scrolls them
    @State private var banner: String?
    @State private var askRecord = false
    @State private var typing = false
    @State private var draft = ""
    @FocusState private var typingFocus: Bool
    @AppStorage("recordingNoticeShown") private var recordingNoticeShown = false
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback
    @AppStorage("noiseMode") private var noiseMode = "normal"

    /// The persona's glaze (§17.6); incoming calls come from the primary.
    private var tint: Color { (call.activePersona ?? PersonaModel.shared.primary)?.glaze ?? Theme.glazeCeladon }

    private var jarState: JarState { JarState(callState: call.displayState, inCall: call.inCall, held: call.held) }

    var body: some View {
        withFeedback
            .sheet(isPresented: $showTranscript) {
                NavigationStack { TranscriptView(lines: call.lines).navigationTitle("대화 내용").navigationBarTitleDisplayMode(.inline) }
                    .presentationDetents([.medium, .large])
            }
            .alert("이 통화를 이 기기에만 녹음해요", isPresented: $askRecord) {
                Button("녹음") { recordingNoticeShown = true; call.toggleRecording() }
                Button("취소", role: .cancel) {}
            }
            .approvalSheet(active: call.inCall)
    }

    private var withFeedback: some View {
        screen
            .preferredColorScheme(.dark)
            .sensoryFeedback(.impact(weight: .light), trigger: jarState) { (old: JarState, new: JarState) -> Bool in
                old == .listening && new == .thinking
            }
            .sensoryFeedback(.warning, trigger: call.error) { (_: String?, new: String?) -> Bool in new != nil }
            .onChange(of: call.error) { _, new in showBanner(new) }
            .onChange(of: call.held) { _, held in AccessibilityNotification.Announcement(held ? "대기 중" : "통화 재개").post() }
    }

    private func showBanner(_ message: String?) {
        guard let message else { return }
        withAnimation(Theme.enter) { banner = message }
        AccessibilityNotification.Announcement(message).post()
        Task { try? await Task.sleep(nanoseconds: 4_000_000_000); withAnimation { banner = nil } }
    }

    private var screen: some View {
        ZStack {
            GlazeBackground(state: jarState, tint: tint)
            if call.secret { Color.black.opacity(0.35).ignoresSafeArea().allowsHitTesting(false) }   // secret: a darker room
            VStack(spacing: Theme.l) {
                topBar
                if let c = call.callContext {
                    Label(c.chip, systemImage: c.kind == "task" ? "checklist" : "bell").font(.footnote.weight(.semibold))
                        .foregroundStyle(Theme.text).lineLimit(1)
                        .padding(.horizontal, Theme.m).padding(.vertical, 4)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                if let banner {
                    Label(banner, systemImage: "exclamationmark.triangle.fill").font(.subheadline).foregroundStyle(Theme.danger)
                        .padding(.horizontal, Theme.l).padding(.vertical, Theme.s)
                        .background(.ultraThinMaterial, in: Capsule())
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                // One Text (icon inline) so the crossfade swaps a single label; a Label with
                // contentTransition left the old state drawn under the new one (user bug, 2026-11-22).
                HStack(spacing: Theme.s) {
                    Text("\(Image(systemName: jarState.symbol)) \(jarState.spokenLabel)").font(.headline).foregroundStyle(jarState.color)
                        .contentTransition(.opacity)
                        .animation(reduceMotion ? nil : Theme.quick, value: jarState)
                        .accessibilityLabel(jarState.spokenLabel)
                    if call.muted { MutedChip() }
                }
                GeometryReader { geo in
                    MoonJar(state: jarState, levels: call.levels, ripple: call.interruptedAt, surge: call.surgeAt, pulse: call.pulseAt, shimmerAt: call.shimmerAt, tint: tint, muted: call.muted).frame(width: geo.size.width, height: geo.size.height)
                        .contentShape(Rectangle())
                        .onTapGesture { call.interrupt() }
                        .brainModeMenu(current: call.brainMode) { call.chooseBrain($0) }
                        .accessibilityHint(jarState == .speaking ? "두 번 탭하면 말을 끊습니다" : "")
                        .accessibilityAction(named: "말 끊기") { call.interrupt() }
                }
                .frame(maxHeight: typeSize >= .accessibility1 ? 140 : .infinity)
                if !call.activity.isEmpty || !call.runs.runs.isEmpty {
                    Button { call.runTarget = call.activity.tapTarget(call.runs) } label: {
                        if call.activity.isEmpty { Label("실행한 도구 \(call.runs.runs.count)개", systemImage: "terminal").font(.footnote).foregroundStyle(Theme.text3) }
                        else { ActivityRow(activity: call.activity) }
                    }
                    .buttonStyle(.plain).accessibilityHint("실행 내용 보기")
                    .toolRuns(call.runs, target: $call.runTarget)
                }
                if noiseMode == "noisy" && jarState == .speaking {
                    Label("시끄러운 곳 모드 · 구를 누르면 말을 끊어요", systemImage: "hand.tap").font(.footnote).foregroundStyle(Theme.text3)
                }
                captions
                typingRow
                dock
                hangUp
            }
            .padding(.horizontal, 20)
            .padding(.bottom, Theme.s)
        }
    }

    private var topBar: some View {
        HStack {
            TimelineView(.periodic(from: .now, by: 1)) { ctx in
                let t = Int(ctx.date.timeIntervalSince(call.connectedAt ?? ctx.date))
                HStack(spacing: Theme.xs) {
                    if call.recording { Circle().fill(Theme.danger).frame(width: 8, height: 8).accessibilityLabel("녹음 중") }
                    Text(String(format: "%02d:%02d", t / 60, t % 60)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text2)
                        .accessibilityLabel("통화 시간 \(t / 60)분 \(t % 60)초")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            VStack(spacing: 2) {
                HStack(spacing: 6) {
                    if let p = call.activePersona, PersonaModel.shared.personas.count > 1 { Circle().fill(p.tint).frame(width: 8, height: 8) }
                    Text(call.callerName ?? name).font(.headline).foregroundStyle(Theme.text)
                }
                BrainModeControl(current: call.brainMode, pick: { call.chooseBrain($0) },
                                 secret: call.secret, canSecret: call.canSecret, setSecret: { call.setSecret($0) },
                                 auto: call.autoOn, autoExpires: call.autoExpires, canAuto: call.canAuto,
                                 setAuto: { call.setAuto($0, minutes: $1, devices: $2) })
            }
            Group {
                if let r = call.log.lastRoute { RouteChip(route: r) }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.top, Theme.s)
    }

    /// The sentence being heard while a reply plays, otherwise the bottom.
    private var captionTarget: (id: AnyHashable, anchor: UnitPoint) {
        if let last = call.lines.last, !last.mine, call.playing {
            let k = call.karaoke(stepped: reduceMotion)
            if let cur = k.current, k.texts.joined(separator: " ") == last.text { return (last.id, KaraokeText.follow(k.texts, current: cur)) }
        }
        return (FollowScroll.bottomID, .bottom)
    }

    /// Last user line in text2, the reply paragraph large; older turns dim and scroll up.
    private var captions: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: Theme.m) {
                    ForEach(Array(call.lines.enumerated()), id: \.element.id) { i, line in
                        let recent = i >= call.lines.count - 2
                        Group {
                            if !line.mine && i == call.lines.count - 1 {
                                // The paragraph being spoken: bright as it is heard, dimmed ahead.
                                TimelineView(.animation(minimumInterval: 1 / 20, paused: !call.playing)) { _ in
                                    let k = call.karaoke(stepped: reduceMotion)
                                    if k.texts.joined(separator: " ") == line.text {
                                        KaraokeText(text: line.text, heard: k.spoken, color: line.interrupted ? Theme.text3 : Theme.text)
                                            .onChange(of: k.current) { _, cur in
                                                guard following, let cur else { return }
                                                withAnimation(Theme.caption) { proxy.scrollTo(line.id, anchor: KaraokeText.follow(k.texts, current: cur)) }
                                            }
                                    } else {
                                        Text(line.text + (line.interrupted ? " …" : ""))
                                    }
                                }
                            } else if line.typed == true {
                                TypedCaption.text(line).accessibilityLabel("입력: \(line.text)")
                            } else {
                                Text(line.text + (line.interrupted ? " …" : ""))
                            }
                        }
                            .font(line.mine ? .body : .title2.weight(.medium))
                            .lineSpacing(line.mine ? 4 : 6)
                            .foregroundStyle(line.mine ? Theme.text2 : (recent && !line.interrupted ? Theme.text : Theme.text3))
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: .infinity)
                            .id(line.id)
                            .transition(reduceMotion ? .opacity : .opacity.combined(with: .offset(y: 6)))
                        ForEach(Array(line.cards.enumerated()), id: \.offset) { _, c in CardView(card: c) }   // under its reply
                    }
                    Color.clear.frame(height: 1).id(FollowScroll.bottomID)
                }
                .padding(.vertical, Theme.m)
                .animation(Theme.caption, value: call.lines)
            }
            .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.18)], startPoint: .top, endPoint: .bottom))
            .background(Theme.bg.opacity(0.55).blur(radius: 20))
            .frame(maxHeight: UIScreen.main.bounds.height * 0.3)
            .followScroll(proxy, following: $following, trigger: call.lines.last?.id) { captionTarget }
            .onChange(of: call.lines.last?.id) { if call.lines.last?.mine == true { following = true } }   // the user spoke: a new turn
            .contentShape(Rectangle())
            .onTapGesture { showTranscript = true }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("대화 내용")
            .accessibilityHint("두 번 탭하면 전체 대화 보기")
        }
    }

    /// Typing during the call (the server lists `text` in its caps); the mic stays as it is.
    @ViewBuilder private var typingRow: some View {
        if call.canType {
            if typing {
                CallTypingField(text: $draft, focus: $typingFocus, send: { call.sendTyped($0, speak: $1) },
                                close: { typing = false; typingFocus = false })
                    .padding(.horizontal, Theme.m).padding(.vertical, Theme.s)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: Theme.rowRadius, style: .continuous))
            } else {
                Button { typing = true; typingFocus = true } label: {
                    Label("입력하기", systemImage: "keyboard").font(.footnote).foregroundStyle(Theme.text2)
                }
                .buttonStyle(.plain)
                .accessibilityHint("키보드로 말을 입력해요")
            }
        }
    }

    private var dock: some View {
        let buttons = Group {
            dockButton("음소거", call.muted ? "mic.slash.fill" : "mic.fill", on: call.muted, onColor: Theme.amber) { call.toggleMute() }
            dockButton("스피커", call.speaker ? "speaker.wave.3.fill" : "speaker.wave.2", on: call.speaker) { call.toggleSpeaker() }
            VStack(spacing: Theme.xs) {
                RoutePicker().frame(width: 56, height: 56)
                    .background(Circle().fill(Theme.card.opacity(0.6)))
                    .accessibilityLabel("오디오 출력 선택")
                Text("출력").font(.footnote).foregroundStyle(Theme.text2)
            }
            dockButton("녹음", call.secret ? "record.circle" : (call.recording ? "stop.circle.fill" : "record.circle"), on: call.recording, onColor: Theme.danger,
                       value: call.recording ? "녹음 중" : "꺼짐") {
                if call.recording || recordingNoticeShown { call.toggleRecording() } else { askRecord = true }
            }
            dockButton("대기", call.held ? "play.fill" : "pause", on: call.held, a11y: "통화 대기",
                       value: call.held ? "대기 중" : "통화 중") { call.toggleHold() }
        }
        return Group {
            if typeSize >= .accessibility1 {
                VStack(spacing: Theme.m) { HStack(spacing: Theme.m) { buttons } }
            } else {
                HStack(spacing: Theme.m) { buttons }
            }
        }
        .padding(.horizontal, Theme.l).padding(.vertical, Theme.m)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(Theme.stroke, lineWidth: 0.5))
    }

    private func dockButton(_ title: String, _ icon: String, on: Bool, onColor: Color = Theme.celadon,
                            a11y: String? = nil, value: String? = nil, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: Theme.xs) {
                Image(systemName: icon).font(.title3)
                    .foregroundStyle(on ? Theme.onAccent : Theme.text)
                    .frame(width: 56, height: 56)
                    .background(Circle().fill(on ? onColor : Theme.card.opacity(0.6)))
                Text(title).font(.footnote).foregroundStyle(Theme.text2)
            }
        }
        .buttonStyle(.plain)
        .animation(Theme.quick, value: on)
        .accessibilityLabel(a11y ?? title)
        .accessibilityValue(value ?? (on ? "켜짐" : "꺼짐"))
        .accessibilityAddTraits(.isToggle)
    }

    private var hangUp: some View {
        Button { call.hangUp() } label: {
            Image(systemName: "phone.down.fill").font(.title).foregroundStyle(.white)
                .frame(width: 72, height: 72).background(Circle().fill(Theme.hangup))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("통화 끊기")
    }
}

/// AirPods / Bluetooth / receiver / speaker picker.
struct RoutePicker: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let v = AVRoutePickerView()
        v.prioritizesVideoDevices = false
        v.tintColor = UIColor(Theme.text)
        v.activeTintColor = UIColor(Theme.celadon)
        return v
    }
    func updateUIView(_ view: AVRoutePickerView, context: Context) {}
}
