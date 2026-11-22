import Foundation

/// Which part of the reply has actually been heard (user request, 2026-11-21). Mirrors CallLog's
/// paragraph rules: reply sentences of one turn form a paragraph; each sentence's audio chunks
/// follow its `reply` event. Positions are in samples scheduled since the last flush, the same
/// clock as `AudioIO.playedSamples`.
struct KaraokeTimeline: Equatable {
    struct Segment: Equatable {
        var text: String
        var start: Int
        var samples = 0
        /// Sample offsets (from `start`) where each audio chunk ends, for stepped mode.
        var chunkEnds: [Int] = []
    }

    private(set) var segments: [Segment] = []
    private(set) var scheduled = 0
    private var open = false
    /// After an interrupt the playback clock resets; the heard part is frozen here.
    private var frozen: Double?

    var texts: [String] { segments.map(\.text) }

    /// Same paragraph rules as `CallLog.apply`.
    mutating func apply(type: String, text: String?) {
        switch type {
        case "reply":
            guard let t = LegacyText.clean(text) else { return }
            if !open { segments = []; frozen = nil; open = true }
            segments.append(Segment(text: t, start: scheduled))
        case "transcript", "turn_done":
            open = false
        default: break
        }
    }

    /// One audio chunk was scheduled; it belongs to the latest sentence.
    mutating func audio(samples: Int) {
        scheduled += samples
        guard !segments.isEmpty else { return }
        segments[segments.count - 1].samples += samples
        segments[segments.count - 1].chunkEnds.append(segments[segments.count - 1].samples)
    }

    /// Interrupt or flush: playback drops its queue and its clock restarts at zero.
    mutating func flush(heard played: Int) {
        frozen = spoken(played: played, stepped: false)
        scheduled = 0
        open = false
    }

    /// The player dropped the previous reply's leftover audio and its clock restarts at zero: keep
    /// the sentences still waiting for audio (the new reply), drop the ones already voiced.
    mutating func restartClock() {
        segments = segments.filter { $0.samples == 0 }.map { var s = $0; s.start = 0; return s }
        scheduled = 0
        frozen = nil
    }

    /// Characters heard so far, counted over the sentences joined with single spaces (as CallLog
    /// joins them). `stepped`: only whole chunks count (Reduce Motion).
    func spoken(played: Int, stepped: Bool) -> Double {
        if let frozen { return frozen }
        var chars = 0.0
        for (i, s) in segments.enumerated() {
            let sep = i == 0 ? 0.0 : 1.0
            let len = Double(s.text.count)
            guard s.samples > 0, played > s.start else { break }
            let into = played - s.start
            if into >= s.samples { chars += sep + len; continue }
            let heard = stepped ? (s.chunkEnds.last { $0 <= into } ?? 0) : into
            chars += sep + len * Double(heard) / Double(s.samples)
            break
        }
        return chars
    }

    /// The sentence being spoken (for auto-scroll).
    func currentSegment(played: Int) -> Int? {
        guard frozen == nil else { return nil }
        return segments.indices.first { played < segments[$0].start + segments[$0].samples }
    }
}

/// The overlay stays up until the last chunk has played, then lingers 1.5 s; the user can still
/// close it or start a new turn earlier.
enum CallOverlay {
    static let linger = 1.5
    /// Hands-free calls (wake, push-to-talk) end when the user says nothing this long after the
    /// last reply finished playing (PM, 2026-11-22), then the panel lingers `linger` and closes.
    static let quiet = 8.0

    static func shouldClose(turnDone: Bool, playing: Bool, drainedFor: TimeInterval?, after: TimeInterval = linger) -> Bool {
        guard turnDone, !playing, let drainedFor else { return false }
        return drainedFor >= after
    }
}

/// Bright for what has been heard, dimmed for the rest.
enum KaraokeStyle {
    static let dimmed = 0.38

    /// Splits `text` at `spoken` characters (rounded down).
    static func split(_ text: String, spoken: Double) -> (heard: String, rest: String) {
        let n = max(0, min(text.count, Int(spoken)))
        let i = text.index(text.startIndex, offsetBy: n)
        return (String(text[..<i]), String(text[i...]))
    }
}

#if canImport(SwiftUI) && !os(watchOS) // the watch shows plain captions
import SwiftUI

/// One reply sentence with the heard part bright and the rest dimmed.
struct KaraokeText: View {
    let text: String
    /// Characters of this sentence heard so far.
    let heard: Double
    var color: Color = Theme.text

    var body: some View {
        let parts = KaraokeStyle.split(text, spoken: heard)
        var a = AttributedString(parts.heard)
        a.foregroundColor = color
        var b = AttributedString(parts.rest)
        b.foregroundColor = color.opacity(KaraokeStyle.dimmed)
        return Text(a + b)
    }

    /// Characters of sentence `index` heard, given the paragraph-wide count (sentences joined by one space).
    static func heard(in texts: [String], index: Int, spoken: Double) -> Double {
        let before = texts.prefix(index).reduce(0) { $0 + $1.count + 1 }
        return max(0, min(Double(texts[index].count), spoken - Double(before)))
    }
}

extension KaraokeText {
    /// Scroll anchor that keeps the sentence being heard in view: the point that far through the paragraph lines up
    /// with the same point of the viewport (a long reply no longer jumps to its unheard end).
    static func follow(_ texts: [String], current: Int) -> UnitPoint {
        let total = texts.reduce(0) { $0 + $1.count + 1 }
        let before = texts.prefix(current).reduce(0) { $0 + $1.count + 1 }
        return UnitPoint(x: 0.5, y: total > 0 ? Double(before) / Double(total) : 0)
    }
}
#endif
