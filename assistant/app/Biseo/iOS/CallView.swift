import AVKit
import SwiftUI

// MARK: - Call

struct CallView: View {
    @ObservedObject private var call = CallManager.shared
    @State private var session = Session.current()
    @State private var showRecordings = false

    private var stateText: String {
        ["listening": "듣는 중", "thinking": "생각 중", "speaking": "말하는 중"][call.state] ?? "연결 중"
    }

    var body: some View {
        VStack(spacing: 16) {
            if call.inCall {
                Text(call.held ? "대기 중" : stateText).font(.title2.bold())
                if call.recording { RecordingBadge(since: call.connectedAt) }
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
                HStack(spacing: 12) {
                    control(call.speaker ? "스피커 끄기" : "스피커", "speaker.wave.2.fill", on: call.speaker) { call.toggleSpeaker() }
                    RoutePicker().frame(width: 44, height: 44).accessibilityLabel("오디오 출력 선택")
                    control(call.held ? "대기 해제" : "대기", "pause.fill", on: call.held) { call.toggleHold() }
                    control(call.recording ? "녹음 중지" : "녹음", "record.circle", on: call.recording, tint: .red) { call.toggleRecording() }
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
                Button { showRecordings = true } label: { Label("녹음 목록", systemImage: "waveform") }
                Spacer()
            }
            if let error = call.error { Text(error).font(.footnote).foregroundStyle(.red) }
        }
        .padding()
        .onAppear { session = Session.current() }
        .sheet(isPresented: $showRecordings) { NavigationStack { RecordingsView() } }
    }

    private func control(_ title: String, _ icon: String, on: Bool, tint: Color = .accentColor, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.title3).frame(width: 44, height: 44)
                .foregroundStyle(on ? .white : .primary)
                .background(on ? tint : Color.secondary.opacity(0.15), in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }
}

/// AirPods / Bluetooth / receiver / speaker picker.
struct RoutePicker: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let v = AVRoutePickerView()
        v.prioritizesVideoDevices = false
        return v
    }
    func updateUIView(_ view: AVRoutePickerView, context: Context) {}
}
