import XCTest
import SwiftUI
@testable import Azel

/// 2026-11-23: the Mac panel showed "Image(provider: …) 오늘 애플 주식 차트 보…" for a typed line. The glyph
/// belongs to the display only; what is sent and logged is the typed text itself.
final class TypedCaptionTests: XCTestCase {
    func testTypedTextIsSentAndLoggedUndecorated() throws {
        let typed = "오늘 애플 주식 차트 보여줘"
        let frame = try XCTUnwrap(CallClient.textFrame(typed, speak: true))
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(frame.utf8)) as? [String: Any])
        XCTAssertEqual(obj["text"] as? String, typed)
        XCTAssertFalse(frame.contains("Image"))
        var log = CallLog()
        log.typed(typed)
        XCTAssertEqual(log.lines.last?.text, typed)
        XCTAssertEqual(log.lines.last?.typed, true)
    }

    func testOnlyTypedLinesGetTheGlyph() {
        XCTAssertEqual(TypedCaption.glyph(CallLine(mine: true, text: "a", typed: true)), "keyboard")
        XCTAssertNil(TypedCaption.glyph(CallLine(mine: true, text: "a")))
    }

    /// The caption draws the glyph as an image, not as text: rendered, it is wider than the text alone and
    /// the same height (a debug description would wrap to several lines).
    @MainActor func testCaptionRendersTheGlyphAsAnImage() throws {
        let line = CallLine(mine: true, text: "오늘 애플 주식", typed: true)
        let plain = try XCTUnwrap(ImageRenderer(content: Text(line.text).font(.footnote).fixedSize()).cgImage)
        let typed = try XCTUnwrap(ImageRenderer(content: TypedCaption.text(line).font(.footnote).fixedSize()).cgImage)
        XCTAssertGreaterThan(typed.width, plain.width)
        XCTAssertLessThan(typed.width, plain.width * 2, "a glyph, not \"Image(provider: …)\"")
    }
}
