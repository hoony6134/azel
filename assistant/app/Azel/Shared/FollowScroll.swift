import SwiftUI

/// Keeps a call transcript on what is current: the sentence being heard while a reply plays, otherwise
/// the bottom. It follows new rows, text that grows in place and any height change, not only new ids
/// (user, 2026-11-23: 자동스크롤도 안맞는다). Scrolling away by hand stops it; scrolling back to the
/// bottom resumes it. Apply to the ScrollView, inside its ScrollViewReader; put `FollowScroll.bottomID`
/// on a last, empty row.
struct FollowScroll: ViewModifier {
    static let bottomID = "follow-bottom"

    let proxy: ScrollViewProxy
    @Binding var following: Bool
    /// Where "current" is right now.
    let target: () -> (id: AnyHashable, anchor: UnitPoint)
    /// Changes that should re-follow besides height (e.g. the sentence being heard).
    let trigger: AnyHashable
    var animated = true

    @State private var atBottom = true
    @State private var byHand = false

    func body(content: Content) -> some View {
        Group {
            if #available(iOS 18, macOS 15, watchOS 11, *) {
                content
                    .onScrollGeometryChange(for: Double.self, of: { $0.contentSize.height }) { _, _ in follow() }
                    .onScrollGeometryChange(for: Bool.self, of: {
                        Self.isAtBottom(offset: $0.contentOffset.y, visible: $0.containerSize.height, content: $0.contentSize.height)
                    }) { _, bottom in atBottom = bottom }
                    .onScrollPhaseChange { _, phase in
                        switch phase {
                        case .interacting, .tracking: byHand = true; following = false
                        case .idle where byHand: byHand = false; following = atBottom
                        default: break
                        }
                    }
            } else {
                content.defaultScrollAnchor(.bottom)
            }
        }
        .onChange(of: trigger) { follow() }
    }

    private func follow() {
        guard following else { return }
        let t = target()
        if animated { withAnimation(Theme.caption) { proxy.scrollTo(t.id, anchor: t.anchor) } } else { proxy.scrollTo(t.id, anchor: t.anchor) }
    }

    /// Within 24 pt of the end counts as the bottom (rounding, a half-visible last line).
    static func isAtBottom(offset: Double, visible: Double, content: Double) -> Bool {
        offset + visible >= content - 24
    }
}

extension View {
    func followScroll(_ proxy: ScrollViewProxy, following: Binding<Bool>, trigger: AnyHashable, animated: Bool = true,
                      target: @escaping () -> (id: AnyHashable, anchor: UnitPoint)) -> some View {
        modifier(FollowScroll(proxy: proxy, following: following, target: target, trigger: trigger, animated: animated))
    }
}
