import SwiftUI

/// 채팅: threads | conversation. A split view on Mac and iPad, a stack on iPhone.
struct ChatScreen: View {
    /// Inside a window that is already a split view (the Mac's main window): threads and chat side by side
    /// without nesting another NavigationSplitView.
    var embedded = false
    @StateObject private var threads = ChatThreads()
    @State private var session = ChatSession()
    @State private var selection: String?
    @State private var opening = false
    @State private var confirmDelete: HistoryItem?
    @FocusState private var searchFocused: Bool
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        Group {
            if embedded {
                HStack(spacing: 0) {
                    sidebar.frame(width: 240)
                    Divider()
                    NavigationStack { conversation }
                }
            } else {
                NavigationSplitView { sidebar } detail: { conversation }
            }
        }
        .task { await threads.refresh() }
        .confirmationDialog("이 대화를 삭제할까요?", isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            titleVisibility: .visible, presenting: confirmDelete) { item in
            Button("삭제", role: .destructive) {
                Task { await threads.delete(item) }
                if selection == item.id { newChat() }
            }
        } message: { _ in Text("서버에서도 지워져요. 되돌릴 수 없어요.") }
    }

    private var conversation: some View {
        ChatConversationView(session: session, opening: opening)
            .id(session.session)   // a different thread: fresh view state (scroll, draft)
    }

    private var sidebar: some View {
        List(selection: $selection) {
            Section {
                Button { newChat() } label: { Label("새 채팅", systemImage: "square.and.pencil") }
                    .keyboardShortcut("n", modifiers: .command)
                TextField("대화 검색", text: $threads.query)
                    .focused($searchFocused)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("대화 검색")
                // ⌘K: jump to search (a hidden button carries the shortcut on every platform)
                Button("검색") { searchFocused = true }.keyboardShortcut("k", modifiers: .command).hidden().frame(width: 0, height: 0)
            }
            Section("대화") {
                if threads.shown.isEmpty && !threads.loading {
                    Text(threads.query.isEmpty ? "아직 대화가 없어요" : "찾는 대화가 없어요").foregroundStyle(Theme.text3)
                }
                ForEach(threads.shown) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.title ?? "새 대화").font(.body).foregroundStyle(Theme.text).lineLimit(1)
                        Text(item.start, format: .relative(presentation: .named)).font(.caption).foregroundStyle(Theme.text3)
                    }
                    .tag(item.id)
                    .contextMenu { Button("삭제", role: .destructive) { confirmDelete = item } }
                    .swipeActions { Button("삭제", role: .destructive) { confirmDelete = item } }
                    .accessibilityElement(children: .combine)
                }
            }
            if let e = threads.error { Text(e).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .navigationTitle("채팅")
        .refreshable { await threads.refresh() }
        .onChange(of: selection) { _, id in
            guard let id, let item = threads.threads.first(where: { $0.id == id }), item.session != session.session else { return }
            open(item)
        }
        #if os(macOS)
        .navigationSplitViewColumnWidth(min: 200, ideal: 240)
        #endif
    }

    private func newChat() {
        session.close()
        session = ChatSession()
        selection = nil
    }

    private func open(_ item: HistoryItem) {
        session.close()
        session = ChatSession(session: item.session ?? "chat-" + item.id)
        opening = true
        let target = session
        Task {
            do { target.restore(try await ChatThreads.open(item)) } catch { target.error = "대화를 불러오지 못했어요" }
            opening = false
        }
    }
}

/// One conversation: the messages, an empty state, the composer, and the thread's options.
struct ChatConversationView: View {
    @ObservedObject var session: ChatSession
    var opening = false
    @ObservedObject private var approvals = ApprovalCenter.shared
    @State private var draft = ""
    @State private var editing = false
    @State private var following = true
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            if session.transcript.messages.isEmpty && !opening { emptyState } else { messages }
            if let a = approvals.pending.first { ChatApprovalRow(request: a).padding(.horizontal, Theme.l).padding(.top, Theme.s) }
            if let e = session.error {
                Text(e).font(.footnote).foregroundStyle(Theme.danger).frame(maxWidth: .infinity).padding(.top, Theme.s)
                    .onTapGesture { session.error = nil }
            }
            ChatComposer(text: $draft, editing: $editing, busy: session.busy, focused: $composerFocused,
                         send: { t in editing ? session.editLast(t) : session.send(t); editing = false; following = true },
                         stop: { session.stop() })
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle(session.persona?.name ?? AssistantName.current)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                BrainModeControl(current: session.brainMode, pick: { session.chooseMode($0) })
            }
        }
        .toolRuns(session.runs, target: $session.runTarget)
        .onAppear { composerFocused = true }
    }

    private var messages: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Theme.l) {
                    if opening { ProgressView().frame(maxWidth: .infinity).padding() }
                    ForEach(session.transcript.messages) { m in
                        ChatMessageRow(message: m, session: session, isLast: m.id == session.transcript.messages.last?.id,
                                       isLastUser: m.id == session.transcript.messages.last(where: { $0.role == .user })?.id,
                                       edit: { draft = m.text; editing = true; composerFocused = true })
                            .id(m.id)
                    }
                    Color.clear.frame(height: 1).id(FollowScroll.bottomID)
                }
                .padding(.horizontal, Theme.l).padding(.vertical, Theme.l)
                .frame(maxWidth: 760)   // a reading column on wide windows
                .frame(maxWidth: .infinity)
            }
            .followScroll(proxy, following: $following, trigger: session.transcript.messages.last.map { "\($0.id)-\($0.text.count)-\($0.cards.count)" } ?? "",
                          animated: false) { (FollowScroll.bottomID, .bottom) }
            .overlay(alignment: .bottom) {
                if !following {
                    Button {
                        following = true
                        withAnimation { proxy.scrollTo(FollowScroll.bottomID, anchor: .bottom) }
                    } label: {
                        Label("최신 메시지", systemImage: "arrow.down").font(.footnote.weight(.semibold))
                            .padding(.horizontal, Theme.m).padding(.vertical, Theme.s)
                            .background(.regularMaterial, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .padding(.bottom, Theme.s)
                    .accessibilityLabel("최신 메시지로 이동")
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: Theme.l) {
            Spacer()
            MoonJar(state: .idle, levels: .silent, tint: session.persona?.glaze ?? Theme.glazeCeladon, breathAmplitude: 0.5)
                .frame(width: 96, height: 96).accessibilityHidden(true)
            Text("무엇을 도와드릴까요?").font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
            VStack(spacing: Theme.s) {
                ForEach(Self.starters, id: \.self) { s in
                    Button { session.send(s) } label: {
                        Text(s).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
                            .padding(Theme.m).background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.rowRadius))
                            .overlay(RoundedRectangle(cornerRadius: Theme.rowRadius).strokeBorder(Theme.stroke))
                    }
                    .buttonStyle(.plain).foregroundStyle(Theme.text)
                }
            }
            .frame(maxWidth: 420)
            Spacer()
        }
        .padding(Theme.xl)
        .frame(maxWidth: .infinity)
    }

    static let starters = ["오늘 일정 정리해 줘", "내 종목 어때?", "서버 상태 확인해 줘"]
}

/// One message: the user's as a quiet bubble, the assistant's as prose with its cards, steps and actions.
struct ChatMessageRow: View {
    let message: ChatMessage
    @ObservedObject var session: ChatSession
    var isLast = false
    var isLastUser = false
    var edit: () -> Void = {}
    @State private var hovering = false

    var body: some View {
        if message.role == .user { user } else { assistant }
    }

    private var user: some View {
        HStack {
            Spacer(minLength: 48)
            VStack(alignment: .trailing, spacing: 4) {
                Text(message.text).textSelection(.enabled).foregroundStyle(Theme.text)
                    .padding(.horizontal, Theme.m).padding(.vertical, Theme.s)
                    .background(Theme.celadon.opacity(0.14), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                if (hovering || isLastUser) && !session.busy && isLastUser {
                    actions([("수정", "pencil", edit), ("복사", "doc.on.doc", { copy(message.text) })])
                }
            }
        }
        .onHover { hovering = $0 }
        .contextMenu {
            Button("복사") { copy(message.text) }
            if isLastUser && !session.busy { Button("수정") { edit() } }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("나: \(message.text)")
    }

    private var assistant: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            if !message.steps.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(message.steps, id: \.self) { id in
                        if let run = session.runs.run(id) {
                            Button { session.runTarget = .run(id) } label: { ToolRunRow(run: run) }
                                .buttonStyle(.plain).accessibilityHint("실행 내용 보기")
                        }
                    }
                }
                .padding(Theme.s).background(Theme.bgRaised, in: RoundedRectangle(cornerRadius: Theme.rowRadius))
            }
            if message.text.isEmpty && message.isStreaming {
                HStack(spacing: Theme.s) { ProgressView().controlSize(.small); Text("생각 중").foregroundStyle(Theme.text3) }
                    .accessibilityLabel("답을 준비하고 있어요")
            } else if !message.text.isEmpty {
                ChatMarkdownView(text: message.text).equatable()
            }
            ForEach(Array(message.cards.enumerated()), id: \.offset) { _, c in CardView(card: c) }
            switch message.status {
            case .stopped: Label("중단됨", systemImage: "stop.circle").font(.caption).foregroundStyle(Theme.text3)
            case .failed(let why):
                HStack {
                    Label(why, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Theme.danger)
                    if isLast { Button("다시 시도") { session.retry() }.font(.caption) }
                }
            default: EmptyView()
            }
            if !message.isStreaming && (hovering || isLast) {
                actions([("복사", "doc.on.doc", { copy(message.text) }), ("읽어 주기", "speaker.wave.2", { session.readAloud(message) })]
                        + (isLast && !session.busy ? [("다시 생성", "arrow.clockwise", { session.retry() })] : []))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onHover { hovering = $0 }
        .contextMenu {
            Button("복사") { copy(message.text) }
            Button("읽어 주기") { session.readAloud(message) }
            if isLast && !session.busy { Button("다시 생성") { session.retry() } }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(session.persona?.name ?? AssistantName.current): \(message.text)"
                            + message.cards.map { ", " + $0.summary() }.joined())
    }

    private func actions(_ items: [(String, String, () -> Void)]) -> some View {
        HStack(spacing: Theme.m) {
            ForEach(items, id: \.0) { item in
                Button(action: item.2) { Image(systemName: item.1) }
                    .buttonStyle(.borderless).foregroundStyle(Theme.text3).help(item.0).accessibilityLabel(item.0)
            }
        }
        .font(.caption)
    }

    private func copy(_ s: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string)
        #else
        UIPasteboard.general.string = s
        #endif
    }
}

/// The composer: grows to 8 lines; on the Mac Return sends and Shift-Return adds a line; send becomes stop while
/// a reply streams. Dictation is the system's (keyboard mic on iPhone and iPad, Edit ▸ Start Dictation on the Mac).
struct ChatComposer: View {
    @Binding var text: String
    @Binding var editing: Bool
    var busy: Bool
    var focused: FocusState<Bool>.Binding
    var send: (String) -> Void
    var stop: () -> Void

    var body: some View {
        VStack(spacing: Theme.xs) {
            if editing {
                HStack {
                    Label("마지막 질문 수정 중", systemImage: "pencil").font(.caption).foregroundStyle(Theme.text2)
                    Spacer()
                    Button("취소") { editing = false; text = "" }.font(.caption)
                }
            }
            HStack(alignment: .bottom, spacing: Theme.s) {
                TextField("메시지", text: $text, axis: .vertical)
                    .lineLimit(1...8)
                    .textFieldStyle(.plain)
                    .focused(focused)
                    .padding(.horizontal, Theme.m).padding(.vertical, 10)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Theme.stroke))
                    #if os(macOS)
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) || press.modifiers.contains(.option) { text += "\n"; return .handled }
                        submit(); return .handled
                    }
                    #endif
                    .accessibilityLabel("메시지 입력")
                if busy {
                    Button(action: stop) { Image(systemName: "stop.circle.fill").font(.title) }
                        .buttonStyle(.plain).foregroundStyle(Theme.text2)
                        .keyboardShortcut(".", modifiers: .command)
                        .accessibilityLabel("답 멈추기")
                } else {
                    Button(action: submit) { Image(systemName: "arrow.up.circle.fill").font(.title) }
                        .buttonStyle(.plain).foregroundStyle(Theme.celadon)
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityLabel("보내기")
                }
            }
        }
        .padding(.horizontal, Theme.l).padding(.vertical, Theme.s)
        .frame(maxWidth: 760)
        .frame(maxWidth: .infinity)
        .background(.bar)
    }

    private func submit() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !busy else { return }
        send(t)
        text = ""
    }
}

/// A pending approval inline in the chat: opens the app's approval sheet.
struct ChatApprovalRow: View {
    let request: ApprovalRequest
    var body: some View {
        Button { ApprovalCenter.shared.presented = request } label: {
            HStack(spacing: Theme.s) {
                Image(systemName: "hand.raised.fill").foregroundStyle(Theme.amber)
                VStack(alignment: .leading, spacing: 2) {
                    Text("승인이 필요해요 · \(request.device.isEmpty ? "기기" : request.device)").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                    Text(request.command).font(.caption.monospaced()).foregroundStyle(Theme.text2).lineLimit(1)
                }
                Spacer()
                Text("검토").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.celadon)
            }
            .padding(Theme.m)
            .background(Theme.amber.opacity(0.12), in: RoundedRectangle(cornerRadius: Theme.rowRadius))
        }
        .buttonStyle(.plain)
        .accessibilityHint("승인 화면 열기")
    }
}
