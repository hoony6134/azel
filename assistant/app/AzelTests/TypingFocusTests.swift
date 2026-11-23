import XCTest
import AppKit
@testable import Azel

/// 2026-11-23: on the Mac panel only the first typed character went in; the field never became first
/// responder. The first key is now replayed into the focused field's editor (through the input method).
@MainActor
final class TypingFocusTests: XCTestCase {
    private func key(_ s: String) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
                         characters: s, charactersIgnoringModifiers: s, isARepeat: false, keyCode: 0)!
    }

    func testKeysGoIntoTheFocusedFieldOneAfterAnother() throws {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 60), styleMask: [.titled], backing: .buffered, defer: false)
        let field = NSTextField(frame: NSRect(x: 10, y: 10, width: 280, height: 24))
        window.contentView?.addSubview(field)
        XCTAssertFalse(KeyReplay.deliver(key("a"), in: window), "no field focused: nothing swallowed")
        XCTAssertTrue(window.makeFirstResponder(field))
        for c in ["a", "b", "c"] { XCTAssertTrue(KeyReplay.deliver(key(c), in: window)) }
        XCTAssertEqual((window.firstResponder as? NSTextView)?.string, "abc", "every key, not just the first")
    }

    func testTheCardWindowNeverTakesKey() {
        let w = CardWindow(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        XCTAssertFalse(w.canBecomeKey)
        XCTAssertFalse(w.canBecomeMain)
    }

    func testWhichKeysOpenTheField() {
        XCTAssertTrue(MacCall.opensTyping("a"))
        XCTAssertTrue(MacCall.opensTyping("ㅇ"))
        XCTAssertFalse(MacCall.opensTyping(" "), "space stays mute")
        XCTAssertFalse(MacCall.opensTyping("\u{1b}"), "Esc stays hang-up")
        XCTAssertFalse(MacCall.opensTyping(""))
    }
}
