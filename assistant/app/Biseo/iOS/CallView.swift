import SwiftUI

// MARK: - Call

struct CallView: View {
    @ObservedObject private var call = CallManager.shared
    @State private var session = Session.current()

    private var stateText: String {
        ["listening": "듣는 중", "thinking": "생각 중", "speaking": "말하는 중"][call.state] ?? "연결 중"
    }

    var body: some View {
        VStack(spacing: 16) {
            if call.inCall {
                Text(stateText).font(.title2.bold())
                ScrollViewReader { proxy in
                    List(call.lines) { line in
                        HStack(alignment: .firstTextBaseline) {
                            if line.mine { Spacer(minLength: 0) }
                            Text(line.text).foregroundStyle(line.mine ? .primary : Color.accentColor)
                            if let route = line.route {
                                Text(route).font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(route == "claude" ? Color.orange.opacity(0.2) : Color.blue.opacity(0.2), in: Capsule())
                                    .accessibilityLabel("\(route) 응답")
                            }
                            if !line.mine { Spacer(minLength: 0) }
                        }
                        .id(line.id)
                    }
                    .listStyle(.plain)
                    .onChange(of: call.lines.count) { if let last = call.lines.last { proxy.scrollTo(last.id) } }
                }
                HStack {
                    Button { call.toggleMute() } label: {
                        Label(call.muted ? "음소거 해제" : "음소거", systemImage: call.muted ? "mic.slash.fill" : "mic.fill")
                            .frame(maxWidth: .infinity).padding()
                    }
                    .buttonStyle(.bordered).tint(call.muted ? .orange : .primary)
                    Button(role: .destructive) { call.hangUp() } label: {
                        Label("끊기", systemImage: "phone.down.fill").frame(maxWidth: .infinity).padding()
                    }
                    .buttonStyle(.borderedProminent).tint(.red)
                }
            } else {
                Spacer()
                Button { call.startCall() } label: {
                    Label("박비서에게 전화", systemImage: "phone.fill")
                        .font(.title2.bold()).frame(maxWidth: .infinity).padding(.vertical, 28)
                }
                .buttonStyle(.borderedProminent).tint(.green)
                Button { session = Session.rotate() } label: { Label("새 대화", systemImage: "square.and.pencil") }
                Text(session).font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
            if let error = call.error { Text(error).font(.footnote).foregroundStyle(.red) }
        }
        .padding()
        .onAppear { session = Session.current() }
    }
}
