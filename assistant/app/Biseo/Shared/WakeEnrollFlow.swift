import AVFoundation
import SwiftUI

// MARK: - Contract

struct WakeEnrollStatus: Decodable, Equatable {
    var clips: Int
    var trained_at: Date?
    var status: String              // none | training | ready | failed

    private enum Keys: String, CodingKey { case clips, trained_at, status }

    init(clips: Int, trained_at: Date?, status: String) { self.clips = clips; self.trained_at = trained_at; self.status = status }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        clips = try c.decodeIfPresent(Int.self, forKey: .clips) ?? 0
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "none"
        if let s = try? c.decodeIfPresent(String.self, forKey: .trained_at) {
            let f = ISO8601DateFormatter()
            trained_at = f.date(from: s) ?? { f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f.date(from: s) }()
        } else if let n = try? c.decodeIfPresent(Double.self, forKey: .trained_at) {
            trained_at = Date(timeIntervalSince1970: n)
        } else { trained_at = nil }
    }
}

struct WakeEnrollResult: Decodable, Equatable {
    struct Rejected: Decodable, Equatable { var index: Int; var reason: String }
    var accepted: Int
    var rejected: [Rejected]
    var training: Bool

    private enum Keys: String, CodingKey { case accepted, rejected, training }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        accepted = try c.decodeIfPresent(Int.self, forKey: .accepted) ?? 0
        rejected = try c.decodeIfPresent([Rejected].self, forKey: .rejected) ?? []
        training = try c.decodeIfPresent(Bool.self, forKey: .training) ?? false
    }
}

enum WakeEnrollAPI {
    static func status() async throws -> WakeEnrollStatus {
        try JSONDecoder().decode(WakeEnrollStatus.self, from: try await Gateway.send("GET", "/v1/wake/enroll"))
    }

    static let maxStored = 20

    /// 409 while training.
    static func delete() async throws { _ = try await Gateway.send("DELETE", "/v1/wake/enroll") }

    /// Multipart: one `clips` part per WAV (the field repeats; it is not `clips[]`).
    static func multipart(_ clips: [Data], boundary: String) -> Data {
        var body = Data()
        for (i, wav) in clips.enumerated() {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"clips\"; filename=\"clip\(i).wav\"\r\n".utf8))
            body.append(Data("Content-Type: audio/wav\r\n\r\n".utf8))
            body.append(wav)
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        return body
    }

    static func upload(_ clips: [Data]) async throws -> WakeEnrollResult {
        guard let url = URL(string: Config.gatewayURL + "/v1/wake/enroll") else { throw URLError(.badURL) }
        let boundary = "azel-\(UUID().uuidString)"
        var req = URLRequest(url: url, timeoutInterval: 60)
        req.httpMethod = "POST"
        req.setValue("Bearer \(Config.token ?? "")", forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, resp) = try await URLSession.shared.upload(for: req, from: multipart(clips, boundary: boundary))
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 400, let m = GatewayError.message(in: data) { throw GatewayError.rejected(m) }
        guard (200..<300).contains(code) else { throw GatewayError.http(code) }
        return try JSONDecoder().decode(WakeEnrollResult.self, from: data)
    }
}

// MARK: - Flow

/// 호출어 등록: intro → prompts (record, check, retry) → upload → training → done.
@MainActor
final class WakeEnrollModel: ObservableObject {
    enum Step: Equatable { case intro, recording(Int), uploading, training, done, failed(String) }

    static func prompts(_ name: String) -> [String] {
        ["‘\(name)’\(particle(name, "이라고", "라고")) 말해 주세요", "한 번 더, 평소처럼", "조금 작게", "조금 크게", "멀리서 부르듯이", "빠르게 한 번", "천천히 한 번", "마지막으로 한 번 더"]
    }

    /// Particle after a quoted name (‘아젤’이라고 / ‘하나’라고).
    static func particle(_ name: String, _ withBatchim: String, _ without: String) -> String {
        String(AssistantName.josa(name, withBatchim, without).dropFirst(name.count))
    }

    @Published private(set) var step: Step = .intro
    @Published private(set) var problem: WakeClip.Problem?
    @Published private(set) var listening = false
    @Published private(set) var status: WakeEnrollStatus?
    @Published private(set) var statusNote: SectionStatus = .ok
    let prompts: [String]
    let recorder = WakeClipRecorder()
    /// Good clips by prompt index; memory only, cleared after upload.
    private(set) var clips: [Int: [Float]] = [:]
    private var redo: [Int] = []

    init(name: String = AssistantName.current) { prompts = Self.prompts(name) }

    func refreshStatus() async {
        switch await attempt({ try await WakeEnrollAPI.status() }) {
        case .success(let s): status = s; statusNote = .ok
        case .failure(let e): statusNote = SectionStatus.from(e) ?? statusNote
        }
    }

    func begin() async {
        clips = [:]
        redo = []
        // The server keeps at most 20 clips: a re-enrollment starts from a clean slate.
        await refreshStatus()
        if let s = status, s.clips + prompts.count > WakeEnrollAPI.maxStored {
            if s.status == "training" { step = .failed(Self.busyMessage); return }
            _ = await attempt { try await WakeEnrollAPI.delete() }
        }
        guard await SetupFlow.requestMic() else { step = .failed("마이크 권한이 필요해요"); return }
        step = .recording(0)
    }

    func record() {
        guard case .recording(let i) = step, !listening else { return }
        problem = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setCategory(.record, mode: .measurement)
        try? AVAudioSession.sharedInstance().setActive(true)
        #endif
        do {
            listening = true
            try recorder.record { [weak self] clip in
                guard let self else { return }
                listening = false
                if let p = WakeClip.check(clip) { problem = p; return }
                guard let trimmed = WakeClip.trimmed(clip), WakeClip.uploadable(trimmed) else { problem = .tooLong; return }
                clips[i] = trimmed
                advance(from: i)
            }
        } catch {
            listening = false
            Log.call.error("wake record failed: \(error.localizedDescription, privacy: .public)")
            step = .failed("녹음을 시작하지 못했어요 (\(error.localizedDescription))")
        }
    }

    func cancelRecording() { recorder.cancel(); listening = false }

    private func advance(from i: Int) {
        let next = redo.isEmpty ? i + 1 : (redo.first { $0 > i } ?? prompts.count)
        if next < prompts.count { step = .recording(next) } else { Task { await upload() } }
    }

    func upload() async {
        step = .uploading
        let order = clips.keys.sorted()
        let wavs = order.map { WakeClip.wav(clips[$0]!) }
        do {
            let result = try await WakeEnrollAPI.upload(wavs)
            clips = [:]                                   // nothing stays on this device
            if !result.rejected.isEmpty {
                redo = result.rejected.compactMap { $0.index < order.count ? order[$0.index] : nil }.sorted()
                problem = nil
                if let first = redo.first { step = .recording(first); return }
            }
            redo = []
            step = .training
            await pollTraining()
        } catch GatewayError.rejected(let why) {
            step = .failed(why)
        } catch {
            Log.call.error("wake upload failed: \(String(describing: error), privacy: .public)")
            step = .failed(SectionStatus.from(error) == .unavailable ? "호출어 등록은 서버 업데이트 후 사용할 수 있어요"
                           : "올리지 못했어요: \(error.localizedDescription)")
        }
    }

    /// Training runs on the Mac; poll until ready/failed (up to ~3 minutes).
    func pollTraining() async {
        for _ in 0..<60 {
            await refreshStatus()
            switch status?.status {
            case "ready": step = .done; return
            case "failed": step = .failed("학습에 실패했어요. 다시 등록해 주세요."); return
            default: try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
        step = .done // still training on the server; the settings row shows the status
    }

    static let busyMessage = "학습 중이라 잠시 후 다시 시도해 주세요"
    @Published var removeMessage: String?

    func remove() async {
        if case .failure(let e) = await attempt({ try await WakeEnrollAPI.delete() }), case GatewayError.http(409) = e {
            removeMessage = Self.busyMessage
        } else { removeMessage = nil }
        await refreshStatus()
    }
}

// MARK: - Screens

struct WakeEnrollView: View {
    @StateObject private var model = WakeEnrollModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: Theme.xl) {
            switch model.step {
            case .intro: intro
            case .recording(let i): recording(i)
            case .uploading: progress("올리는 중…")
            case .training: progress("목소리를 익히는 중이에요…")
            case .done: finished
            case .failed(let why):
                EmptyState(title: "호출어를 등록하지 못했어요", message: why, action: ("다시 시도", { Task { await model.begin() } }))
            }
        }
        .padding(20)
        .frame(maxWidth: 520, maxHeight: .infinity)
        .frame(maxWidth: .infinity)
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("호출어 등록")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("닫기") { model.cancelRecording(); dismiss() } } }
        .onDisappear { model.cancelRecording() }
    }

    private var intro: some View {
        VStack(spacing: Theme.l) {
            Spacer()
            MoonJar(state: .idle, levels: .silent).frame(width: 160, height: 160)
            Text("목소리로 \(AssistantName.josa(AssistantName.current, "을", "를")) 불러요").font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
            Text("‘\(AssistantName.current)’\(WakeEnrollModel.particle(AssistantName.current, "을", "를")) \(model.prompts.count)번, 조금씩 다르게 말해 주세요. 한 번 말할 때마다 자동으로 넘어가요.")
                .multilineTextAlignment(.center).foregroundStyle(Theme.text2)
            Text("녹음은 사용자의 Mac에만 저장되고, 이 기기에서는 올린 뒤 바로 지워져요.")
                .font(.footnote).multilineTextAlignment(.center).foregroundStyle(Theme.text3)
            Spacer()
            primary("시작하기") { Task { await model.begin() } }
        }
    }

    private func recording(_ i: Int) -> some View {
        VStack(spacing: Theme.l) {
            Text("\(i + 1) / \(model.prompts.count)").font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
            Text(model.prompts[i]).font(.title2.weight(.semibold)).multilineTextAlignment(.center).foregroundStyle(Theme.text)
            MoonJar(state: model.listening ? .listening : .idle, levels: model.recorder.levels).frame(width: 200, height: 200)
            if let p = model.problem {
                Label(p.message, systemImage: "exclamationmark.triangle").font(.subheadline).foregroundStyle(Theme.amber)
                    .multilineTextAlignment(.center)
            } else {
                Text(model.listening ? "듣고 있어요…" : "준비되면 누르고 말해 주세요").font(.subheadline).foregroundStyle(Theme.text2)
            }
            Spacer()
            primary(model.listening ? "듣는 중" : (model.problem == nil ? "녹음" : "다시 녹음")) { model.record() }
                .disabled(model.listening)
        }
        .animation(Theme.quick, value: model.problem)
    }

    private func progress(_ text: String) -> some View {
        VStack(spacing: Theme.l) {
            Spacer()
            MoonJar(state: .thinking, levels: .silent).frame(width: 160, height: 160)
            Text(text).foregroundStyle(Theme.text2)
            ProgressView()
            Spacer()
        }
    }

    private var finished: some View {
        VStack(spacing: Theme.l) {
            Spacer()
            MoonJar(state: .speaking, levels: .silent).frame(width: 160, height: 160)
            Text("이제 \(AssistantName.josa(AssistantName.current, "이", "가")) 당신 목소리에만 반응해요").font(.title3.weight(.semibold))
                .multilineTextAlignment(.center).foregroundStyle(Theme.text)
            Spacer()
            primary("완료") { dismiss() }
        }
    }

    private func primary(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.headline).foregroundStyle(Theme.onAccent).frame(maxWidth: .infinity).frame(height: 52)
                .background(Theme.celadon, in: RoundedRectangle(cornerRadius: Theme.callButtonRadius, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}

/// Settings row: status, 다시 등록, 삭제.
struct WakeEnrollRow: View {
    @StateObject private var model = WakeEnrollModel()
    @State private var enrolling = false
    @State private var confirmDelete = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s) {
            HStack {
                Text("호출어 등록").foregroundStyle(Theme.text)
                Spacer()
                Text(statusText).font(.subheadline).foregroundStyle(Theme.text2)
            }
            SectionNote(status: model.statusNote) { Task { await model.refreshStatus() } }
            if let m = model.removeMessage { Text(m).font(.footnote).foregroundStyle(Theme.amber) }
            HStack {
                Button(model.status?.status == "ready" || model.status?.status == "failed" ? "다시 등록" : "등록") { enrolling = true }
                    .tint(Theme.celadon)
                Spacer()
                if let s = model.status, s.status != "none" {
                    Button("삭제", role: .destructive) { confirmDelete = true }
                }
            }
            .font(.subheadline)
            .disabled(model.statusNote == .unavailable)
        }
        .task { await model.refreshStatus() }
        .sheet(isPresented: $enrolling, onDismiss: { Task { await model.refreshStatus() } }) {
            NavigationStack { WakeEnrollView() }
                #if os(macOS)
                .frame(width: 480, height: 620)
                #endif
        }
        .confirmationDialog("등록한 호출어 목소리를 지울까요?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("삭제", role: .destructive) { Task { await model.remove() } }
            Button("취소", role: .cancel) {}
        }
    }

    private var statusText: String {
        guard let s = model.status else { return "" }
        switch s.status {
        case "ready": return "등록됨" + (s.trained_at.map { " · " + $0.formatted(date: .abbreviated, time: .omitted) } ?? "")
        case "training": return "학습 중"
        case "failed": return "실패"
        default: return "등록 안 됨"
        }
    }
}
