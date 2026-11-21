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

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.xl) {
                statusLine
                VStack(spacing: Theme.xs) {
                    Text(greeting()).font(.title.weight(.semibold)).lineSpacing(4).foregroundStyle(Theme.text)
                    Text(Date(), format: .dateTime.month().day().weekday(.wide)).font(.subheadline).foregroundStyle(Theme.text2)
                }
                GeometryReader { geo in
                    MoonJar(state: model.statusError == nil ? .idle : .offline, levels: .silent)
                        .frame(width: geo.size.width, height: geo.size.width)
                        .contentShape(Circle())
                        .onTapGesture { if model.statusError == nil { call.startCall() } }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityHint("두 번 탭하면 전화를 겁니다")
                }
                .aspectRatio(1, contentMode: .fit)
                .padding(.horizontal, 60)
                PersonaSwitcher(model: personas)
                callButton
                if let e = call.error, !call.inCall {
                    Label(e, systemImage: "exclamationmark.triangle.fill").font(.subheadline).foregroundStyle(Theme.danger)
                        .multilineTextAlignment(.leading)
                        .accessibilityElement(children: .combine)
                }
                quickActions
                ScheduleCard(schedule: schedule)
                recents
            }
            .padding(.horizontal, 20)
            .padding(.vertical, Theme.l)
        }
        .background(alignment: .top) {
            ZStack(alignment: .top) {
                Theme.bg
                RadialGradient(colors: [Theme.glazeCeladon.opacity(0.18), .clear], center: .top, startRadius: 0, endRadius: 380)
            }
            .ignoresSafeArea()
        }
        .toolbar(.hidden, for: .navigationBar)
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
        .disabled(model.statusError != nil)
        .opacity(model.statusError != nil ? 0.5 : 1)
        .accessibilityHint(model.statusError != nil ? FriendlyError.describe(model.statusError!).title : "통화를 시작합니다")
    }

    private var quickActions: some View {
        HStack(spacing: Theme.s) {
            quick("오늘 브리핑", "sun.max", label: "오늘 브리핑 듣기") { call.startCall(prefix: "briefing") }
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
