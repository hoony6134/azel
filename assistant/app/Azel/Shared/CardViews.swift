import SwiftUI
import Charts

/// One card under its reply (iPhone call screen, Mac home, Mac call panel). Static views: drawn once per
/// turn, nothing per frame, nothing on the audio path; only the stock card fetches another range on a tap.
struct CardView: View {
    let card: ReplyCard
    var compact = false

    var body: some View {
        Group {
            switch card {
            case .stock(let s): StockCardView(initial: s, compact: compact)
            case .portfolio(let items): PortfolioCardView(items: items)
            case .agenda(let items): AgendaCardView(items: items)
            case .todo(let items): TodoCardView(items: items)
            case .weather(let w): WeatherCardView(w: w, compact: compact)
            case .infra(let i): InfraCardView(infra: i)
            }
        }
        .padding(Theme.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Theme.stroke))
        .multilineTextAlignment(.leading)
        .accessibilityElement(children: .contain)
    }
}

/// Korean market colors: red up, blue down.
enum CardColor {
    static let up = Color(red: 0.86, green: 0.2, blue: 0.2)
    static let down = Color(red: 0.16, green: 0.42, blue: 0.86)
    static func change(_ v: Double?) -> Color { (v ?? 0) > 0 ? up : (v ?? 0) < 0 ? down : Theme.text2 }

    /// Infra thresholds: green under 70, amber under 90, red from 90 (temperature: 70 / 85 °C).
    static func load(_ v: Double, temp: Bool = false) -> Color {
        let (warn, bad) = temp ? (70.0, 85.0) : (70.0, 90.0)
        return v >= bad ? Theme.danger : v >= warn ? Theme.amber : Theme.celadon
    }
}

// MARK: stock

struct StockCardView: View {
    let initial: ReplyCard.Stock
    var compact = false
    @State private var stock: ReplyCard.Stock?
    @State private var range = "1d"
    @State private var loading = false
    @State private var failed = false

    static let ranges: [(key: String, label: String)] = [("1d", "1일"), ("7d", "7일"), ("1mo", "30일"), ("3mo", "3개월"), ("1y", "1년")]

    private var s: ReplyCard.Stock { stock ?? initial }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            HStack(alignment: .firstTextBaseline) {
                Text(s.name).font(.headline).foregroundStyle(Theme.text)
                if !s.isIndex { Text(s.symbol).font(.caption.monospaced()).foregroundStyle(Theme.text3) }
                Spacer()
                if let state = s.state { Text(Self.stateLabel(state)).font(.caption2).foregroundStyle(Theme.text3) }
            }
            HStack(alignment: .firstTextBaseline, spacing: Theme.s) {
                Text(s.price.map(ReplyCard.price) ?? "—").font(.title2.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.text)
                if let c = s.change {
                    Text("\(c > 0 ? "▲" : c < 0 ? "▼" : "") \(ReplyCard.price(abs(c)))" + (s.pct.map { " (\(ReplyCard.pct($0)))" } ?? ""))
                        .font(.subheadline.monospacedDigit()).foregroundStyle(CardColor.change(c))
                }
            }
            .accessibilityElement(children: .combine)
            if !s.points.isEmpty { chart.frame(height: compact ? 90 : 140) }
            if !compact {
                Picker("기간", selection: $range) { ForEach(Self.ranges, id: \.key) { Text($0.label).tag($0.key) } }
                    .pickerStyle(.segmented).labelsHidden()
                    .onChange(of: range) { _, r in Task { await load(r) } }
                if failed { Text("다른 기간을 불러오지 못했어요").font(.caption).foregroundStyle(Theme.danger) }
                if !s.isIndex, let h = s.high, let l = s.low {
                    Text("고가 \(ReplyCard.price(h)) · 저가 \(ReplyCard.price(l))" + (s.volume.map { " · 거래량 \(ReplyCard.price($0))" } ?? ""))
                        .font(.caption.monospacedDigit()).foregroundStyle(Theme.text3)
                }
            }
        }
    }

    private var chart: some View {
        let base = s.range == "1d" ? s.prevClose : s.points.first?.v
        let last = s.points.last?.v ?? 0
        let tint = CardColor.change(base.map { last - $0 })
        let values = s.points.map(\.v) + [base].compactMap { $0 }
        let lo = values.min() ?? 0, hi = values.max() ?? 1, pad = max((hi - lo) * 0.08, 0.01)
        return Chart {
            ForEach(Array(s.points.enumerated()), id: \.offset) { _, p in
                LineMark(x: .value("시각", Date(timeIntervalSince1970: p.t)), y: .value("가격", p.v)).foregroundStyle(tint)
                    .interpolationMethod(.monotone)
            }
            if let base {
                RuleMark(y: .value("기준", base)).foregroundStyle(Theme.text3).lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
            }
        }
        .chartYScale(domain: (lo - pad)...(hi + pad))
        .chartXAxis(.hidden)
        .chartYAxis { AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) }
        .opacity(loading ? 0.4 : 1)
        .accessibilityLabel("\(s.name) 가격 그래프")
    }

    private func load(_ r: String) async {
        if r == initial.range { stock = nil; failed = false; return }
        loading = true
        defer { loading = false }
        let q = s.symbol.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? s.symbol
        do {
            // The gateway's TV route takes the app's own token too (full scope); no new route needed.
            let obj = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/tv?detail=stock&symbol=\(q)&range=\(r)")) as? [String: Any]
            if let next = obj.flatMap({ ReplyCard.stock(JSON($0)) }), range == r { stock = next; failed = false }
        } catch { failed = true }
    }

    static func stateLabel(_ s: String) -> String {
        switch s { case "open", "regular": return "장중"; case "pre": return "장전"; case "post": return "장후"; case "closed": return "장마감"; default: return s }
    }
}

struct PortfolioCardView: View {
    let items: [ReplyCard.Holding]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            Text("내 종목").font(.headline).foregroundStyle(Theme.text)
            ForEach(Array(items.enumerated()), id: \.offset) { _, h in
                HStack(spacing: Theme.s) {
                    Text(h.name).font(.subheadline).foregroundStyle(Theme.text).lineLimit(1)
                    Spacer(minLength: Theme.s)
                    if h.spark.count > 1 {
                        Chart(Array(h.spark.enumerated()), id: \.offset) { i, v in
                            LineMark(x: .value("i", i), y: .value("v", v)).foregroundStyle(CardColor.change(h.pct))
                        }
                        .chartXAxis(.hidden).chartYAxis(.hidden)
                        .chartYScale(domain: (h.spark.min()!)...(max(h.spark.max()!, h.spark.min()! + 0.0001)))
                        .frame(width: 54, height: 20)
                    }
                    Text(h.price.map(ReplyCard.price) ?? (h.listed ? "—" : "비상장")).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.text)
                        .frame(minWidth: 64, alignment: .trailing)
                    Text(h.pct.map(ReplyCard.pct) ?? "").font(.caption.monospacedDigit()).foregroundStyle(CardColor.change(h.pct))
                        .frame(minWidth: 48, alignment: .trailing)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

// MARK: agenda and todo

struct SourceTag: View {
    let source: String?
    var body: some View {
        if let source {
            Text(["gistra": "Gistra", "google": "Google", "reminders": "미리알림"][source] ?? source)
                .font(.caption2.weight(.semibold))
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background((source == "gistra" ? Theme.indigo : Theme.celadon).opacity(0.15), in: Capsule())
                .foregroundStyle(source == "gistra" ? Theme.indigo : Theme.celadon)
        }
    }
}

struct AgendaCardView: View {
    let items: [ReplyCard.Event]

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            Text(items.isEmpty ? "일정 없음" : "일정 \(items.count)개").font(.headline).foregroundStyle(Theme.text)
            ForEach(Array(items.enumerated()), id: \.offset) { i, e in
                HStack(alignment: .top, spacing: Theme.s) {
                    VStack(spacing: 0) {
                        Circle().fill(Theme.celadon).frame(width: 7, height: 7).padding(.top, 5)
                        if i < items.count - 1 { Rectangle().fill(Theme.stroke).frame(width: 1).frame(maxHeight: .infinity) }
                    }
                    Text(e.allDay ? "종일" : [e.start, e.end].compactMap { $0.map(ReplyCard.time) }.joined(separator: "–"))
                        .font(.caption.monospacedDigit()).foregroundStyle(Theme.text2).frame(width: 78, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.title).font(.subheadline).foregroundStyle(Theme.text)
                        HStack(spacing: Theme.xs) {
                            SourceTag(source: e.source)
                            if let c = e.calendar { Text(c).font(.caption2).foregroundStyle(Theme.text3) }
                            if let l = e.location { Text(l).font(.caption2).foregroundStyle(Theme.text3).lineLimit(1) }
                        }
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

struct TodoCardView: View {
    let items: [ReplyCard.Todo]
    var now = Date()

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            Text("할 일 \(items.filter { !$0.done }.count)개").font(.headline).foregroundStyle(Theme.text)
            ForEach(Array(items.enumerated()), id: \.offset) { _, t in
                HStack(spacing: Theme.s) {
                    Image(systemName: t.done ? "checkmark.circle.fill" : "circle").foregroundStyle(t.done ? Theme.celadon : Theme.text3)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(t.title).font(.subheadline).foregroundStyle(t.done ? Theme.text3 : Theme.text).strikethrough(t.done)
                        HStack(spacing: Theme.xs) {
                            SourceTag(source: t.source)
                            if let c = t.course { Text(c).font(.caption2).foregroundStyle(Theme.text3) }
                        }
                    }
                    Spacer()
                    if let due = t.due {
                        let d = ReplyCard.dday(due, now: now)
                        Text(d).font(.caption.weight(.semibold).monospacedDigit())
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(Self.tint(d).opacity(0.15), in: Capsule()).foregroundStyle(Self.tint(d))
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    static func tint(_ dday: String) -> Color {
        dday == "D-day" || dday.hasPrefix("D+") ? Theme.danger : dday == "D-1" || dday == "D-2" ? Theme.amber : Theme.text2
    }
}

// MARK: weather

enum WeatherIcon {
    /// The TV's icon names (tv.kma_icon / wmo_icon) → SF Symbols.
    static func symbol(_ name: String?) -> String {
        switch name {
        case "clear": return "sun.max.fill"
        case "cloudy": return "cloud.sun.fill"
        case "overcast": return "cloud.fill"
        case "rain": return "cloud.rain.fill"
        case "shower": return "cloud.heavyrain.fill"
        case "sleet": return "cloud.sleet.fill"
        case "snow": return "cloud.snow.fill"
        default: return "cloud"
        }
    }
}

struct WeatherCardView: View {
    let w: ReplyCard.Weather
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            HStack(spacing: Theme.m) {
                Image(systemName: WeatherIcon.symbol(w.icon)).symbolRenderingMode(.multicolor).font(.system(size: 34))
                VStack(alignment: .leading, spacing: 2) {
                    Text(w.now.map { "\(Int($0.rounded()))°" } ?? "—").font(.title.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.text)
                    Text([w.place, w.sky].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.text2)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    if let h = w.high, let l = w.low { Text("\(Int(h.rounded()))° / \(Int(l.rounded()))°").font(.subheadline.monospacedDigit()) }
                    if let p = w.pop { Text("강수 \(Int(p))%").font(.caption).foregroundStyle(Theme.indigo) }
                }
            }
            .accessibilityElement(children: .combine)
            if !w.hourly.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Theme.m) {
                        ForEach(Array(w.hourly.enumerated()), id: \.offset) { _, h in
                            VStack(spacing: 3) {
                                Text("\(h.h)시").font(.caption2).foregroundStyle(Theme.text3)
                                Image(systemName: WeatherIcon.symbol(h.icon)).symbolRenderingMode(.multicolor)
                                Text(h.t.map { "\(Int($0.rounded()))°" } ?? "—").font(.caption.monospacedDigit())
                                Text(h.pop.map { "\(Int($0))%" } ?? "").font(.caption2).foregroundStyle(Theme.indigo)
                            }
                        }
                    }
                }
            }
            if !compact && !w.daily.isEmpty {
                ForEach(Array(w.daily.enumerated()), id: \.offset) { _, d in
                    HStack {
                        Text(d.date.map(Self.weekday) ?? "").font(.subheadline).frame(width: 44, alignment: .leading)
                        Image(systemName: WeatherIcon.symbol(d.icon)).symbolRenderingMode(.multicolor).frame(width: 28)
                        Text(d.pop.map { "\(Int($0))%" } ?? "").font(.caption).foregroundStyle(Theme.indigo).frame(width: 40)
                        Spacer()
                        Text([d.high, d.low].compactMap { $0.map { "\(Int($0.rounded()))°" } }.joined(separator: " / "))
                            .font(.subheadline.monospacedDigit())
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    static func weekday(_ d: Date) -> String {
        if Calendar.current.isDateInToday(d) { return "오늘" }
        let f = DateFormatter(); f.locale = Locale(identifier: "ko_KR"); f.dateFormat = "E"
        return f.string(from: d)
    }
}

// MARK: infra

struct InfraCardView: View {
    let infra: ReplyCard.Infra

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            Text("서버 \(infra.hosts.count)대").font(.headline).foregroundStyle(Theme.text)
            ForEach(Array(infra.hosts.enumerated()), id: \.offset) { _, h in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: Theme.xs) {
                        Circle().fill(h.up ? Theme.celadon : Theme.danger).frame(width: 8, height: 8)
                        Text(h.name).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text)
                    }
                    HStack(spacing: 4) {
                        ForEach(Self.chips(h), id: \.label) { c in Chip(text: c.label, color: c.color) }
                    }
                }
                .accessibilityElement(children: .combine)
            }
            if !infra.down.isEmpty {
                HStack(spacing: 4) { ForEach(infra.down, id: \.self) { Chip(text: "\($0) 중단", color: Theme.danger) } }
            }
            ForEach(infra.alerts, id: \.self) { Label($0, systemImage: "exclamationmark.triangle.fill").font(.caption).foregroundStyle(Theme.amber) }
        }
    }

    static func chips(_ h: ReplyCard.Infra.Host) -> [(label: String, color: Color)] {
        [("CPU", h.cpu, false), ("GPU", h.gpu, false), ("MEM", h.mem, false), ("DISK", h.disk, false), ("", h.temp, true)]
            .compactMap { k, v, temp in v.map { (temp ? "\(Int($0))°C" : "\(k) \(Int($0))%", CardColor.load($0, temp: temp)) } }
    }

    struct Chip: View {
        let text: String
        let color: Color
        var body: some View {
            Text(text).font(.caption2.weight(.semibold).monospacedDigit())
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(color.opacity(0.15), in: Capsule()).foregroundStyle(color)
        }
    }
}

#if DEBUG
enum CardSamples {
    static let all: [ReplyCard] = [stock, portfolio, agenda, todo, weather, infra].compactMap { $0 }
    static let stock = ReplyCard(json: ["kind": "stock", "symbol": "005930", "name": "삼성전자", "price": 72300, "change": 850, "pct": 1.19,
                                   "state": "open", "prev_close": 71450, "high": 72600, "low": 71200, "volume": 12_345_678,
                                   "points": (0..<60).map { [1_795_400_000 + $0 * 300, 71450 + 900 * sin(Double($0) / 9)] }])
    static let portfolio = ReplyCard(json: ["kind": "portfolio", "items": [
        ["name": "엔비디아", "symbol": "NVDA", "price": 181.2, "pct": -0.8, "spark": [183, 182, 184, 181, 181.2]],
        ["name": "에스오에스랩", "symbol": "464080", "price": 15320, "pct": 3.4, "spark": [14800, 14900, 15100, 15000, 15320]],
        ["name": "앤트로픽", "listed": false]]])
    static let agenda = ReplyCard(json: ["kind": "agenda", "items": [
        ["start": "2026-11-23T14:00:00+09:00", "end": "2026-11-23T15:15:00+09:00", "title": "컴파일러", "source": "gistra", "calendar": "수업"],
        ["start": "2026-11-23T19:00:00+09:00", "title": "저녁 약속", "source": "google", "calendar": "개인"]]])
    static let todo = ReplyCard(json: ["kind": "todo", "items": [
        ["title": "컴파일러 과제 3", "due": "2026-11-25T23:59:00+09:00", "course": "컴파일러", "source": "gistra"],
        ["title": "등록금 납부", "due": "2026-11-23", "source": "reminders"]]])
    static let weather = ReplyCard(json: ["kind": "weather", "place": "광주", "now": 7.4, "sky": "구름 많음", "icon": "cloudy", "high": 11, "low": 2, "pop": 20,
                                     "hourly": (14..<26).map { ["h": String(format: "%02d", $0 % 24), "t": 9 - ($0 - 14) / 2, "pop": $0 > 20 ? 30 : 10, "icon": $0 > 20 ? "rain" : "cloudy"] },
                                     "daily": (24..<29).map { ["date": "2026-11-\($0)", "high": 10, "low": 1, "pop": 20, "icon": "clear"] }])
    static let infra = ReplyCard(json: ["kind": "infra", "hosts": [
        ["name": "vf-spark", "up": true, "cpu_pct": 41, "gpu_pct": 92, "mem_pct": 77, "disk_pct": 63, "temp_c": 71],
        ["name": "macbook", "up": true, "cpu_pct": 12, "mem_pct": 58, "disk_pct": 81]], "down": ["searxng"], "alerts": [["summary": "GPU 메모리 높음"]]])
}

#Preview("Cards") {
    ScrollView { VStack(spacing: 12) { ForEach(Array(CardSamples.all.enumerated()), id: \.offset) { CardView(card: $0.element) } }.padding() }
        .frame(width: 420, height: 1400)
}
#endif
