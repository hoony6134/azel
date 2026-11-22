import CoreMotion
import SwiftUI

/// DESIGN §7.2: status, greeting, idle jar, call capsule, quick actions, recent calls.
struct HomeView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var history: HistoryModel
    @ObservedObject var schedule: ScheduleModel
    @Binding var tab: Int
    @ObservedObject private var call = CallManager.shared
    @State private var toast: String?
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback
    @ObservedObject private var personas = PersonaModel.shared

    @StateObject private var tilt = TiltModel()
    @ObservedObject private var brain = BrainModeChoice.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    /// CoreMotion only while the home screen is really in view: foreground, no call cover, motion allowed.
    private var tilting: Bool { TiltModel.shouldRun(active: scenePhase == .active, inCall: call.inCall, reduceMotion: reduceMotion, visible: homeVisible) }
    @State private var homeVisible = false

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.xl) {
                statusLine
                VStack(spacing: Theme.xs) {
                    Text(greeting()).font(.title.weight(.semibold)).lineSpacing(4).foregroundStyle(Theme.text)
                    Text(Date(), format: .dateTime.month().day().weekday(.wide)).font(.subheadline).foregroundStyle(Theme.text2)
                }
                GeometryReader { geo in
                    MoonJar(state: model.statusError == nil ? .idle : .offline, levels: .silent,
                            tint: personas.selected?.glaze ?? Theme.glazeCeladon)
                        .animation(Theme.state, value: personas.selectedID)
                        .offset(x: tilt.offset.width, y: tilt.offset.height)
                        .frame(width: geo.size.width, height: geo.size.width)
                        .contentShape(Circle())
                        .onTapGesture { if model.statusError == nil { call.startCall() } }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityHint("두 번 탭하면 전화를 겁니다")
                }
                .aspectRatio(1, contentMode: .fit)
                .padding(.horizontal, 60)
                MiniOrbRow(model: personas)
                callButton
                if let e = call.error, !call.inCall {
                    Label(e, systemImage: "exclamationmark.triangle.fill").font(.subheadline).foregroundStyle(Theme.danger)
                        .multilineTextAlignment(.leading)
                        .accessibilityElement(children: .combine)
                }
                quickActions
                ScheduleCard(schedule: schedule)
                RunningTasksCard(tasks: TaskModel.shared, app: model)
                recents
            }
            .padding(.horizontal, 20)
            .padding(.vertical, Theme.l)
        }
        .background(alignment: .top) {
            ZStack(alignment: .top) {
                Theme.bg
                RadialGradient(colors: [(personas.selected?.glaze ?? Theme.glazeCeladon).opacity(0.18), .clear], center: .top, startRadius: 0, endRadius: 380)
                    .animation(Theme.state, value: personas.selectedID)
            }
            .ignoresSafeArea()
        }
        .toolbar(.hidden, for: .navigationBar)
        .onAppear { homeVisible = true }
        .onDisappear { homeVisible = false }
        .onChange(of: tilting, initial: true) { _, on in on ? tilt.start() : tilt.stop() }
        .overlay(alignment: .bottom) {
            if let toast {
                Text(toast).font(.subheadline).foregroundStyle(Theme.text)
                    .padding(.horizontal, Theme.l).padding(.vertical, Theme.s)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, Theme.l)
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .navigationDestination(for: HistoryItem.self) { HistoryDetailView(item: $0, history: history) }
        .refreshable { await model.refresh(); await history.refresh(); await schedule.refresh(); await personas.refresh() }
    }

    @ViewBuilder private var statusLine: some View {
        if let e = model.statusError {
            let f = FriendlyError.describe(e)
            Card {
                Label(f.title, systemImage: "exclamationmark.triangle.fill").font(.headline).foregroundStyle(Theme.danger)
                Text(f.message).font(.subheadline).foregroundStyle(Theme.text2)
                Button("다시 시도") { Task { await model.refresh() } }.font(.subheadline).tint(Theme.celadon)
            }
        } else if let s = model.status {
            HStack(spacing: Theme.s) {
                StatusDot(ok: model.online)
                Text(model.online ? "연결됨 · spark \(Int(s.spark.ms ?? 0)) ms" : "일부 서비스가 응답하지 않아요")
                    .font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
            }
            .accessibilityElement(children: .combine)
        } else {
            Text(" ").font(.footnote)
        }
    }

    private var callButton: some View {
        Button { call.startCall() } label: {
            Label("\(personas.selected?.name ?? name)에게 전화", systemImage: "phone.fill")
                .font(.headline).foregroundStyle(Theme.onAccent)
                .frame(maxWidth: .infinity).frame(height: 64)
                .background(personas.personas.count > 1 ? (personas.selected?.tint ?? Theme.celadon) : Theme.celadon,
                            in: RoundedRectangle(cornerRadius: Theme.callButtonRadius, style: .continuous))
        }
        .buttonStyle(.plain)
        .brainModeMenu(current: brain.next?.rawValue, pick: { brain.next = $0 },
                       secretCall: { brain.nextSecret = true; call.startCall() })
        .disabled(model.statusError != nil)
        .opacity(model.statusError != nil ? 0.5 : 1)
        .accessibilityHint(model.statusError != nil ? FriendlyError.describe(model.statusError!).title : "통화를 시작합니다")
        .overlay(alignment: .bottom) {
            if let n = brain.next, n != .auto {
                Text("다음 통화: \(n.label)").font(.footnote).foregroundStyle(Theme.text3).offset(y: 22)
            }
        }
    }

    private var quickActions: some View {
        HStack(spacing: Theme.s) {
            // §17.3: the briefing always comes from the primary.
            let briefingTitle = personas.personas.count > 1 && personas.selected?.id != personas.primary?.id
                ? "\(personas.primary?.name ?? name)의 오늘 브리핑" : "오늘 브리핑"
            quick(briefingTitle, "sun.max", label: briefingTitle + " 듣기") { call.startCall(prefix: "briefing", persona: personas.primary) }
            quick("새 대화", "square.and.pencil", label: "새 대화 시작") {
                model.newConversation()
                withAnimation(Theme.quick) { toast = "새 대화를 시작해요" }
                Task { try? await Task.sleep(nanoseconds: 2_000_000_000); withAnimation { toast = nil } }
            }
            .accessibilityHint("이전 대화 맥락을 비웁니다")
            if !model.approvals.isEmpty {
                quick("승인 \(model.approvals.count)건", "checkmark.shield", label: "승인 \(model.approvals.count)건") { tab = 2 }
            }
        }
    }

    private func quick(_ title: String, _ icon: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.subheadline).foregroundStyle(Theme.text)
                .lineLimit(1).minimumScaleFactor(0.8)
                .frame(maxWidth: .infinity).frame(minHeight: 44)
                .overlay(Capsule().strokeBorder(Theme.stroke))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    /// Top 3 of /v1/history (§7.2), the cached list when offline.
    @ViewBuilder private var recents: some View {
        VStack(alignment: .leading, spacing: Theme.m) {
            HStack {
                Text("최근 대화").font(.headline).foregroundStyle(Theme.text)
                Spacer()
                if !history.recent.isEmpty { Button("모두 보기") { tab = 1 }.font(.subheadline).tint(Theme.celadon) }
            }
            if history.recent.isEmpty {
                VStack(spacing: Theme.xs) {
                    Text("아직 대화가 없어요").foregroundStyle(Theme.text2)
                    Text("첫 전화를 걸어 보세요").font(.footnote).foregroundStyle(Theme.text3)
                }
                .frame(maxWidth: .infinity).padding(.vertical, Theme.l)
            }
            ForEach(history.recent) { item in
                NavigationLink(value: item) { HistoryCard(item: item) }.buttonStyle(.plain)
            }
        }
    }
}

/// Home orb parallax (user request, 2026-11-22): the orb leans a few points the way the phone tilts,
/// then drifts back to centre as the new pose becomes the rest pose. Off with Reduce Motion.
@MainActor
final class TiltModel: ObservableObject {
    @Published private(set) var offset: CGSize = .zero
    private let motion = CMMotionManager()
    private var follow = TiltFollower()

    nonisolated static func shouldRun(active: Bool, inCall: Bool, reduceMotion: Bool, visible: Bool) -> Bool {
        active && !inCall && !reduceMotion && visible
    }

    func start() {
        guard motion.isDeviceMotionAvailable, !motion.isDeviceMotionActive else { return }
        motion.deviceMotionUpdateInterval = 1.0 / 30
        motion.startDeviceMotionUpdates(to: .main) { [weak self] m, _ in
            guard let self, let g = m?.gravity else { return }
            let o = self.follow.step(x: g.x, y: g.y)
            withAnimation(.interactiveSpring(response: 0.35, dampingFraction: 0.8)) { self.offset = CGSize(width: o.x, height: o.y) }
        }
    }

    func stop() {
        motion.stopDeviceMotionUpdates()
        follow = TiltFollower()
        withAnimation(.spring(duration: 0.4)) { offset = .zero }
    }
}

/// Gravity in, orb offset in points out. The rest pose follows slowly, so only changes show.
struct TiltFollower {
    static let gain = 40.0, limit = 14.0, drift = 0.02
    private var rest: (x: Double, y: Double)?

    mutating func step(x: Double, y: Double) -> (x: Double, y: Double) {
        guard var r = rest else { rest = (x, y); return (0, 0) }
        r.x += (x - r.x) * Self.drift
        r.y += (y - r.y) * Self.drift
        rest = r
        func clamp(_ v: Double) -> Double { max(-Self.limit, min(Self.limit, v)) }
        // Screen y grows downward; tilting the top away (gravity.y rising) moves the orb up.
        return (clamp((x - r.x) * Self.gain), clamp(-(y - r.y) * Self.gain))
    }
}
