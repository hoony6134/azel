import Foundation

/// One JSON text message from `WS /v1/call`.
struct CallEvent: Decodable, Equatable {
    let type: String          // state | transcript | reply | interrupt | turn_done | error | activity
    var value: String?        // state
    var text: String?         // transcript, reply
    var route: String?        // reply
    var message: String?      // error
    var phase: String?        // activity: routing | tool_start | tool_end | thinking (others ignored)
    var label: String?        // activity, server wording
    var tool: String?         // activity
    var kind: String?         // activity: calendar | mail | memory | … (DESIGN §13.2)
    var reply_id: Int?        // reply_start
    var mode: String?         // the active brain mode (state / mode events)
    var context: Context?     // `context`: the first frame of a call placed for a task or a reminder
    var caps: [String]?       // what this call's server supports (`hello` frame): text, mode, played…
    var reason: String?       // text_rejected: too_long | too_fast
    var secret: Bool?         // state frames: this call is secret (incognito)
    var auto: Bool?           // state frames: an auto-approve grant is live for this call
    var expires_at: Double?   // auto_approve: when the grant ends (epoch)
    /// A boolean `value` (`{"type":"secret","value":true}`); `value` then stays nil.
    var flag: Bool?
    /// `card`: a visual card for the reply (nil for an unknown kind).
    var card: ReplyCard?
    /// Tool runs (`activity` tool_start / tool_output / tool_end): which call, where, what went in and out.
    var call_id: String?
    var host: String?
    var input: String?        // a command line, or the tool's JSON arguments pretty-printed
    var output: String?       // tool_output: the chunk; tool_end: the last ~8 KB
    var exit_code: Int?
    var ok: Bool?
    var duration_ms: Double?
    var truncated: Bool?
    var task_id: String?

    struct Context: Decodable, Equatable { var kind: String?; var label: String?; var id: String? }

    private enum CodingKeys: String, CodingKey {
        case type, value, text, route, message, phase, label, tool, kind, reply_id, mode, context, caps, reason, secret, auto, expires_at, card
        case call_id, host, input, output, chunk, exit_code, ok, duration_ms, truncated, task_id
    }

    init(type: String, value: String? = nil, text: String? = nil, route: String? = nil, card: ReplyCard? = nil, replyId: Int? = nil) {
        self.type = type; self.value = value; self.text = text; self.route = route; self.card = card; self.reply_id = replyId
    }

    /// Field by field and tolerant: one odd field never drops the whole frame.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        if let s = try? c.decodeIfPresent(String.self, forKey: .value) { value = s } else { flag = try? c.decodeIfPresent(Bool.self, forKey: .value) }
        func str(_ k: CodingKeys) -> String? { try? c.decodeIfPresent(String.self, forKey: k) }
        text = str(.text); route = str(.route); message = str(.message); phase = str(.phase); label = str(.label)
        tool = str(.tool); kind = str(.kind); mode = str(.mode); reason = str(.reason)
        reply_id = try? c.decodeIfPresent(Int.self, forKey: .reply_id)
        context = try? c.decodeIfPresent(Context.self, forKey: .context)
        caps = try? c.decodeIfPresent([String].self, forKey: .caps)
        secret = try? c.decodeIfPresent(Bool.self, forKey: .secret)
        auto = try? c.decodeIfPresent(Bool.self, forKey: .auto)
        expires_at = try? c.decodeIfPresent(Double.self, forKey: .expires_at)
        if type == "activity" {
            call_id = str(.call_id) ?? (try? c.decodeIfPresent(Int.self, forKey: .call_id)).flatMap { $0.map(String.init) }
            host = str(.host); task_id = str(.task_id)
            output = str(.output) ?? str(.chunk)
            exit_code = try? c.decodeIfPresent(Int.self, forKey: .exit_code)
            ok = try? c.decodeIfPresent(Bool.self, forKey: .ok)
            duration_ms = try? c.decodeIfPresent(Double.self, forKey: .duration_ms)
            truncated = try? c.decodeIfPresent(Bool.self, forKey: .truncated)
            if let v = (try? c.decodeIfPresent(AnyJSON.self, forKey: .input))?.value {
                input = (v as? String) ?? ((try? JSONSerialization.data(withJSONObject: v, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]))
                    .flatMap { String(data: $0, encoding: .utf8) })
            }
        }
        if type == "card", let obj = (try? c.decodeIfPresent(AnyJSON.self, forKey: .card))?.value as? [String: Any] { card = ReplyCard(json: obj) }
    }
}

/// Any JSON value as Foundation types, for the free-form card payload.
struct AnyJSON: Decodable {
    let value: Any
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { value = NSNull() }
        else if let b = try? c.decode(Bool.self) { value = b }
        else if let d = try? c.decode(Double.self) { value = d }
        else if let s = try? c.decode(String.self) { value = s }
        else if let a = try? c.decode([AnyJSON].self) { value = a.map(\.value) }
        else { value = try c.decode([String: AnyJSON].self).mapValues(\.value) }
    }
}
