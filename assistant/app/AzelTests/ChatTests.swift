import XCTest
import SwiftUI
@testable import Azel

final class ChatTranscriptTests: XCTestCase {
    private func ev(_ json: String) throws -> CallEvent { try JSONDecoder().decode(CallEvent.self, from: Data(json.utf8)) }

    func testDeltasAssembleOneStreamingReply() throws {
        var t = ChatTranscript()
        t.send("오늘 일정 알려줘")
        XCTAssertTrue(t.busy, "waiting for the first words")
        t.apply(try ev(#"{"type":"reply_start","reply_id":3}"#))
        for piece in ["오늘은 ", "**두 가지** ", "일정이 있어요.\n\n- 컴파일러"] {
            let json = String(data: try JSONSerialization.data(withJSONObject: ["type": "reply_delta", "reply_id": 3, "text": piece]), encoding: .utf8)!
            t.apply(try ev(json))
        }
        t.apply(try ev(#"{"type":"reply","reply_id":3,"text":"오늘은 두 가지 일정이 있어요."}"#))   // the sentence copy: ignored
        XCTAssertEqual(t.messages.count, 2)
        XCTAssertEqual(t.messages[1].text, "오늘은 **두 가지** 일정이 있어요.\n\n- 컴파일러")
        XCTAssertTrue(t.messages[1].isStreaming)
        t.apply(try ev(#"{"type":"turn_done"}"#))
        XCTAssertEqual(t.messages[1].status, .done)
        XCTAssertFalse(t.busy)
    }

    func testSentencesStreamWhenTheServerHasNoDeltas() throws {
        var t = ChatTranscript()
        t.send("날씨")
        t.apply(try ev(#"{"type":"reply_start","reply_id":1}"#))
        t.apply(try ev(#"{"type":"reply","text":"광주는 맑아요."}"#))
        t.apply(try ev(#"{"type":"reply","text":"최고 11도예요."}"#))
        XCTAssertEqual(t.messages.last?.text, "광주는 맑아요. 최고 11도예요.")
    }

    func testCardsAndStepsAttachToTheirReplyEvenWhenEarly() throws {
        var t = ChatTranscript()
        t.send("삼성전자 주가")
        let card = try XCTUnwrap(CardSamples.stock)
        t.apply(CallEvent(type: "card", card: card, replyId: 9))                             // before reply_start
        t.apply(try ev(#"{"type":"activity","phase":"tool_start","call_id":"c1","tool":"stock_quote"}"#))
        t.apply(try ev(#"{"type":"reply_start","reply_id":9}"#))
        t.apply(try ev(#"{"type":"reply_delta","reply_id":9,"text":"72,300원이에요."}"#))
        XCTAssertEqual(t.messages.last?.cards, [card])
        XCTAssertEqual(t.messages.last?.steps, ["c1"])
    }

    func testASilentTurnWithOnlyACardStillShowsIt() throws {
        var t = ChatTranscript()
        t.send("서버 상태")
        t.apply(CallEvent(type: "card", card: try XCTUnwrap(CardSamples.infra), replyId: 2))
        t.apply(try ev(#"{"type":"turn_done"}"#))
        XCTAssertEqual(t.messages.last?.role, .assistant)
        XCTAssertEqual(t.messages.last?.cards.count, 1)
        XCTAssertEqual(t.messages.last?.status, .done)
    }

    func testStopErrorAndALostConnection() throws {
        var t = ChatTranscript()
        t.send("길게 설명해줘")
        t.apply(try ev(#"{"type":"reply_delta","reply_id":4,"text":"첫 문단"}"#))
        t.apply(CallEvent(type: "interrupt"))
        XCTAssertEqual(t.messages.last?.status, .stopped)
        XCTAssertEqual(t.messages.last?.text, "첫 문단", "what came stays")
        t.send("다시")
        t.apply(try ev(#"{"type":"error","message":"brain down"}"#))
        XCTAssertEqual(t.messages.last?.status, .failed("brain down"))
        t.send("또")
        t.connectionLost("끊겼어요")
        XCTAssertEqual(t.messages.last?.status, .failed("끊겼어요"))
        XCTAssertFalse(t.busy)
    }

    func testRetryAndEditLast() throws {
        var t = ChatTranscript()
        t.send("A")
        t.apply(try ev(#"{"type":"reply_delta","reply_id":1,"text":"answer A"}"#))
        t.apply(try ev(#"{"type":"turn_done"}"#))
        t.retry()
        XCTAssertEqual(t.messages.map(\.text), ["A"], "the last reply goes")
        t.apply(try ev(#"{"type":"reply_delta","reply_id":2,"text":"again"}"#))
        t.apply(try ev(#"{"type":"turn_done"}"#))
        t.editLast("B")
        XCTAssertEqual(t.messages.map(\.text), ["B"], "the edited question, its old answer gone")
    }

    func testDeltaEventDecodes() throws {
        let e = try ev(#"{"type":"reply_delta","reply_id":7,"text":"안녕"}"#)
        XCTAssertEqual(e.reply_id, 7); XCTAssertEqual(e.text, "안녕")
    }
}

final class ChatPlumbingTests: XCTestCase {
    func testChatUsesTheCallSocketInTextOnlyMode() throws {
        let req = try ChatSession.request(voiceURL: "wss://azel.example/voice", token: "t", session: "chat-abc", persona: "azel", mode: "local")
        let items = try XCTUnwrap(URLComponents(url: try XCTUnwrap(req.url), resolvingAgainstBaseURL: false)?.queryItems)
        let q = Dictionary(items.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { a, _ in a })
        XCTAssertEqual(req.url?.path, "/voice/v1/call", "the existing route")
        XCTAssertEqual(q["intent"], "chat"); XCTAssertEqual(q["audio"], "0"); XCTAssertEqual(q["session"], "chat-abc")
        XCTAssertNil(q["duck"], "call-only")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer t")
    }

    func testThreadsAreChatSessionsAndReopenFromHistory() {
        XCTAssertTrue(ChatThreads.isChat(HistoryItem(id: "1", session: "chat-x", started_at: 0)))
        XCTAssertFalse(ChatThreads.isChat(HistoryItem(id: "2", session: "mac-y", started_at: 0)))
        let t = ChatThreads.transcript([.init(at: 10, user: "질문", assistant: "답"), .init(at: 20, user: "끊긴 질문", assistant: "반", interrupted: true)],
                                       start: Date(timeIntervalSince1970: 0))
        XCTAssertEqual(t.messages.map(\.text), ["질문", "답", "끊긴 질문", "반"])
        XCTAssertEqual(t.messages.last?.status, .stopped)
        XCTAssertFalse(t.busy)
    }

    func testMarkdownIsReadAloudAsPlainText() {
        XCTAssertEqual(ChatSession.plain("**굵게** [링크](https://a.b) `코드`"), "굵게 링크 코드")
        XCTAssertTrue(ChatSession.plain("앞\n```swift\nlet x = 1\n```\n뒤").contains("코드 생략"))
    }

    func testTheParserToleratesAHalfStreamedReply() {
        let blocks = ChatMarkdown.parse("목록:\n- 하나\n- 둘\n\n```swift\nlet x")
        XCTAssertEqual(blocks.count, 3)
        if case .code(let lang, let text) = blocks.last { XCTAssertEqual(lang, "swift"); XCTAssertEqual(text, "let x") } else { XCTFail("open fence is code") }
    }
}

/// Renders a sample conversation's rows (light and dark) for review; writes PNGs only when asked.
@MainActor
final class ChatRenderTests: XCTestCase {
    func testRowsRender() throws {
        let session = ChatSession(session: "chat-render")
        var t = ChatTranscript()
        t.send("내일 일정이랑 할 일 알려줘")
        t.apply(CallEvent(type: "card", card: CardSamples.agenda, replyId: 1))
        t.apply(CallEvent(type: "reply_start", replyId: 1))
        let md = "내일은 **두 가지** 일정이 있어요.\n\n1. 10:00 컴파일러 수업\n2. 19:00 저녁 약속\n\n| 과목 | 마감 |\n|---|---|\n| 컴파일러 | D-2 |\n\n```swift\nlet ready = true\n```"
        t.apply(CallEvent(type: "reply_delta", text: md, replyId: 1))
        t.apply(CallEvent(type: "turn_done"))
        session.restore(t)
        let folder = AzelFiles.folder.appendingPathComponent("card-previews")
        let write = FileManager.default.fileExists(atPath: AzelFiles.folder.appendingPathComponent("card-previews.request").path)
        if write { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        for scheme in [ColorScheme.light, .dark] {
            let view = VStack(alignment: .leading, spacing: 16) {
                ForEach(session.transcript.messages) { m in
                    ChatMessageRow(message: m, session: session, isLast: m.id == session.transcript.messages.last?.id,
                                   isLastUser: m.role == .user)
                }
            }
            .padding(16).frame(width: 560).background(Theme.bg).environment(\.colorScheme, scheme)
            let r = ImageRenderer(content: view); r.scale = 2
            let image = try XCTUnwrap(r.cgImage)
            XCTAssertGreaterThan(image.height, 300)
            if write {
                try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])?
                    .write(to: folder.appendingPathComponent("chat-\(scheme == .dark ? "dark" : "light").png"))
            }
        }
    }
}
