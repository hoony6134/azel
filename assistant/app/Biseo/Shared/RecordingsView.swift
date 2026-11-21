import AVFoundation
import SwiftUI

/// Red dot + elapsed time while a call is being recorded.
struct RecordingBadge: View {
    var since: Date?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            let t = Int(ctx.date.timeIntervalSince(since ?? ctx.date))
            Label(String(format: "녹음 중 %02d:%02d", t / 60, t % 60), systemImage: "record.circle.fill")
                .font(.footnote.monospacedDigit().bold()).foregroundStyle(.red)
        }
        .accessibilityLabel("녹음 중")
    }
}

/// Play, share (audio + transcript) and delete call recordings.
struct RecordingsView: View {
    @State private var files: [URL] = []
    @State private var player: AVAudioPlayer?
    @State private var playing: URL?

    var body: some View {
        List {
            if files.isEmpty { Text("녹음이 없습니다").foregroundStyle(.secondary) }
            ForEach(files, id: \.self) { url in
                HStack {
                    Button { toggle(url) } label: {
                        Image(systemName: playing == url ? "stop.circle.fill" : "play.circle.fill").font(.title2)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(playing == url ? "정지" : "재생")
                    VStack(alignment: .leading) {
                        Text(title(url))
                        Text(url.deletingPathExtension().lastPathComponent).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    ShareLink(items: [url, Recordings.transcriptURL(for: url)].filter { FileManager.default.fileExists(atPath: $0.path) }) {
                        Image(systemName: "square.and.arrow.up")
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("공유")
                    Button(role: .destructive) { delete(url) } label: { Image(systemName: "trash") }
                        .buttonStyle(.plain)
                        .accessibilityLabel("삭제")
                }
            }
            .onDelete { $0.map { files[$0] }.forEach(delete) }
        }
        .navigationTitle("녹음")
        .onAppear { files = Recordings.list() }
        .onDisappear { player?.stop(); playing = nil }
    }

    private func title(_ url: URL) -> String {
        let date = (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .now
        let seconds = Int((try? AVAudioPlayer(contentsOf: url).duration) ?? 0)
        return date.formatted(date: .abbreviated, time: .shortened) + String(format: " · %d:%02d", seconds / 60, seconds % 60)
    }

    private func toggle(_ url: URL) {
        player?.stop()
        if playing == url { playing = nil; return }
        player = try? AVAudioPlayer(contentsOf: url)
        playing = player?.play() == true ? url : nil
    }

    private func delete(_ url: URL) {
        if playing == url { player?.stop(); playing = nil }
        Recordings.delete(url)
        files = Recordings.list()
    }
}
