import ActivityKit
import SwiftUI
import WidgetKit

@main
struct AzelWidgets: WidgetBundle {
    var body: some Widget {
        CallLiveActivity()
        TaskLiveActivity()
    }
}

private extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

/// The moon jar, flattened: a glazed disc with a soft highlight (widgets don't animate a canvas).
private struct Orb: View {
    let glaze: UInt32
    var body: some View {
        Circle()
            .fill(RadialGradient(colors: [.white.opacity(0.9), Color(hex: glaze), Color(hex: glaze).opacity(0.75)],
                                 center: UnitPoint(x: 0.38, y: 0.32), startRadius: 0, endRadius: 40))
            .overlay(Circle().strokeBorder(Color(hex: glaze).opacity(0.5), lineWidth: 1))
            .accessibilityHidden(true)
    }
}

/// 허용 / 거부 / 항상 허용 for a waiting device command; the app's intent sends the decision.
private struct ApprovalButtons: View {
    let approval: PendingApproval
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 4) {
                Image(systemName: "hand.raised.fill").font(.caption).foregroundStyle(tint)
                Text(approval.shortCommand.map { "\(approval.device) · \($0)" } ?? "승인 필요")
                    .font(.caption.monospaced()).lineLimit(1)
            }
            HStack(spacing: 6) {
                Button(intent: ApprovalDecisionIntent(approvalID: approval.id, decision: "once")) { Text("허용").frame(maxWidth: .infinity) }
                    .tint(tint)
                Button(intent: ApprovalDecisionIntent(approvalID: approval.id, decision: "deny")) { Text("거부").frame(maxWidth: .infinity) }
                    .tint(.red)
                Button(intent: ApprovalDecisionIntent(approvalID: approval.id, decision: "always")) { Text("항상 허용").frame(maxWidth: .infinity) }
                    .tint(.gray)
                    .disabled(approval.forcedOnce)
            }
            .font(.caption.weight(.semibold))
            .buttonStyle(.bordered)
        }
    }
}

struct CallLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: CallActivityAttributes.self) { ctx in
            let s = ctx.state, a = ctx.attributes
            HStack(alignment: .center, spacing: 14) {
                Orb(glaze: a.glaze).frame(width: 44, height: 44)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(a.name).font(.headline).lineLimit(1)
                        Text(LiveActivityText.callState(s.state)).font(.subheadline).foregroundStyle(Color(hex: a.glaze))
                        if s.muted { Image(systemName: "mic.slash.fill").font(.caption).foregroundStyle(.secondary).accessibilityLabel("음소거") }
                        Spacer()
                        Text(a.startedAt, style: .timer).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing).frame(maxWidth: 64)
                    }
                    if let c = s.caption {
                        Text(c).font(.subheadline).foregroundStyle(s.mine ? .secondary : .primary).lineLimit(2)
                    }
                    if let ap = s.approval { ApprovalButtons(approval: ap, tint: Color(hex: a.glaze)).padding(.top, 4) }
                }
            }
            .padding(16)
            .activityBackgroundTint(Color.black.opacity(0.55))
            .activitySystemActionForegroundColor(.white)
            .widgetURL(URL(string: "azel://call"))
        } dynamicIsland: { ctx in
            let s = ctx.state, a = ctx.attributes
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Orb(glaze: a.glaze).frame(width: 40, height: 40).padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(a.startedAt, style: .timer).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing).frame(maxWidth: 60)
                }
                DynamicIslandExpandedRegion(.center) {
                    HStack(spacing: 6) {
                        Text(a.name).font(.headline).lineLimit(1)
                        Text(LiveActivityText.callState(s.state)).font(.subheadline).foregroundStyle(Color(hex: a.glaze))
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    if let ap = s.approval {
                        ApprovalButtons(approval: ap, tint: Color(hex: a.glaze))
                    } else if let c = s.caption {
                        Text(c).font(.subheadline).foregroundStyle(s.mine ? .secondary : .primary).lineLimit(2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            } compactLeading: {
                Orb(glaze: a.glaze).frame(width: 18, height: 18)
            } compactTrailing: {
                Image(systemName: LiveActivityText.symbol(callState: s.state)).foregroundStyle(Color(hex: a.glaze))
                    .accessibilityLabel(LiveActivityText.callState(s.state))
            } minimal: {
                Orb(glaze: a.glaze).frame(width: 18, height: 18)
            }
            .keylineTint(Color(hex: a.glaze))
            .widgetURL(URL(string: "azel://call"))
        }
    }
}

struct TaskLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: TaskActivityAttributes.self) { ctx in
            let s = ctx.state, a = ctx.attributes
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: icon(s.status)).font(.title2).foregroundStyle(Color(hex: a.glaze)).frame(width: 32)
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(a.goal).font(.headline).lineLimit(1)
                        Spacer()
                        Text(LiveActivityText.taskStatus(s.status)).font(.subheadline.weight(.semibold)).foregroundStyle(Color(hex: a.glaze))
                    }
                    if let step = s.step { Text(step).font(.subheadline).foregroundStyle(.secondary).lineLimit(2) }
                    if s.steps > 0 { Text("\(s.steps)단계").font(.caption).foregroundStyle(.secondary) }
                    if let ap = s.approval { ApprovalButtons(approval: ap, tint: Color(hex: a.glaze)).padding(.top, 4) }
                }
            }
            .padding(16)
            .activityBackgroundTint(Color.black.opacity(0.55))
            .activitySystemActionForegroundColor(.white)
            .widgetURL(URL(string: "azel://task/\(a.taskID)"))
        } dynamicIsland: { ctx in
            let s = ctx.state, a = ctx.attributes
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: icon(s.status)).font(.title2).foregroundStyle(Color(hex: a.glaze)).padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(LiveActivityText.taskStatus(s.status)).font(.subheadline.weight(.semibold)).foregroundStyle(Color(hex: a.glaze))
                }
                DynamicIslandExpandedRegion(.center) { Text(a.goal).font(.headline).lineLimit(1) }
                DynamicIslandExpandedRegion(.bottom) {
                    if let ap = s.approval { ApprovalButtons(approval: ap, tint: Color(hex: a.glaze)) }
                    else if let step = s.step { Text(step).font(.subheadline).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading) }
                }
            } compactLeading: {
                Image(systemName: icon(s.status)).foregroundStyle(Color(hex: a.glaze))
            } compactTrailing: {
                Text(LiveActivityText.taskStatus(s.status)).font(.caption2).foregroundStyle(Color(hex: a.glaze))
            } minimal: {
                Image(systemName: icon(s.status)).foregroundStyle(Color(hex: a.glaze))
            }
            .keylineTint(Color(hex: a.glaze))
            .widgetURL(URL(string: "azel://task/\(a.taskID)"))
        }
    }

    private func icon(_ status: String) -> String {
        switch status {
        case "done": return "checkmark.circle.fill"
        case "failed": return "xmark.octagon.fill"
        case "cancelled": return "slash.circle"
        case "waiting_approval": return "hand.raised.fill"
        default: return "gearshape.2.fill"
        }
    }
}
