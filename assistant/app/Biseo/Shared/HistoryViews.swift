import AVFoundation
import SwiftUI

// MARK: - Shared bits

enum HistoryFormat {
    static func meta(_ item: HistoryItem) -> String {
        var parts = [item.start.formatted(date: .omitted, time: .shortened)]
        if let d = item.duration_s { parts.append(d < 60 ? "\(Int(d))초" : "\(Int((d / 60).rounded()))분") }
        if let t = item.turns { parts.append("\(t)턴") }
        return parts.joined(separator: " · ")
    }

    static func dayTitle(_ date: Date, now: Date = Date()) -> String {
        let cal = Calendar.current
        if cal.isDate(date, inSameDayAs: now) { return "오늘" }
        if let y = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(date, inSameDayAs: y) { return "어제" }
        return date.formatted(.dateTime.month().day().weekday(.wide))
    }

    /// Newest day first, keeping the server's order inside each day.
    static func byDay(_ items: [HistoryItem]) -> [(String, [HistoryItem])] {
        var out: [(String, [HistoryItem])] = []
        for item in items {
            let key = dayTitle(item.start)
            if out.last?.0 == key { out[out.count - 1].1.append(item) } else { out.append((key, [item])) }
        }
        return out
    }

    /// From the user's side, like the phone's call log: incoming = the assistant called.
    static func directionLabel(incoming: Bool) -> String { incoming ? "받은 전화" : "건 전화" }
    static func directionGlyph(incoming: Bool) -> String { incoming ? "phone.arrow.down.left" : "phone.arrow.up.right" }

    static let kindNames = ["calendar": "캘린더", "mail": "메일", "memory": "기억", "schedule": "일정", "server": "서버",
                            "files": "파일", "notes": "노션", "web": "웹", "terminal": "명령"]

    static func a11y(_ item: HistoryItem) -> String {
        var parts = [item.start.formatted(date: .omitted, time: .shortened), item.title ?? "제목 없음"]
        if let d = item.duration_s { parts.append("\(max(1, Int((d / 60).rounded())))분") }
        if let t = item.turns { parts.append("\(t)턴") }
        parts.append(directionLabel(incoming: item.incoming))
        if item.routeList.contains("claude") { parts.append("Claude 사용") }
        return parts.joined(separator: ", ")
    }
}

/// Direction glyph: the assistant called (celadon) or the user called.
struct DirectionGlyph: View {
    let incoming: Bool
    var body: some View {
        Image(systemName: HistoryFormat.directionGlyph(incoming: incoming))
            .font(.system(size: 17)).frame(width: 20)
            .foregroundStyle(incoming ? Theme.celadon : Theme.text2)
            .accessibilityLabel(HistoryFormat.directionLabel(incoming: incoming))
    }
}

/// One history row (§15.2).
struct HistoryRow: View {
    let item: HistoryItem
    var query: String = ""
    var selected = false
    var deleting = false
    var failed = false
    var retry: (() -> Void)? = nil

    var body: some View {
        HStack(alignment: .top, spacing: Theme.m) {
            DirectionGlyph(incoming: item.incoming)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title ?? "제목 없음").foregroundStyle(Theme.text).lineLimit(1)
                Text(HistoryFormat.meta(item)).font(.footnote.monospacedDigit()).foregroundStyle(selected ? Theme.text2 : Theme.text3)
                if let match = item.match { Text(highlight(match)).font(.footnote).foregroundStyle(Theme.text2).lineLimit(2) }
                if failed {
                    HStack(spacing: Theme.xs) {
                        Text("지우지 못했어요").foregroundStyle(Theme.danger)
                        if let retry { Button("다시 시도", action: retry).buttonStyle(.borderless).tint(Theme.celadon) }
                    }
                    .font(.footnote)
                }
            }
            Spacer(minLength: Theme.s)
            if deleting { ProgressView().controlSize(.small) } else {
                HStack(spacing: Theme.xs) {
                    if PersonaModel.shared.personas.count > 1, let p = PersonaModel.shared.persona(id: item.persona_id) { PersonaChip(persona: p) }
                    ForEach(item.routeList, id: \.self) { RouteChip(route: $0) }
                    if !Recordings.matching(item).isEmpty { Image(systemName: "waveform").foregroundStyle(Theme.text3).accessibilityLabel("녹음 있음") }
                }
            }
        }
        .padding(.vertical, 6)
        .frame(minHeight: 52)
        .opacity(deleting ? 0.5 : 1)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(HistoryFormat.a11y(item))
        .accessibilityHint("전체 대화 보기")
    }

    /// The query in semibold `text`.
    private func highlight(_ s: String) -> AttributedString {
        var a = AttributedString(s)
        let q = query.trimmingCharacters(in: .whitespaces)
        if !q.isEmpty, let r = a.range(of: q, options: .caseInsensitive) {
            a[r].font = .footnote.weight(.semibold)
            a[r].foregroundColor = Theme.text
        }
        return a
    }
}

/// Home "최근 대화" card for a history item (§7.2, §13.5).
struct HistoryCard: View {
    let item: HistoryItem
    var compact = false

    var body: some View {
        Card {
            Text(item.title ?? "제목 없음").foregroundStyle(Theme.text).lineLimit(1)
            HStack(spacing: Theme.xs) {
                DirectionGlyph(incoming: item.incoming).font(.footnote)
                Text(HistoryFormat.meta(item)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                Spacer()
                ForEach(item.routeList, id: \.self) { RouteChip(route: $0) }
            }
            if !compact, let reply = item.last_reply {
                Text(reply).font(.subheadline).foregroundStyle(Theme.text2).lineLimit(2)
            }
            if !compact, let kinds = item.kinds, !kinds.isEmpty {
                HStack(spacing: Theme.xs) {
                    ForEach(kinds.prefix(3), id: \.self) { Image(systemName: Activity.glyph(kind: $0, tool: nil)) }
                    Text(kinds.compactMap { HistoryFormat.kindNames[$0] }.joined(separator: " · ") + " 확인").lineLimit(1)
                }
                .font(.footnote).foregroundStyle(Theme.text3)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(HistoryFormat.a11y(item))
        .accessibilityHint("전체 대화 보기")
    }
}

/// "기록을 지웠어요 · 실행 취소" (§15.2).
struct UndoToast: View {
    @ObservedObject var model: HistoryModel

    var body: some View {
        if model.pendingDelete != nil {
            HStack(spacing: Theme.m) {
                Text("기록을 지웠어요").foregroundStyle(Theme.text)
                Button("실행 취소") { model.undoDelete() }.fontWeight(.semibold).tint(Theme.celadon)
            }
            .font(.subheadline)
            .padding(.horizontal, Theme.l).padding(.vertical, Theme.m)
            .background(.ultraThinMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(Theme.stroke))
            .padding(.bottom, Theme.l)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

/// Offline banner (§15.4).
struct OfflineBanner: View {
    let since: Date?
    let retry: () -> Void

    var body: some View {
        Card {
            HStack(spacing: Theme.s) {
                Image(systemName: "wifi.slash").foregroundStyle(Theme.amber)
                Text("오프라인이에요" + (since.map { " · \($0.formatted(date: .omitted, time: .shortened))에 받은 기록" } ?? ""))
                    .font(.subheadline).foregroundStyle(Theme.text)
                Spacer()
                Button("다시 시도", action: retry).font(.subheadline).tint(Theme.celadon)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - List (§15.2; Mac sidebar §15.5)

struct HistoryList: View {
    @ObservedObject var model: HistoryModel
    /// Mac: sidebar selection. iOS: nil, rows push the detail.
    var selection: Binding<HistoryItem?>? = nil
    var onCall: (() -> Void)? = nil

    var body: some View {
        Group {
            if !model.loadedOnce && model.items.isEmpty && model.status == .ok {
                List(0..<6, id: \.self) { _ in HistoryRow(item: HistoryItem(id: "p", title: "오늘 일정 뭐 있어?", started_at: Date().timeIntervalSince1970, duration_s: 240, turns: 6)) }
                    .redacted(reason: .placeholder)
            } else if model.items.isEmpty, let q = model.results.map({ _ in model.query }) {
                ContentUnavailableView { Text("‘\(q)’에 맞는 기록이 없어요").font(.subheadline).foregroundStyle(Theme.text2) }
            } else if model.items.isEmpty, model.status == .unavailable {
                EmptyState(title: "기록은 서버 업데이트 후 볼 수 있어요", message: "이 기능은 서버 업데이트 후 사용할 수 있어요.")
            } else if model.items.isEmpty, model.status.isFailed, model.offlineSince == nil {
                EmptyState(title: "기록을 불러올 수 없어요", message: FriendlyError.describe(model.error!).message,
                           detail: model.error?.localizedDescription, action: ("다시 시도", { Task { await model.refresh() } }))
            } else if model.items.isEmpty {
                VStack {
                    EmptyState(title: "아직 대화가 없어요", message: "첫 전화를 걸어 보세요.", action: onCall.map { ("\(AssistantName.current)에게 전화", $0) })
                }
            } else {
                list
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg.ignoresSafeArea())
        .searchable(text: $model.query, prompt: "기록 검색")
        .toolbar {
            if PersonaModel.shared.personas.count > 1 {
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Picker("비서", selection: $model.personaFilter) {
                            Text("모든 비서").tag(String?.none)
                            ForEach(PersonaModel.shared.personas) { Text($0.name).tag(Optional($0.id)) }
                        }
                    } label: {
                        Label(PersonaModel.shared.persona(id: model.personaFilter)?.name ?? "모든 비서", systemImage: "line.3.horizontal.decrease.circle")
                    }
                    .accessibilityLabel("비서별로 보기")
                }
            }
        }
        .refreshable { await model.refresh() }
        .overlay(alignment: .bottom) { UndoToast(model: model).animation(Theme.enter, value: model.pendingDelete) }
        .onDisappear { model.commitPendingDelete() }
    }

    @ViewBuilder private var list: some View {
        let sections = HistoryFormat.byDay(model.items)
        let rows = List(selection: selection) {
            if let since = model.offlineSince {
                OfflineBanner(since: since) { Task { await model.refresh() } }.listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            if model.offlineSearch { Text("오프라인이라 제목만 찾아요").font(.footnote).foregroundStyle(Theme.text3) }
            ForEach(Array(sections.enumerated()), id: \.element.0) { index, section in
                Section {
                    ForEach(section.1) { item in row(item) }
                } header: {
                    Text(section.0).font(.footnote.weight(.semibold)).foregroundStyle(Theme.text2).accessibilityAddTraits(.isHeader)
                }
                .onAppear { if index == sections.count - 1 { Task { await model.loadMore() } } }
            }
        }
        #if os(iOS)
        rows.listStyle(.insetGrouped)
        #else
        rows.listStyle(.sidebar)
        #endif
    }

    @ViewBuilder private func row(_ item: HistoryItem) -> some View {
        let hasRecording = !Recordings.matching(item).isEmpty
        let content = HistoryRow(item: item, query: model.query, selected: selection?.wrappedValue?.id == item.id,
                                 failed: model.failedDelete == item.id, retry: { model.delete(item) })
        Group {
            #if os(iOS)
            NavigationLink(value: item) { content }
            #else
            content.tag(item)
            #endif
        }
        .listRowBackground(Theme.card)
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            if !model.isOffline { Button(role: .destructive) { model.delete(item) } label: { Label("삭제", systemImage: "trash") } }
        }
        .contextMenu {
            Button(role: .destructive) { model.delete(item) } label: { Label("삭제", systemImage: "trash") }.disabled(model.isOffline)
            if hasRecording {
                Button(role: .destructive) { model.delete(item, withRecording: true) } label: { Label("기록과 녹음 지우기", systemImage: "trash.slash") }
                    .disabled(model.isOffline)
                #if os(macOS)
                Button("녹음 Finder에서 보기") { NSWorkspace.shared.activateFileViewerSelecting(Recordings.matching(item)) }
                #endif
            }
        }
        .accessibilityAction(named: "삭제") { model.delete(item) }
    }
}

extension HistoryItem: Hashable {
    func hash(into h: inout Hasher) { h.combine(id) }
}

// MARK: - Detail (§15.3)

struct HistoryDetailView: View {
    @ObservedObject var history: HistoryModel
    @StateObject private var model: HistoryDetailModel
    @StateObject private var player = RecordingPlayer()
    @State private var expanded: Set<Int> = []
    @State private var following = true
    @State private var confirmDelete = false
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(item: HistoryItem, history: HistoryModel) {
        self.history = history
        _model = StateObject(wrappedValue: HistoryDetailModel(item: item))
    }

    private var item: HistoryItem { model.call?.item ?? model.item }
    private var recording: URL? { Recordings.matching(model.item).first }
    /// The turn being played: the last one whose `at` has passed.
    private var currentTurn: Int? {
        guard player.isLoaded, let turns = model.call?.turns else { return nil }
        return turns.lastIndex { ($0.at ?? .infinity) <= player.time }
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            GlazeStatic().ignoresSafeArea()
            content
            if player.isLoaded && !following {
                Button { following = true } label: {
                    Label("현재 위치로", systemImage: "arrow.down.to.line").font(.subheadline)
                        .padding(.horizontal, Theme.l).padding(.vertical, Theme.s)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                .buttonStyle(.plain).padding(.bottom, Theme.l)
            }
        }
        .navigationTitle("")
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if let call = model.call { ShareLink(item: call.plainText) { Image(systemName: "square.and.arrow.up") }.accessibilityLabel("공유") }
                Button { confirmDelete = true } label: { Image(systemName: "trash") }.accessibilityLabel("삭제").disabled(history.isOffline)
            }
        }
        .confirmationDialog("이 기록을 지울까요?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("지우기", role: .destructive) { history.delete(model.item); dismiss() }
            if recording != nil { Button("기록과 녹음 지우기", role: .destructive) { history.delete(model.item, withRecording: true); dismiss() } }
            Button("취소", role: .cancel) {}
        }
        .task { await model.load(); if model.state == .deleted { history.forget(model.item.id) } }
        .onAppear { if let recording { player.load(recording) } }
        .onDisappear { player.stop() }
    }

    @ViewBuilder private var content: some View {
        switch model.state {
        case .deleted:
            EmptyState(title: "이미 지워진 기록이에요", message: "다른 기기에서 지웠을 수 있어요.")
        case .unavailable where model.call == nil:
            EmptyState(title: "이 기록은 아직 이 기기에 없어요", message: "연결되면 다시 불러올게요.", action: ("다시 시도", { Task { await model.load() } }))
        case .loading where model.call == nil:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        default:
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: Theme.xl) {
                        if case .offlineCached(let since) = model.state { OfflineBanner(since: since) { Task { await model.load() } } }
                        header
                        ForEach(Array((model.call?.turns ?? []).enumerated()), id: \.offset) { i, turn in
                            turnView(i, turn).id(i)
                        }
                    }
                    .padding(20)
                    .frame(maxWidth: 640)
                    .frame(maxWidth: .infinity)
                }
                .safeAreaInset(edge: .top) { if player.isLoaded { RecordingPlayerBar(player: player).padding(.horizontal, 20).padding(.vertical, Theme.s) } }
                .simultaneousGesture(DragGesture().onChanged { _ in if player.playing { following = false } })
                .onChange(of: currentTurn) { _, turn in
                    guard following, let turn else { return }
                    if reduceMotion { proxy.scrollTo(turn, anchor: UnitPoint(x: 0.5, y: 0.33)) }
                    else { withAnimation(.smooth) { proxy.scrollTo(turn, anchor: UnitPoint(x: 0.5, y: 0.33)) } }
                }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            Text(item.title ?? "제목 없음").font(.title3.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(3)
            HStack(spacing: Theme.xs) {
                DirectionGlyph(incoming: item.incoming).font(.footnote)
                Text(HistoryFormat.dayTitle(item.start) + " " + HistoryFormat.meta(item)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text2)
                ForEach(item.routeList, id: \.self) { RouteChip(route: $0) }
            }
            if let reason = item.reason, !reason.isEmpty { Text("용건: \(reason)").font(.footnote).foregroundStyle(Theme.text2) }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func turnView(_ i: Int, _ turn: HistoryCall.Turn) -> some View {
        let dim = currentTurn != nil && currentTurn != i
        VStack(alignment: .leading, spacing: Theme.m) {
            if let user = turn.user {
                Text(user).foregroundStyle(dim ? Theme.text3 : Theme.text2)
                    .multilineTextAlignment(.trailing)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.leading, 60)
            }
            if turn.assistant == nil, turn.interrupted == true { InterruptedMark() }
            if let reply = turn.assistant {
                VStack(alignment: .leading, spacing: 6) {
                    if let route = turn.route { RouteChip(route: route) }
                    Text(reply + (turn.interrupted == true ? " …" : "")).font(.title3).foregroundStyle(dim ? Theme.text3 : Theme.text)
                        .textSelection(.enabled)
                    if turn.interrupted == true { InterruptedMark() }
                    if let steps = turn.activity, !steps.isEmpty { trail(i, steps) }
                }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { if let at = turn.at, player.isLoaded { player.seek(at); following = true } }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(currentTurn == i ? .isSelected : [])
    }

    private func trail(_ i: Int, _ steps: [HistoryCall.Step]) -> some View {
        VStack(alignment: .leading, spacing: Theme.xs) {
            Button {
                withAnimation(reduceMotion ? nil : .smooth(duration: 0.3)) { if expanded.contains(i) { expanded.remove(i) } else { expanded.insert(i) } }
            } label: {
                HStack(spacing: Theme.xs) {
                    Image(systemName: "chevron.right").rotationEffect(.degrees(expanded.contains(i) ? 90 : 0))
                    ForEach(Array(steps.prefix(3).enumerated()), id: \.offset) { Image(systemName: Activity.glyph(kind: $0.element.kind, tool: nil)) }
                    Text("\(steps.count)단계")
                }
                .font(.footnote).foregroundStyle(Theme.text3)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("한 일 \(steps.count)단계")
            .accessibilityHint("두 번 탭하면 펼칩니다")
            if expanded.contains(i) {
                ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                    Button { if let at = step.at, player.isLoaded { player.seek(at) } } label: {
                        Label("✓ \(step.label)", systemImage: Activity.glyph(kind: step.kind, tool: nil)).font(.footnote).foregroundStyle(Theme.text2)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

/// The call screen's glaze at 15%, static (§15.3).
private struct GlazeStatic: View {
    var body: some View {
        ZStack {
            Theme.bg
            RadialGradient(colors: [Theme.glazeCeladon.opacity(0.15), .clear], center: .top, startRadius: 0, endRadius: 420)
        }
    }
}

// MARK: - Player (§15.3)

@MainActor
final class RecordingPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published private(set) var isLoaded = false
    @Published private(set) var playing = false
    @Published private(set) var time: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var bars: [Float] = []
    @Published var rate: Float = 1 { didSet { player?.rate = rate } }
    private var player: AVAudioPlayer?
    private var timer: Timer?

    func load(_ url: URL) {
        guard let p = try? AVAudioPlayer(contentsOf: url) else { return }
        p.enableRate = true
        p.delegate = self
        p.prepareToPlay()
        player = p
        duration = p.duration
        isLoaded = true
        bars = Self.waveform(url)
    }

    func toggle() {
        guard let player else { return }
        if player.isPlaying { player.pause(); stopTimer() } else {
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            try? AVAudioSession.sharedInstance().setActive(true)
            #endif
            player.rate = rate
            player.play()
            startTimer()
        }
        playing = player.isPlaying
    }

    func seek(_ t: Double) {
        player?.currentTime = max(0, min(duration, t))
        time = player?.currentTime ?? 0
    }

    func stop() { player?.stop(); stopTimer(); playing = false }

    nonisolated func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.playing = false; self.stopTimer(); self.time = self.duration }
    }

    private func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor in if let p = self?.player { self?.time = p.currentTime } }
        }
    }

    private func stopTimer() { timer?.invalidate(); timer = nil }

    /// 24 RMS bars, normalized, computed once per file.
    static func waveform(_ url: URL, bars count: Int = 24) -> [Float] {
        guard let file = try? AVAudioFile(forReading: url), file.length > 0,
              let buf = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: buf)) != nil, let data = buf.floatChannelData else { return [] }
        let n = Int(buf.frameLength), per = max(1, n / count)
        let rms = (0..<count).map { b -> Float in
            let start = b * per, end = min(n, start + per)
            guard end > start else { return 0 }
            var sum: Float = 0
            for i in start..<end { sum += data[0][i] * data[0][i] }
            return (sum / Float(end - start)).squareRoot()
        }
        let peak = rms.max() ?? 0
        return peak > 0 ? rms.map { $0 / peak } : rms
    }
}

struct RecordingPlayerBar: View {
    @ObservedObject var player: RecordingPlayer

    var body: some View {
        HStack(spacing: Theme.m) {
            Button { player.toggle() } label: {
                Image(systemName: player.playing ? "pause.fill" : "play.fill").foregroundStyle(Theme.onAccent)
                    .frame(width: 44, height: 44).background(Circle().fill(Theme.celadon))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.space, modifiers: [])
            .accessibilityLabel(player.playing ? "일시정지" : "재생")
            .accessibilityValue("\(clock(player.time)) / \(clock(player.duration))")
            GeometryReader { geo in
                let progress = player.duration > 0 ? player.time / player.duration : 0
                HStack(alignment: .center, spacing: 2) {
                    ForEach(Array(player.bars.enumerated()), id: \.offset) { i, v in
                        Capsule().fill(Double(i) / Double(max(1, player.bars.count)) < progress ? Theme.celadon : Theme.stroke)
                            .frame(height: max(3, CGFloat(v) * 26))
                    }
                }
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { g in player.seek(Double(g.location.x / geo.size.width) * player.duration) })
            }
            .frame(height: 30)
            .accessibilityElement()
            .accessibilityLabel("재생 위치")
            .accessibilityValue(clock(player.time))
            .accessibilityAdjustableAction { dir in player.seek(player.time + (dir == .increment ? 10 : -10)) }
            Text("\(clock(player.time)) / \(clock(player.duration))").font(.footnote.monospacedDigit()).foregroundStyle(Theme.text2)
            Button { player.rate = player.rate == 1 ? 1.5 : player.rate == 1.5 ? 2 : 1 } label: {
                Text(player.rate == 1 ? "1×" : player.rate == 1.5 ? "1.5×" : "2×").font(.footnote.weight(.semibold).monospacedDigit())
                    .frame(minWidth: 36)
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("재생 속도")
            .accessibilityValue(player.rate == 1 ? "1배" : player.rate == 1.5 ? "1.5배" : "2배")
        }
        .padding(.horizontal, Theme.m)
        .frame(height: 56)
        .background(Theme.card, in: Capsule())
        .overlay(Capsule().strokeBorder(Theme.stroke))
    }

    private func clock(_ t: Double) -> String { String(format: "%d:%02d", Int(t) / 60, Int(t) % 60) }
}

/// Subtle "끊김" mark on an interrupted turn.
struct InterruptedMark: View {
    var body: some View {
        Label("끊김", systemImage: "scissors").font(.footnote).foregroundStyle(Theme.text3)
            .accessibilityLabel("말이 끊김")
    }
}
