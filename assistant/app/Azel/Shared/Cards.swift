import Foundation

/// A visual card the voice server attaches to a reply (`{"type":"card","turn":…,"card":{"kind":…}}`), built
/// from tool results. Parsed leniently from JSON: every field is optional, numbers may come as ints or
/// floats, and an unknown kind is ignored (nil). Plain values only, so a card renders once per turn.
enum ReplyCard: Equatable {
    case stock(Stock)
    case portfolio([Holding])
    case agenda([Event], label: String?)
    case todo([Todo])
    case weather(Weather)
    case infra(Infra)

    struct Stock: Equatable {
        var symbol: String, name: String
        var price, change, pct, prevClose, high, low, volume: Double?
        var state: String?, currency: String?, range: String
        var isIndex: Bool
        var points: [Point]
        struct Point: Equatable { var t: Double; var v: Double }
    }
    struct Holding: Equatable { var name: String; var symbol: String?; var price, change, pct: Double?; var spark: [Double]; var listed: Bool }
    struct Event: Equatable { var start: Date?; var end: Date?; var allDay: Bool; var title: String; var source: String?; var calendar: String?; var location: String? }
    struct Todo: Equatable { var title: String; var due: Date?; var dueHasTime: Bool; var course: String?; var source: String?; var done: Bool }
    struct Weather: Equatable {
        var place: String?, sky: String?, icon: String?
        var now, high, low, pop: Double?
        var hourly: [Hour], daily: [Day]
        struct Hour: Equatable { var h: String; var t: Double?; var pop: Double?; var icon: String? }
        struct Day: Equatable { var date: Date?; var high, low, pop: Double?; var icon: String? }
    }
    struct Infra: Equatable {
        var hosts: [Host], alerts: [String], down: [String]
        struct Host: Equatable { var name: String; var up: Bool; var cpu, gpu, mem, disk, temp: Double? }
    }

    /// nil for an unknown kind or a card without its essentials.
    init?(json: [String: Any]) {
        let j = JSON(json)
        switch j.str("kind") {
        case "stock":
            guard let s = Self.stock(j) else { return nil }
            self = .stock(s)
        case "portfolio":
            self = .portfolio(j.list("items").compactMap { i in
                guard let name = i.str("name") else { return nil }
                return Holding(name: name, symbol: i.str("symbol") ?? i.str("ticker"), price: i.num("price"), change: i.num("change"),
                               pct: i.num("change_pct") ?? i.num("pct"), spark: i.nums("spark"), listed: i.bool("listed") ?? true)
            })
        case "agenda":
            self = .agenda(j.list("items").compactMap { i in
                guard let title = i.str("title") else { return nil }
                return Event(start: i.date("start"), end: i.date("end"), allDay: i.bool("all_day") ?? (i.str("start")?.count == 10),
                             title: title, source: i.str("source"), calendar: i.str("calendar"), location: i.str("location"))
            }.sorted { ($0.start ?? .distantFuture) < ($1.start ?? .distantFuture) }, label: j.str("range_label"))
        case "todo":
            self = .todo(j.list("items").compactMap { i in
                guard let title = i.str("title") else { return nil }
                return Todo(title: title, due: i.date("due"), dueHasTime: (i.str("due")?.count ?? 0) > 10, course: i.str("course"),
                            source: i.str("source"), done: i.bool("done") ?? false)
            }.sorted { ($0.due ?? .distantFuture) < ($1.due ?? .distantFuture) })
        case "weather":
            // The contract: now {temp, sky, icon, pop}, hours [{t ISO, temp, icon, pop}], days [{d, min, max, icon, pop}].
            // The TV's flat shape (now: number, hourly/daily) is read too.
            let now = (j.raw["now"] as? [String: Any]).map(JSON.init)
            let days = j.list("days").isEmpty ? j.list("daily") : j.list("days")
            let today = days.first
            self = .weather(Weather(place: j.str("place"), sky: now?.str("sky") ?? j.str("sky"), icon: now?.str("icon") ?? j.str("icon"),
                                    now: now?.num("temp") ?? j.num("now"), high: j.num("high") ?? today?.num("max"),
                                    low: j.num("low") ?? today?.num("min"), pop: now?.num("pop") ?? j.num("pop"),
                                    hourly: (j.list("hours").isEmpty ? j.list("hourly") : j.list("hours")).prefix(12).map { h in
                                        Weather.Hour(h: h.str("h") ?? h.date("t").map(Self.hour) ?? "", t: h.num("temp") ?? h.num("t"),
                                                     pop: h.num("pop"), icon: h.str("icon"))
                                    },
                                    daily: days.prefix(5).map { d in
                                        Weather.Day(date: d.date("d") ?? d.date("date"), high: d.num("max") ?? d.num("high"),
                                                    low: d.num("min") ?? d.num("low"), pop: d.num("pop"), icon: d.str("icon"))
                                    }))
        case "infra":
            self = .infra(Infra(hosts: j.list("hosts").compactMap { h in
                guard let name = h.str("name") else { return nil }
                return Infra.Host(name: name, up: h.bool("up") ?? true, cpu: h.num("cpu_pct") ?? h.num("cpu"), gpu: h.num("gpu_pct") ?? h.num("gpu"),
                                  mem: h.num("mem_pct") ?? h.num("mem"), disk: h.num("disk_pct") ?? h.num("disk"), temp: h.num("temp_c") ?? h.num("temp"))
            }, alerts: j.list("hosts").flatMap { h in h.strs("alerts").map { "\(h.str("name") ?? ""): \($0)" } }
                + j.list("alerts").compactMap { $0.str("summary") ?? $0.str("name") } + j.strs("alerts"),
               down: j.strs("down") + j.list("down").compactMap { $0.str("name") }))
        default:
            return nil   // unknown kinds are ignored
        }
    }

    /// The stock card, or the gateway's `GET /v1/tv?detail=stock` answer (prev / index) for another range.
    static func stock(_ j: JSON) -> Stock? {
        guard let symbol = j.str("symbol") else { return nil }
        let points = (j.raw["points"] as? [[Any]] ?? []).compactMap { p -> Stock.Point? in
            guard p.count >= 2, let t = JSON.number(p[0]), let v = JSON.number(p[1]) else { return nil }
            return Stock.Point(t: t, v: v)
        }
        return Stock(symbol: symbol, name: j.str("name") ?? j.str("title") ?? symbol, price: j.num("price"), change: j.num("change"),
                     pct: j.num("change_pct") ?? j.num("pct"),
                     prevClose: j.num("prev_close") ?? j.num("prev"), high: j.num("high"), low: j.num("low"), volume: j.num("volume"),
                     state: j.str("market_state") ?? j.str("state"), currency: j.str("currency"), range: j.str("range") ?? "1d",
                     isIndex: j.bool("is_index") ?? j.bool("index") ?? symbol.hasPrefix("^"), points: points)
    }

    /// The reply line for cards that came in a turn without a spoken reply.
    static func line(_ cards: [ReplyCard]) -> CallLine { CallLine(mine: false, text: "", cards: cards) }

    /// One line for the Watch (no charts): "삼성전자 72,300 +1.2%", "오늘 일정 3개 · 다음 14:00 수업", "GPU 92% vf-spark".
    func summary(now: Date = Date()) -> String {
        switch self {
        case .stock(let s):
            return [s.name, s.price.map(Self.price), s.pct.map(Self.pct)].compactMap { $0 }.joined(separator: " ")
        case .portfolio(let items):
            let up = items.filter { ($0.pct ?? 0) > 0 }.count
            return "내 종목 \(items.count)개 · 오른 종목 \(up)개"
        case .agenda(let items, _):
            let next = items.first { ($0.start ?? .distantPast) >= now && !$0.allDay }
            let head = "일정 \(items.count)개"
            guard let next, let start = next.start else { return head }
            return "\(head) · 다음 \(Self.time(start)) \(next.title)"
        case .todo(let items):
            let open = items.filter { !$0.done }
            guard let first = open.first else { return "할 일 없음" }
            return "할 일 \(open.count)개 · \(first.title)" + (first.due.map { " \(Self.dday($0, now: now))" } ?? "")
        case .weather(let w):
            return [w.place, w.now.map { "\(Int($0.rounded()))°" }, w.sky, w.pop.map { "강수 \(Int($0))%" }].compactMap { $0 }.joined(separator: " ")
        case .infra(let i):
            if let d = i.down.first { return "중단: \(d)" + (i.down.count > 1 ? " 외 \(i.down.count - 1)" : "") }
            let worst = i.hosts.flatMap { h in [("GPU", h.gpu), ("CPU", h.cpu), ("MEM", h.mem), ("DISK", h.disk)].compactMap { k, v in v.map { (k, $0, h.name) } } }
                .max { $0.1 < $1.1 }
            guard let worst else { return "서버 \(i.hosts.count)대" }
            return "\(worst.0) \(Int(worst.1))% \(worst.2)"
        }
    }

    // MARK: formatting shared by the views

    static func price(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = v >= 1000 ? 0 : 2
        return f.string(from: v as NSNumber) ?? "\(v)"
    }
    static func pct(_ v: Double) -> String { String(format: "%@%.1f%%", v > 0 ? "+" : "", v) }
    static func hour(_ d: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "ko_KR"); f.dateFormat = "HH"
        return f.string(from: d)
    }
    static func time(_ d: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "ko_KR"); f.dateFormat = "HH:mm"
        return f.string(from: d)
    }
    /// "D-day", "D-2", "D+1" by calendar day in the device's time zone.
    static func dday(_ due: Date, now: Date) -> String {
        let cal = Calendar.current
        let n = cal.dateComponents([.day], from: cal.startOfDay(for: now), to: cal.startOfDay(for: due)).day ?? 0
        return n == 0 ? "D-day" : n > 0 ? "D-\(n)" : "D+\(-n)"
    }
}

/// A lenient view of a JSON object.
struct JSON {
    let raw: [String: Any]
    init(_ raw: [String: Any]) { self.raw = raw }

    func str(_ k: String) -> String? { (raw[k] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    func num(_ k: String) -> Double? { raw[k].flatMap(Self.number) }
    func bool(_ k: String) -> Bool? { raw[k] as? Bool }
    func nums(_ k: String) -> [Double] { (raw[k] as? [Any] ?? []).compactMap(Self.number) }
    func strs(_ k: String) -> [String] { (raw[k] as? [Any] ?? []).compactMap { $0 as? String } }
    func list(_ k: String) -> [JSON] { (raw[k] as? [Any] ?? []).compactMap { ($0 as? [String: Any]).map(JSON.init) } }
    /// ISO 8601 with or without seconds/offset, or a plain date (2026-11-25, local midnight).
    func date(_ k: String) -> Date? { str(k).flatMap(Self.date) }

    static func number(_ v: Any) -> Double? {
        if v is Bool { return nil }
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    static func date(_ s: String) -> Date? {
        if s.count == 10 {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"
            return f.date(from: s)
        }
        let iso = ISO8601DateFormatter()
        if let d = iso.date(from: s) { return d }
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = iso.date(from: s) { return d }
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        for fmt in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm"] {
            f.dateFormat = fmt
            if let d = f.date(from: s) { return d }
        }
        return nil
    }
}
