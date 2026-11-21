import SwiftUI

/// DESIGN §13.3: the current step as a capsule with an animated ellipsis, and a ✓ trail of the
/// last three steps (older ones collapse to "+N"). Dimmed while speaking.
struct ActivityRow: View {
    let activity: ActivityLog
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(spacing: Theme.s) {
            if let current = activity.current {
                HStack(spacing: Theme.s) {
                    Image(systemName: current.glyph).font(.system(size: 16)).foregroundStyle(Theme.indigo)
                    Text(current.label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                    Ellipsis(animated: !reduceMotion)
                }
                .padding(.horizontal, Theme.m)
                .frame(height: 32)
                .background(Theme.card.opacity(0.7), in: Capsule())
                .overlay(Capsule().strokeBorder(Theme.stroke))
                .id(current.label)
                .transition(reduceMotion ? .opacity : .opacity.combined(with: .offset(y: 6)))
            }
            if !activity.done.isEmpty {
                Text(trail).font(.footnote).foregroundStyle(activity.speaking ? Theme.text3 : Theme.text2)
                    .lineLimit(typeSize >= .accessibility1 ? 2 : 1)
            }
        }
        .animation(reduceMotion ? .easeInOut(duration: 0.22) : Theme.caption, value: activity)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(activity.current.map { "진행 중: \($0.label)" } ?? "한 일")
        .accessibilityValue(activity.done.isEmpty ? "" : "완료: " + activity.done.map(\.label).joined(separator: ", "))
    }

    private var trail: String {
        let shown = activity.done.suffix(3).map { "✓ \($0.label)" }.joined(separator: " · ")
        let hidden = activity.done.count - 3
        return hidden > 0 ? "+\(hidden) · " + shown : shown
    }
}

/// Three dots fading in sequence over 1.2 s; a static "…" with Reduce Motion.
private struct Ellipsis: View {
    let animated: Bool

    var body: some View {
        if animated {
            TimelineView(.animation(minimumInterval: 1 / 20)) { tl in
                let phase = tl.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.2) / 1.2
                HStack(spacing: 2) {
                    ForEach(0..<3) { i in
                        Circle().fill(Theme.text2).frame(width: 4, height: 4)
                            .opacity(0.25 + 0.75 * max(0, 1 - abs(phase * 3 - Double(i) - 0.5)))
                    }
                }
            }
            .accessibilityHidden(true)
        } else {
            Text("…").foregroundStyle(Theme.text2).accessibilityHidden(true)
        }
    }
}
