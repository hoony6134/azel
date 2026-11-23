import XCTest
import SwiftUI
@testable import Azel

final class CardTests: XCTestCase {
    private func event(_ json: String) throws -> CallEvent { try JSONDecoder().decode(CallEvent.self, from: Data(json.utf8)) }

    func testEveryKindDecodesFromTheWire() throws {
        let stock = try event(#"{"type":"card","turn":7,"card":{"kind":"stock","symbol":"005930","name":"삼성전자","price":72300,"change":850,"pct":1.19,"state":"open","prev_close":71450,"high":72600,"low":71200,"volume":12345678,"is_index":false,"points":[[1795400000,71500],[1795400300,71800.5]]}}"#)
        guard case .stock(let s)? = stock.card else { return XCTFail("stock") }
        XCTAssertEqual(s.price, 72300); XCTAssertEqual(s.prevClose, 71450); XCTAssertEqual(s.points.count, 2); XCTAssertEqual(s.points[1].v, 71800.5)
        XCTAssertFalse(s.isIndex)

        let p = try event(#"{"type":"card","card":{"kind":"portfolio","items":[{"name":"엔비디아","symbol":"NVDA","price":181.2,"pct":-0.8,"spark":[1,2,3]},{"name":"앤트로픽","listed":false}]}}"#)
        guard case .portfolio(let items)? = p.card else { return XCTFail("portfolio") }
        XCTAssertEqual(items.count, 2); XCTAssertEqual(items[0].spark, [1, 2, 3]); XCTAssertFalse(items[1].listed)

        let a = try event(#"{"type":"card","card":{"kind":"agenda","items":[{"start":"2026-11-23T19:00:00+09:00","title":"저녁","source":"google","calendar":"개인"},{"start":"2026-11-23T14:00:00+09:00","end":"2026-11-23T15:15:00+09:00","title":"컴파일러","source":"gistra","calendar":"수업"},{"start":"2026-11-23","title":"종일 행사"}]}}"#)
        guard case .agenda(let ev)? = a.card else { return XCTFail("agenda") }
        XCTAssertEqual(ev.map(\.title).first, "종일 행사", "sorted by start")
        XCTAssertTrue(ev[0].allDay); XCTAssertEqual(ev[1].title, "컴파일러"); XCTAssertEqual(ev[1].source, "gistra")

        let t = try event(#"{"type":"card","card":{"kind":"todo","items":[{"title":"B","due":"2026-11-30"},{"title":"A","due":"2026-11-25T23:59:00+09:00","course":"컴파일러","source":"gistra"},{"title":"C"}]}}"#)
        guard case .todo(let todos)? = t.card else { return XCTFail("todo") }
        XCTAssertEqual(todos.map(\.title), ["A", "B", "C"], "by due date, undated last")

        let w = try event(#"{"type":"card","card":{"kind":"weather","place":"광주","now":7.4,"icon":"cloudy","high":11,"low":2,"pop":20,"hourly":[{"h":"14","t":9,"pop":10,"icon":"clear"}],"daily":[{"date":"2026-11-24","high":10,"low":1,"icon":"rain"}]}}"#)
        guard case .weather(let wx)? = w.card else { return XCTFail("weather") }
        XCTAssertEqual(wx.now, 7.4); XCTAssertEqual(wx.hourly.first?.h, "14"); XCTAssertEqual(wx.daily.count, 1)

        let i = try event(#"{"type":"card","card":{"kind":"infra","hosts":[{"name":"vf-spark","up":true,"cpu_pct":41,"gpu_pct":92,"mem_pct":77,"disk_pct":63,"temp_c":71}],"alerts":[{"summary":"GPU 높음"}],"down":["searxng"]}}"#)
        guard case .infra(let inf)? = i.card else { return XCTFail("infra") }
        XCTAssertEqual(inf.hosts.first?.gpu, 92); XCTAssertEqual(inf.down, ["searxng"]); XCTAssertEqual(inf.alerts, ["GPU 높음"])
    }

    func testUnknownKindsAndBrokenCardsAreIgnored() throws {
        XCTAssertNil(try event(#"{"type":"card","card":{"kind":"hologram","x":1}}"#).card)
        XCTAssertNil(try event(#"{"type":"card","card":{"kind":"stock"}}"#).card, "no symbol")
        XCTAssertNil(try event(#"{"type":"card","card":"oops"}"#).card)
        XCTAssertEqual(try event(#"{"type":"card","card":{"kind":"hologram"}}"#).type, "card", "the frame itself still decodes")
        var log = CallLog()
        log.apply(CallEvent(type: "reply", text: "안녕하세요"))
        log.apply(try event(#"{"type":"card","card":{"kind":"hologram"}}"#))
        XCTAssertEqual(log.lines.last?.cards, [])
    }

    /// The gateway's GET /v1/tv?detail=stock answer (other ranges) uses prev / index.
    func testStockRangeAnswerFromTheGatewayParses() {
        let s = ReplyCard.stock(JSON(["symbol": "^GSPC", "name": "S&P 500", "price": 6000.5, "prev": 5990, "index": true, "range": "1mo",
                                 "points": [[1, 5900], [2, 6000.5]]]))
        XCTAssertEqual(s?.prevClose, 5990); XCTAssertEqual(s?.isIndex, true); XCTAssertEqual(s?.range, "1mo")
    }

    /// Cards go under their turn's reply: before the reply text they wait for it; a new turn starts clean.
    func testCardsAttachToTheirTurnsReply() throws {
        let card = try XCTUnwrap(CardSamples.weather)
        var log = CallLog()
        log.apply(CallEvent(type: "transcript", text: "날씨 알려줘"))
        log.apply(CallEvent(type: "card", card: card))                  // before the reply text
        XCTAssertEqual(log.lines.count, 1, "no empty line for it")
        log.apply(CallEvent(type: "reply", text: "광주는 7도예요."))
        XCTAssertEqual(log.lines.last?.cards, [card])
        log.apply(CallEvent(type: "card", card: card))                  // after it: same reply
        XCTAssertEqual(log.lines.last?.cards.count, 2)
        log.apply(CallEvent(type: "transcript", text: "고마워"))
        log.apply(CallEvent(type: "reply", text: "네."))
        XCTAssertEqual(log.lines.last?.cards, [], "the next turn has none of them")
        XCTAssertEqual(log.lines[1].cards.count, 2, "they stay under their own reply")
    }

    /// The card window floats just above the panel (the panel never moves), flips below when there's no
    /// room, and goes on the next turn.
    @MainActor func testCardWindowSitsAboveThePanelAndFlips() throws {
        let screen = NSRect(x: 0, y: 0, width: 1440, height: 900)
        let low = NSRect(x: 490, y: 80, width: 460, height: 112)
        let above = MacCall.cardFrame(panel: low, height: 200, screen: screen)
        XCTAssertEqual(above.minY, low.maxY + MacCall.cardGap)
        XCTAssertEqual(above.minX, low.minX); XCTAssertEqual(above.width, low.width)
        let high = NSRect(x: 490, y: 760, width: 460, height: 112)
        let below = MacCall.cardFrame(panel: high, height: 200, screen: screen)
        XCTAssertEqual(below.maxY, high.minY - MacCall.cardGap, "no room above: below")
        let tall = MacCall.cardFrame(panel: NSRect(x: 0, y: 300, width: 460, height: 112), height: 700, screen: screen)
        XCTAssertLessThanOrEqual(tall.maxY, screen.maxY, "neither fits: kept on screen")
        var log = CallLog()
        log.apply(CallEvent(type: "reply", text: "삼성전자는…"))
        log.apply(CallEvent(type: "card", card: try XCTUnwrap(CardSamples.stock)))
        XCTAssertFalse(MacCall.panelCards(log).isEmpty, "shown")
        log.apply(CallEvent(type: "transcript", text: "다음"))
        XCTAssertTrue(MacCall.panelCards(log).isEmpty, "gone on the next turn")
    }

    /// The final contract's shapes (cards-contract.md): weather now{} / hours[t] / days[d], change_pct,
    /// market_state, per-host alerts, a card in a silent turn.
    func testFinalContractShapes() throws {
        let w = ReplyCard(json: ["kind": "weather", "title": "광주 날씨", "place": "광주",
                                 "now": ["temp": 7.4, "sky": "흐림", "icon": "overcast", "pop": 30],
                                 "hours": [["t": "2026-11-23T15:00:00+09:00", "temp": 8, "icon": "rain", "pop": 60]],
                                 "days": [["d": "2026-11-23", "min": 2, "max": 11, "icon": "overcast", "pop": 30]]])
        guard case .weather(let wx)? = w else { return XCTFail("weather") }
        XCTAssertEqual(wx.now, 7.4); XCTAssertEqual(wx.icon, "overcast"); XCTAssertEqual(wx.high, 11); XCTAssertEqual(wx.low, 2)
        XCTAssertEqual(wx.hourly.first?.h, "15"); XCTAssertEqual(wx.hourly.first?.t, 8); XCTAssertEqual(wx.daily.first?.high, 11)
        let s = ReplyCard(json: ["kind": "stock", "title": "삼성전자", "symbol": "005930", "price": 72300, "change_pct": 1.19, "market_state": "closed"])
        guard case .stock(let st)? = s else { return XCTFail("stock") }
        XCTAssertEqual(st.pct, 1.19); XCTAssertEqual(st.state, "closed"); XCTAssertEqual(st.name, "삼성전자")
        let p = ReplyCard(json: ["kind": "portfolio", "title": "내 종목", "items": [["symbol": "NVDA", "name": "엔비디아", "change_pct": -0.8]]])
        guard case .portfolio(let items)? = p else { return XCTFail("portfolio") }
        XCTAssertEqual(items.first?.pct, -0.8)
        let i = ReplyCard(json: ["kind": "infra", "title": "서버", "hosts": [["name": "vf-spark", "up": true, "alerts": ["GPU 높음"]]]])
        guard case .infra(let inf)? = i else { return XCTFail("infra") }
        XCTAssertEqual(inf.alerts, ["vf-spark: GPU 높음"])
        var log = CallLog()
        log.apply(CallEvent(type: "transcript", text: "서버 상태"))
        log.apply(CallEvent(type: "card", card: i))
        log.apply(CallEvent(type: "turn_done"))
        XCTAssertEqual(log.lines.last?.cards.count, 1, "a silent turn's card still shows")
    }

    func testWatchSummaries() throws {
        let now = JSON.date("2026-11-23T12:00:00+09:00")!
        XCTAssertEqual(CardSamples.stock?.summary(now: now), "삼성전자 72,300 +1.2%")
        XCTAssertEqual(CardSamples.agenda?.summary(now: now), "일정 2개 · 다음 14:00 컴파일러")
        XCTAssertEqual(CardSamples.infra?.summary(now: now), "중단: searxng")
        let busy = ReplyCard(json: ["kind": "infra", "hosts": [["name": "vf-spark", "gpu_pct": 92, "cpu_pct": 40]]])
        XCTAssertEqual(busy?.summary(now: now), "GPU 92% vf-spark")
        XCTAssertEqual(CardSamples.todo?.summary(now: now), "할 일 2개 · 등록금 납부 D-day")
    }

    func testDdayAndThresholds() {
        let now = JSON.date("2026-11-23T12:00:00+09:00")!
        XCTAssertEqual(ReplyCard.dday(JSON.date("2026-11-25")!, now: now), "D-2")
        XCTAssertEqual(ReplyCard.dday(JSON.date("2026-11-22")!, now: now), "D+1")
        XCTAssertEqual(CardColor.load(92), Theme.danger)
        XCTAssertEqual(CardColor.load(75), Theme.amber)
        XCTAssertEqual(CardColor.load(80, temp: true), Theme.amber)
    }

    /// Renders every sample card to PNG (screenshots for review); each must draw something.
    @MainActor func testCardsRender() throws {
        // The shared Azel folder (readable outside the sandbox) only when asked, else the test temp folder.
        let dir = ProcessInfo.processInfo.environment["AZEL_CARD_PNGS"] != nil || FileManager.default.fileExists(atPath: AzelFiles.folder.appendingPathComponent("card-previews.request").path)
            ? AzelFiles.folder.appendingPathComponent("card-previews") : FileManager.default.temporaryDirectory.appendingPathComponent("azel-cards")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for (i, card) in CardSamples.all.enumerated() {
            for scheme in [ColorScheme.light, .dark] {
                let view = CardView(card: card).frame(width: 400).padding(12).background(Theme.bg).environment(\.colorScheme, scheme)
                let r = ImageRenderer(content: view)
                r.scale = 2
                let image = try XCTUnwrap(r.cgImage, "card \(i)")
                XCTAssertGreaterThan(image.height, 100)
                let rep = NSBitmapImageRep(cgImage: image)
                try rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("card-\(i)-\(scheme == .dark ? "dark" : "light").png"))
            }
        }
        print("CARD_PNG_DIR \(dir.path)")
    }
}
