import AVFoundation
import SwiftUI

// MARK: - Contract

struct WakeEnrollStatus: Decodable, Equatable {
    var clips: Int
    var trained_at: Date?
    var status: String              // none | training | ready | failed
    // Retraining server (RETRAIN.md); all nil on the old one.
    var min_clips: Int?
    var improved: Bool?
    var error: String?
    /// recall_before/after, fa_nearmiss(_before), … ; nil until trained.
    var metrics: [String: Double]?

    private enum Keys: String, CodingKey { case clips, trained_at, status, min_clips, improved, error, metrics }

    /// The server retrains a model on the clips (and reports `min_clips`).
    var retrains: Bool { min_clips != nil }

    /// The result line under the status, or nil when there is nothing to report yet.
    var resultText: String? {
        if let error, !error.isEmpty { return error }
        guard status == "ready", let improved else { return nil }
        if !improved { return "기존 모델을 유지했어요 (오인식이 늘어서)" }
        guard let b = metrics?["recall_before"], let a = metrics?["recall_after"] else { return "내 목소리로 학습한 모델로 바꿨어요" }
        return "인식률 \(Self.percent(b)) → \(Self.percent(a))"
    }

    /// "24/60, 20개부터 학습"
    static func countLine(clips: Int, minClips: Int) -> String { "\(clips)/\(WakeEnrollAPI.maxTotal), \(minClips)개부터 학습" }

    /// 0.82 → "82%" (a value over 1 is taken as already in percent).
    static func percent(_ v: Double) -> String { "\(Int((v <= 1 ? v * 100 : v).rounded()))%" }

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
        min_clips = try? c.decodeIfPresent(Int.self, forKey: .min_clips)
        improved = try? c.decodeIfPresent(Bool.self, forKey: .improved)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        metrics = (try? c.decodeIfPresent([String: Double?].self, forKey: .metrics))?.compactMapValues { $0 }
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
    /// Retraining server: clips per request and in total.
    static let maxTotal = 60

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
        guard (200..<300).contains(code) else { throw GatewayError.from(status: code, body: data) }
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

    /// Retraining prompts, cycled (PM, 2026-11-22).
    nonisolated static let tones = ["보통 목소리로", "작게", "멀리서 부르듯이", "빠르게", "피곤한 톤으로"]

    nonisolated static func retrainPrompts(_ name: String, count: Int) -> [String] {
        (0..<count).map { "‘\(name)’ — " + tones[$0 % tones.count] }
    }

    /// Clips to record this time: up to `min_clips` the first time, then 10 more per session,
    /// never past the server's 60.
    nonisolated static func sessionLength(stored: Int, minClips: Int) -> Int {
        let want = stored < minClips ? minClips - stored : 10
        return max(0, min(want, WakeEnrollAPI.maxTotal - stored))
    }

    @Published private(set) var step: Step = .intro
    @Published private(set) var problem: WakeClip.Problem?
    @Published private(set) var listening = false
    @Published private(set) var status: WakeEnrollStatus?
    @Published private(set) var statusNote: SectionStatus = .ok
    @Published private(set) var prompts: [String]
    private let name: String
    let recorder = WakeClipRecorder()
    /// Good clips by prompt index; memory only, cleared after upload.
    private(set) var clips: [Int: [Float]] = [:]
    private var redo: [Int] = []

    init(name: String = AssistantName.current) { self.name = name; prompts = Self.prompts(name) }

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
        if let s = status, let minClips = s.min_clips {
            // Retraining server: recording never waits for training; clips sent during a run are
            // stored and trigger another run when it ends (wake_enroll.py train_loop).
            // Retraining server: keep what's stored and add to it.
            let n = Self.sessionLength(stored: s.clips, minClips: minClips)
            guard n > 0 else { step = .failed("녹음은 \(WakeEnrollAPI.maxTotal)개까지예요. 기본 모델로 되돌리면 처음부터 다시 할 수 있어요."); return }
            prompts = Self.retrainPrompts(name, count: n)
        } else if let s = status, s.status == "training" {
            step = .failed(Self.busyMessage); return
        } else if let s = status, s.clips + prompts.count > WakeEnrollAPI.maxStored {
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

    /// Training runs on the Mac (1–4 minutes); poll until ready/failed, up to ~5 minutes.
    func pollTraining() async {
        for _ in 0..<100 {
            await refreshStatus()
            switch status?.status {
            case "ready": step = .done; return
            case "failed": step = .failed(status?.error ?? "학습에 실패했어요. 다시 등록해 주세요."); return
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
            case .training: progress(model.status?.retrains == true ? "목소리를 익히는 중이에요… 보통 1–4분 걸려요" : "목소리를 익히는 중이에요…")
            case .done: finished
            case .failed(let why):
                EmptyState(title: "호출어를 등록하지 못했어요", message: why, action: ("다시 시도", { Task { await model.begin() } }))
            }
        }
        .padding(20)
        .frame(maxWidth: 520, maxHeight: .infinity)
        .frame(maxWidth: .infinity)
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("내 목소리로 호출어 학습")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("닫기") { model.cancelRecording(); dismiss() } } }
        .onDisappear {
            model.cancelRecording()
            #if os(macOS)
            AzelFiles.enrolling.remove()
            #endif
        }
        #if os(macOS)
        // The wake listener ignores the mic while this exists, so recording "아젤" doesn't wake it.
        .onAppear { AzelFiles.enrolling.create() }
        #endif
    }

    private var intro: some View {
        VStack(spacing: Theme.l) {
            Spacer()
            MoonJar(state: .idle, levels: .silent).frame(width: 160, height: 160)
            Text("목소리로 \(AssistantName.josa(AssistantName.current, "을", "를")) 불러요").font(.title2.weight(.semibold)).foregroundStyle(Theme.text)
            Text(introText)
                .multilineTextAlignment(.center).foregroundStyle(Theme.text2)
            Text("녹음은 사용자의 Mac에만 저장되고, 이 기기에서는 올린 뒤 바로 지워져요.")
                .font(.footnote).multilineTextAlignment(.center).foregroundStyle(Theme.text3)
            Spacer()
            primary("시작하기") { Task { await model.begin() } }
        }
    }

    private var introText: String {
        let n = AssistantName.current, p = WakeEnrollModel.particle(n, "을", "를")
        if let s = model.status, let minClips = s.min_clips {
            let first = s.clips < minClips
            return "‘\(n)’\(p) 보통 목소리, 작게, 멀리서, 빠르게, 피곤한 톤으로 번갈아 말해 주세요. "
                + (first ? "\(minClips)번 녹음하면 학습을 시작해요." : "지금까지 \(s.clips)개를 녹음했어요. 더 녹음하면 그 목소리까지 넣어 다시 학습해요.")
        }
        return "‘\(n)’\(p) \(model.prompts.count)번, 조금씩 다르게 말해 주세요. 한 번 말할 때마다 자동으로 넘어가요."
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
            if let s = model.status, s.retrains {
                Text(s.resultText ?? "학습이 아직 끝나지 않았어요. 설정에서 결과를 볼 수 있어요.").font(.title3.weight(.semibold))
                    .multilineTextAlignment(.center).foregroundStyle(Theme.text)
            } else {
                Text("이제 \(AssistantName.josa(AssistantName.current, "이", "가")) 당신 목소리에만 반응해요").font(.title3.weight(.semibold))
                    .multilineTextAlignment(.center).foregroundStyle(Theme.text)
            }
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
                Text(model.status?.retrains == true ? "내 목소리로 호출어 학습" : "호출어 등록").foregroundStyle(Theme.text)
                Spacer()
                Text(statusText).font(.subheadline).foregroundStyle(Theme.text2)
            }
            SectionNote(status: model.statusNote) { Task { await model.refreshStatus() } }
            if let s = model.status, s.retrains {
                if let r = s.resultText { Text(r).font(.subheadline).foregroundStyle(s.error == nil ? Theme.text2 : Theme.danger) }
                if s.status == "training" { Text("학습 중이에요. 보통 1–4분 걸려요.").font(.footnote).foregroundStyle(Theme.text3) }
                if let minClips = s.min_clips {
                    Text(WakeEnrollStatus.countLine(clips: s.clips, minClips: minClips)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                }
            }
            if let m = model.removeMessage { Text(m).font(.footnote).foregroundStyle(Theme.amber) }
            HStack {
                Button(enrollTitle) { enrolling = true }
                    .tint(Theme.celadon)
                    .disabled(model.status.map { $0.retrains ? $0.clips >= WakeEnrollAPI.maxTotal : $0.status == "training" } ?? false)
                Spacer()
                if let s = model.status, s.status != "none" || s.clips > 0 {
                    Button(s.retrains ? "기본 모델로 되돌리기" : "삭제", role: .destructive) { confirmDelete = true }
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
        .confirmationDialog(model.status?.retrains == true ? "기본 모델로 되돌릴까요?" : "등록한 호출어 목소리를 지울까요?",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button(model.status?.retrains == true ? "되돌리기" : "삭제", role: .destructive) { Task { await model.remove() } }
            Button("취소", role: .cancel) {}
        } message: {
            if model.status?.retrains == true { Text("녹음한 목소리와 학습한 모델을 모두 지우고 처음 모델로 돌아가요.") }
        }
    }

    private var enrollTitle: String {
        guard let s = model.status else { return "등록" }
        if s.retrains { return s.clips == 0 ? "녹음 시작" : "더 녹음하기" }
        return s.status == "ready" || s.status == "failed" ? "다시 등록" : "등록"
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
