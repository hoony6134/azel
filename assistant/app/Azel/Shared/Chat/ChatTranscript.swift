import Foundation

/// One message in a chat thread. The assistant's text is markdown and grows while it streams.
struct ChatMessage: Identifiable, Equatable {
    enum Role: Equatable { case user, assistant }
    enum Status: Equatable { case streaming, done, stopped, failed(String) }

    let id: UUID
    var role: Role
    var text: String
    var status: Status
    /// The server's reply_id (assistant): cards and deltas attach by it.
    var replyId: Int?
    var cards: [ReplyCard] = []
    /// Tool runs of this reply (`call_id`s), tappable to their detail.
    var steps: [String] = []
    var created: Date

    init(id: UUID = UUID(), role: Role, text: String, status: Status = .done, replyId: Int? = nil, created: Date = Date()) {
        self.id = id; self.role = role; self.text = text; self.status = status; self.replyId = replyId; self.created = created
    }

    var isStreaming: Bool { status == .streaming }
}

/// Builds a chat from the call socket's events (text-only mode): the user's turns come from `send`, the
/// assistant's reply streams as `reply_delta` token pieces (or, from a server without deltas, whole `reply`
/// sentences), with cards and tool steps attached by reply_id. Pure, so the stream assembly is unit-tested.
struct ChatTranscript: Equatable {
    private(set) var messages: [ChatMessage] = []
    /// reply_ids that streamed deltas: their sentence `reply` events would repeat the text.
    private var deltaReplies: Set<Int> = []
    /// Cards that came before their reply started (they usually do).
    private var pendingCards: [(replyId: Int?, card: ReplyCard)] = []
    private var pendingSteps: [String] = []

    static func == (a: ChatTranscript, b: ChatTranscript) -> Bool { a.messages == b.messages }

    init(messages: [ChatMessage] = []) { self.messages = messages }

    /// A reply is under way (streaming or waiting for its first words).
    var busy: Bool { messages.last.map { $0.role == .user || $0.isStreaming } ?? false }

    mutating func send(_ text: String, at: Date = Date()) {
        messages.append(ChatMessage(role: .user, text: text, created: at))
    }

    /// edit-last: the last user message gets new text; everything after it goes (the server regenerates).
    mutating func editLast(_ text: String) {
        guard let i = messages.lastIndex(where: { $0.role == .user }) else { return }
        messages[i].text = text
        messages.removeSubrange((i + 1)...)
    }

    /// retry: the last assistant reply goes; a new one streams in its place.
    mutating func retry() {
        if let last = messages.last, last.role == .assistant { messages.removeLast() }
    }

    mutating func apply(_ e: CallEvent) {
        switch e.type {
        case "reply_start":
            startReply(e.reply_id)
        case "reply_delta":
            guard let t = e.text, !t.isEmpty else { return }
            let i = assistantIndex(for: e.reply_id)
            if let id = e.reply_id { deltaReplies.insert(id) }
            messages[i].text += t
        case "reply":
            guard let t = LegacyText.clean(e.text) else { return }
            let i = assistantIndex(for: e.reply_id)
            if let id = messages[i].replyId, deltaReplies.contains(id) { return }   // already streamed as deltas
            messages[i].text += (messages[i].text.isEmpty ? "" : " ") + t
        case "card":
            guard let card = e.card else { return }
            if let i = messages.lastIndex(where: { $0.role == .assistant && $0.replyId != nil && $0.replyId == e.reply_id })
                ?? (e.reply_id == nil ? openAssistant : nil) {
                messages[i].cards.append(card)
            } else {
                pendingCards.append((e.reply_id, card))
            }
        case "activity" where e.phase == "tool_start":
            guard let id = e.call_id else { return }
            if let i = openAssistant { messages[i].steps.append(id) } else { pendingSteps.append(id) }
        case "interrupt":
            if let i = openAssistant { messages[i].status = .stopped }
        case "turn_done":
            if let i = openAssistant {
                messages[i].status = .done
            } else if !pendingCards.isEmpty || !pendingSteps.isEmpty {   // a turn with cards but no words
                startReply(nil, takeAllCards: true); if let i = openAssistant { messages[i].status = .done }
            }
        case "error":
            let msg = e.message ?? "답을 받지 못했어요"
            if let i = openAssistant { messages[i].status = .failed(msg) }
            else { messages.append(ChatMessage(role: .assistant, text: "", status: .failed(msg))) }
        default:
            break
        }
    }

    /// The socket closed or failed mid-reply.
    mutating func connectionLost(_ message: String) {
        if let i = openAssistant { messages[i].status = .failed(message) }
        else if messages.last?.role == .user { messages.append(ChatMessage(role: .assistant, text: "", status: .failed(message))) }
    }

    // MARK: -

    private var openAssistant: Int? {
        guard let i = messages.indices.last, messages[i].role == .assistant, messages[i].isStreaming else { return nil }
        return i
    }

    private mutating func startReply(_ id: Int?, takeAllCards: Bool = false) {
        if let i = openAssistant, messages[i].replyId == id || messages[i].replyId == nil {
            messages[i].replyId = id ?? messages[i].replyId
        } else {
            if let i = openAssistant { messages[i].status = .done }
            messages.append(ChatMessage(role: .assistant, text: "", status: .streaming, replyId: id))
        }
        let i = messages.count - 1
        let mine = { (c: (replyId: Int?, card: ReplyCard)) in takeAllCards || c.replyId == nil || c.replyId == id }
        messages[i].cards += pendingCards.filter(mine).map(\.card)
        pendingCards.removeAll(where: mine)
        messages[i].steps += pendingSteps
        pendingSteps = []
    }

    /// The assistant message an event belongs to, starting one if text arrives without reply_start.
    private mutating func assistantIndex(for id: Int?) -> Int {
        if let id, let i = messages.lastIndex(where: { $0.role == .assistant && $0.replyId == id }) { return i }
        if let i = openAssistant { return i }
        startReply(id)
        return messages.count - 1
    }
}
