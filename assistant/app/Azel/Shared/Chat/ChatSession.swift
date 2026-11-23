import Foundation
import AVFoundation

/// One chat thread on the call socket in text-only mode (`/v1/call?intent=chat&audio=0`, no new public route).
/// Events are batched into the transcript at ~12 Hz (one redraw per batch, not per token); a generation number
/// keeps a closed socket from writing into the thread that replaced it.
@MainActor
final class ChatSession: ObservableObject {
    @Published private(set) var transcript = ChatTranscript()
    @Published private(set) var connected = false
    @Published var error: String?
    /// The server's `hello` caps ("chat", "attachments", "retry" …).
    @Published private(set) var caps: Set<String> = []
    @Published private(set) var brainMode: String?
    let runs = ToolRunStore()
    @Published var runTarget: ToolRunTarget?

    /// The thread's session id on the server ("chat-<uuid>"): reopening it resumes the thread.
    let session: String
    var persona: Persona?
    private var socket: URLSessionWebSocketTask?
    private var generation = 0
    private var pending: [CallEvent] = []
    private var flushScheduled = false
    private let speaker = AVSpeechSynthesizer()

    init(session: String = "chat-" + UUID().uuidString.lowercased(), transcript: ChatTranscript = ChatTranscript(), persona: Persona? = nil) {
        self.session = session
        self.transcript = transcript
        self.persona = persona ?? PersonaModel.shared.selected
    }

    deinit { socket?.cancel(with: .normalClosure, reason: nil) }

    var busy: Bool { transcript.busy }

    /// A reopened thread's past messages (the server resumes its context on the same session).
    func restore(_ t: ChatTranscript) { if transcript.messages.isEmpty { transcript = t } }

    // MARK: user actions

    func send(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        transcript.send(text)
        sendFrame(["type": "text", "text": text, "speak": false])
    }

    func stop() {
        guard busy else { return }
        sendFrame(["type": "interrupt"])
        transcript.apply(CallEvent(type: "interrupt"))   // shown at once; the server's own interrupt is idempotent
    }

    /// Regenerate the last reply: the server's `retry` when it offers one, else the last question again.
    func retry() {
        guard !busy, let last = transcript.messages.last(where: { $0.role == .user })?.text else { return }
        transcript.retry()
        if caps.contains("retry") { sendFrame(["type": "retry"]) } else {
            transcript.editLast(last)
            sendFrame(["type": "text", "text": last, "speak": false])
        }
    }

    func editLast(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        transcript.editLast(text)
        if caps.contains("edit_last") { sendFrame(["type": "edit_last", "text": text]) }
        else { sendFrame(["type": "text", "text": text, "speak": false]) }
    }

    func chooseMode(_ m: BrainMode) { brainMode = m.rawValue; sendFrame(["type": "mode", "value": m.rawValue]) }

    /// "읽어 주기": on-device speech, nothing from the server.
    func readAloud(_ message: ChatMessage) {
        speaker.stopSpeaking(at: .immediate)
        let u = AVSpeechUtterance(string: ChatSession.plain(message.text))
        u.voice = AVSpeechSynthesisVoice(language: "ko-KR")
        speaker.speak(u)
    }

    func close() {
        generation += 1
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        connected = false
    }

    // MARK: socket

    private func sendFrame(_ obj: [String: Any]) {
        if socket == nil { connect() }
        guard let socket, let data = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: data, encoding: .utf8) else { return }
        socket.send(.string(s)) { [weak self] error in
            guard let error else { return }
            Task { @MainActor in self?.lost(error.localizedDescription) }
        }
    }

    private func connect() {
        guard let token = Config.token else { error = "설정에서 토큰을 입력해 주세요"; return }
        do {
            let req = try Self.request(voiceURL: Config.voiceURL, token: token, session: session, persona: persona?.id, mode: brainMode)
            let task = URLSession.shared.webSocketTask(with: req)
            generation += 1
            socket = task
            task.resume()
            connected = true
            error = nil
            receive(task, generation: generation)
        } catch {
            self.error = "연결 주소가 올바르지 않아요"
        }
    }

    /// The call request with chat's text-only switches (and without the call-only `duck`).
    nonisolated static func request(voiceURL: String, token: String, session: String, persona: String?, mode: String?) throws -> URLRequest {
        var req = try CallClient.request(voiceURL: voiceURL, token: token, session: session, callID: nil, intent: "chat", persona: persona, mode: mode)
        guard let url = req.url, var c = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return req }
        c.queryItems = (c.queryItems ?? []).filter { $0.name != "duck" } + [URLQueryItem(name: "audio", value: "0")]
        req.url = c.url
        return req
    }

    private func receive(_ task: URLSessionWebSocketTask, generation gen: Int) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, gen == self.generation else { return }   // a replaced socket: ignore
                switch result {
                case .success(.string(let text)):
                    if let e = try? JSONDecoder().decode(CallEvent.self, from: Data(text.utf8)) { self.enqueue(e) }
                    self.receive(task, generation: gen)
                case .success:
                    self.receive(task, generation: gen)   // no audio in chat; ignore anything binary
                case .failure(let e):
                    self.lost(e.localizedDescription)
                }
            }
        }
    }

    private func enqueue(_ e: CallEvent) {
        if let caps = e.caps { self.caps = Set(caps) }
        if let m = CallClient.reportedMode(e) { brainMode = m }
        pending.append(e)
        guard !flushScheduled else { return }
        flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in self?.flush() }
    }

    func flush() {
        flushScheduled = false
        let events = pending
        pending = []
        var t = transcript
        for e in events { t.apply(e); runs.apply(e) }
        transcript = t   // one publish per batch
    }

    private func lost(_ message: String) {
        flush()
        socket = nil
        connected = false
        if busy { transcript.connectionLost("연결이 끊겼어요. 다시 시도해 주세요") }
        Log.call.notice("chat socket closed: \(message, privacy: .public)")
    }

    /// Markdown as plain text for speech: no fences, markers or link targets.
    nonisolated static func plain(_ markdown: String) -> String {
        var s = markdown.replacingOccurrences(of: "```[\\s\\S]*?```", with: " 코드 생략. ", options: .regularExpression)
        s = s.replacingOccurrences(of: "\\[([^\\]]+)\\]\\([^)]+\\)", with: "$1", options: .regularExpression)
        s = s.replacingOccurrences(of: "[*_`#>|]", with: "", options: .regularExpression)
        return s
    }
}

/// The chat threads list: server-side, from the history routes. Chat threads are sessions named "chat-…"
/// (`?kind=chat` when the gateway filters; the session prefix otherwise).
@MainActor
final class ChatThreads: ObservableObject {
    @Published private(set) var threads: [HistoryItem] = []
    @Published private(set) var loading = false
    @Published var error: String?
    @Published var query = ""

    var shown: [HistoryItem] {
        let q = query.trimmingCharacters(in: .whitespaces)
        return q.isEmpty ? threads : threads.filter { ($0.title ?? "").localizedCaseInsensitiveContains(q) || ($0.last_reply ?? "").localizedCaseInsensitiveContains(q) }
    }

    nonisolated static func isChat(_ item: HistoryItem) -> Bool { item.session?.hasPrefix("chat-") == true || item.kinds?.contains("chat") == true }

    func refresh() async {
        loading = true
        defer { loading = false }
        do {
            threads = try await HistoryAPI.page(kind: "chat", limit: 100).filter(Self.isChat)
            error = nil
        } catch { self.error = FriendlyError.describe(error).title }
    }

    func delete(_ item: HistoryItem) async {
        let before = threads
        threads.removeAll { $0.id == item.id }
        do { try await HistoryAPI.delete(item.id) } catch {
            threads = before
            self.error = "삭제하지 못했어요: \(FriendlyError.describe(error).title)"
        }
    }

    /// A thread's past messages, to continue it.
    static func open(_ item: HistoryItem) async throws -> ChatTranscript {
        let call = try await HistoryAPI.call(item.id)
        return transcript(call.turns, start: item.start)
    }

    nonisolated static func transcript(_ turns: [HistoryCall.Turn], start: Date) -> ChatTranscript {
        var msgs: [ChatMessage] = []
        for t in turns {
            let at = t.at.map { Date(timeIntervalSince1970: $0) } ?? start
            if let u = t.user { msgs.append(ChatMessage(role: .user, text: u, created: at)) }
            if let a = t.assistant { msgs.append(ChatMessage(role: .assistant, text: a, status: t.interrupted == true ? .stopped : .done, created: at)) }
        }
        return ChatTranscript(messages: msgs)
    }
}
