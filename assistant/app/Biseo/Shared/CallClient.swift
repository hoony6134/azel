import Foundation

/// `WS /v1/call` (PROTOCOL.md §1): streams the mic up, plays replies, honours `interrupt`.
/// The platform shell (CallKit on iOS, the floating panel on macOS) owns the call UI and lifecycle.
final class CallClient {
    enum StartError: LocalizedError {
        case notConfigured, badURL, audio
        var errorDescription: String? {
            switch self {
            case .notConfigured: return "서버 설정이 없습니다"
            case .badURL: return "잘못된 음성 서버 주소"
            case .audio: return "오디오 시작 실패"
            }
        }
    }

    /// Server text events, on the main queue.
    var onEvent: ((CallEvent) -> Void)?
    /// The socket closed or failed (not after `stop()`), on the main queue, with a message for the
    /// user when the call never got going.
    var onClose: ((String?) -> Void)?
    private var receivedAny = false
    /// Something the user should see without the call ending (e.g. the mic sends only silence).
    var onWarning: ((String) -> Void)?
    private let statsLock = NSLock()
    private var sentBytes = 0, sentPeak: Int16 = 0, receivedAudioBytes = 0
    private var silenceCheck: DispatchWorkItem?

    static let silentMicMessage = "마이크 소리가 들어오지 않아요. 입력 장치와 마이크 권한을 확인해 주세요."

    /// Peak sample of PCM16 bytes (all-zero chunks mean the mic path is broken, not a quiet room).
    static func peak(_ pcm16: Data) -> Int16 {
        pcm16.withUnsafeBytes { raw in
            (0..<(pcm16.count / 2)).reduce(Int16(0)) { m, i in
                let v = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                return max(m, v == .min ? .max : abs(v))
            }
        }
    }

    /// 401/403 on the WebSocket handshake: wrong token, or a server that doesn't take the header yet.
    static func closeMessage(status: Int?, receivedAny: Bool) -> String? {
        if status == 401 || status == 403 { return "토큰이 맞지 않거나 서버가 아직 업데이트되지 않았어요" }
        if !receivedAny { return "음성 서버에 연결하지 못했어요" + (status.map { " (HTTP \($0))" } ?? "") }
        return nil
    }
    /// Read on the audio thread; set from the main thread.
    var muted = false
    /// Hold: no mic goes up, playback pauses, the recording pauses. The socket stays open.
    var held = false {
        didSet { if held != oldValue { playback.held = held; applyGate() } }
    }
    /// Playback runs only when neither the user (hold) nor the server (pause) stopped it.
    private var playback = PlaybackGate()

    private func applyGate() { playback.playing ? audio.resume() : audio.pause() }
    var isConnected: Bool { socket != nil }
    /// Mac push-to-talk: false between key presses, so nothing goes up (the socket stays open).
    var sending = true
    var isRecording: Bool { recorder != nil }

    let audio = AudioIO()
    private var socket: URLSessionWebSocketTask?
    private let lock = NSLock()
    private var _recorder: CallRecorder?
    private var recorder: CallRecorder? {
        get { lock.lock(); defer { lock.unlock() }; return _recorder }
        set { lock.lock(); _recorder = newValue; lock.unlock() }
    }

    enum MicAction: Equatable { case send, silence, drop }
    /// What happens to one mic chunk: held drops it (and pauses the recording timeline); muted keeps
    /// the recording's clock running with silence but sends nothing.
    static func micAction(muted: Bool, held: Bool) -> MicAction { held ? .drop : muted ? .silence : .send }

    init() {}

    /// The token goes in the Authorization header, never the query: query strings end up in proxy logs.
    /// `intent`: e.g. "briefing" for 오늘 브리핑 / the Siri briefing, so the server opens with it.
    static func request(voiceURL: String, token: String, session: String, callID: String?, intent: String? = nil,
                        persona: String? = nil, mode: String? = nil) throws -> URLRequest {
        guard var url = URLComponents(string: voiceURL + "/v1/call") else { throw StartError.notConfigured }
        url.queryItems = [URLQueryItem(name: "session", value: session)]
        if let callID { url.queryItems?.append(URLQueryItem(name: "call_id", value: callID)) }
        if let intent { url.queryItems?.append(URLQueryItem(name: "intent", value: intent)) }
        if let persona { url.queryItems?.append(URLQueryItem(name: "persona", value: persona)) }
        if let mode { url.queryItems?.append(URLQueryItem(name: "mode", value: mode)) }
        guard let wsURL = url.url, ["ws", "wss"].contains(wsURL.scheme) else { throw StartError.badURL }
        var req = URLRequest(url: wsURL)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return req
    }

    /// iOS: call only once the audio session is active. If this throws `.audio` the socket is open: call `stop()`.
    func start(session: String, callID: String? = nil, intent: String? = nil, persona: String? = nil, mode: String? = nil) throws {
        guard let token = Config.token else { throw StartError.notConfigured }
        let task = URLSession.shared.webSocketTask(with: try Self.request(voiceURL: Config.voiceURL, token: token,
                                                                         session: session, callID: callID, intent: intent, persona: persona, mode: mode))
        socket = task
        receivedAny = false
        statsLock.lock(); sentBytes = 0; sentPeak = 0; receivedAudioBytes = 0; droppedAudioBytes = 0; statsLock.unlock()
        Log.call.info("call start session=\(session, privacy: .public) intent=\(intent ?? "-", privacy: .public) host=\(task.originalRequest?.url?.host ?? "?", privacy: .public)")
        task.resume()
        audio.onConfigurationChange = { [weak self] in self?.routeChanged() }
        audio.onMic = { [weak self, weak task] data in
            guard let self else { return }
            let action = sending ? Self.micAction(muted: muted, held: held) : .drop
            if action == .send {
                task?.send(.data(data)) { error in if let error { Log.call.error("mic send failed: \(error.localizedDescription, privacy: .public)") } }
                let p = Self.peak(data)
                statsLock.lock(); sentBytes += data.count; sentPeak = max(sentPeak, p); statsLock.unlock()
            }
            if action != .drop { recorder?.mic(data, silent: action == .silence) }
        }
        do { try audio.start() } catch {
            Log.call.error("audio start failed: \(error.localizedDescription, privacy: .public)")
            throw StartError.audio
        }
        receive(task, call: beginCall())
        pulseTimer?.invalidate()
        wasPlaying = false
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let playing = audio.speechPlaying
            if playing != wasPlaying { wasPlaying = playing; onPlayback?(playing) }
            reportPlayed()
            if let cue = thinking.tick(now: ProcessInfo.processInfo.systemUptime) { self.cue(cue, listening: false) }
        }
        // 4 s in: if everything sent so far is exact zeros, the mic path is broken; say so.
        let check = DispatchWorkItem { [weak self] in
            guard let self else { return }
            statsLock.lock(); let bytes = sentBytes, peak = sentPeak, got = receivedAudioBytes; statsLock.unlock()
            Log.call.info("call 4s: sent \(bytes, privacy: .public) B peak \(peak, privacy: .public), received audio \(got, privacy: .public) B")
            if bytes > 0 && peak == 0 { onWarning?(Self.silentMicMessage) }
        }
        silenceCheck = check
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: check)
    }

    private func receive(_ task: URLSessionWebSocketTask, call: Int) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                if !receivedAny { receivedAny = true; Log.call.info("call socket open, first message") }
                deliver(message, call: call)
            case .failure:
                let status = (task.response as? HTTPURLResponse)?.statusCode
                let message = Self.closeMessage(status: status, receivedAny: receivedAny)
                Log.call.error("call socket closed: status=\(status ?? -1, privacy: .public) code=\(task.closeCode.rawValue, privacy: .public)")
                DispatchQueue.main.async { if task === self.socket { self.onClose?(message) } }
                return
            }
            receive(task, call: call)
        }
    }

    /// Which call the socket callbacks belong to: bumped at start and at stop, so a frame of the
    /// call that just ended (still in flight after hangup) never plays, in this call or the next.
    private var callNumber = 0

    private func beginCall() -> Int {
        admissionLock.lock(); defer { admissionLock.unlock() }
        callNumber += 1
        return callNumber
    }

    /// `process` for a message of call `call`; dropped (audio counted) when that call is over.
    func deliver(_ message: URLSessionWebSocketTask.Message, call: Int) {
        admissionLock.lock(); let current = callNumber == call; admissionLock.unlock()
        guard current else {
            if case .data(let pcm) = message { statsLock.lock(); droppedAudioBytes += pcm.count; statsLock.unlock() }
            return
        }
        process(message)
    }

    /// For tests: the number the next socket's callbacks would carry if a call were up.
    var currentCall: Int { admissionLock.lock(); defer { admissionLock.unlock() }; return callNumber }

    /// One socket message, in arrival order. Audio and interrupts are handled right here; the UI
    /// hops to main. Internal (not private) so tests can feed a scripted server.
    func process(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .data(let pcm):
            // After an interrupt, frames of the old reply may still arrive: drop them.
            admissionLock.lock(); let admit = admission.admits; admissionLock.unlock()
            guard admit else { statsLock.lock(); droppedAudioBytes += pcm.count; statsLock.unlock(); return }
            audio.play(pcm)
            karaokeLock.lock(); karaoke.audio(samples: pcm.count / 2); karaokeLock.unlock()
            playedLock.lock(); played.audio(samples: pcm.count / 2); playedLock.unlock()
            DispatchQueue.main.async { self.thinking.audioArrived() }
            statsLock.lock(); receivedAudioBytes += pcm.count; statsLock.unlock()
            recorder?.assistant(pcm)
        case .string(let text):
            guard let event = try? JSONDecoder().decode(CallEvent.self, from: Data(text.utf8)) else { return }
            if event.type != "activity" { Log.call.info("event \(event.type, privacy: .public) \(event.value ?? "", privacy: .public)") }
            if event.type == "error" { Log.call.error("server error: \(event.message ?? "", privacy: .public)") }
            // Event order matters for audio, so these run here, before the UI hop.
            admissionLock.lock()
            let newReply = event.type == "reply_start" && admission.replyStart(event.reply_id)
            admission.apply(type: event.type, value: event.value)
            admissionLock.unlock()
            // A new reply queues after the previous one's tail (PM, 2026-11-22: a reply_start alone
            // never cuts audio; only `interrupt` does).
            playedLock.lock()
            if newReply, let id = event.reply_id { played.start(id) }
            if event.type == "turn_done" { played.close() }
            playedLock.unlock()
            switch event.type {
            case "interrupt":
                cutPlayback()
                recorder?.interrupt()
            case "pause", "resume":
                playback.apply(event.type)   // tentative barge-in: stop playback, keep the queue
                applyGate()
            default: break
            }
            karaokeLock.lock(); karaoke.apply(type: event.type, text: event.text); karaokeLock.unlock()
            recorder?.note(event)
            DispatchQueue.main.async {
                if let cue = self.thinking.event(event.type, value: event.value, now: ProcessInfo.processInfo.systemUptime) {
                    self.cue(cue, listening: false)
                }
                self.onEvent?(event)
            }
        @unknown default:
            break
        }
    }

    /// Interrupt (server or tap): stop the player now (a stopped node drops every scheduled
    /// buffer; pausing would keep them), reset the karaoke paragraph, and refuse audio until the
    /// next reply starts.
    private func cutPlayback() {
        freezeKaraoke()
        audio.flush()
        playedLock.lock(); played.flush(); playedLock.unlock()   // a cut reply is never reported as played
        playback.apply("interrupt")
        if !playback.playing { audio.pause() }
        admissionLock.lock(); admission.interrupt(); admissionLock.unlock()
    }

    /// `{"type":"played","reply_id":N}` once reply N's last buffer has actually played, so the server
    /// stops guessing when playback ended (the cut-off investigation, 2026-11-22).
    private var played = PlayedTracker()
    private let playedLock = NSLock()

    private func reportPlayed() {
        playedLock.lock(); let due = played.due(played: audio.playedSamples); playedLock.unlock()
        for id in due { socket?.send(.string(#"{"type":"played","reply_id":\#(id)}"#)) { _ in } }
    }

    /// Which incoming audio belongs to the reply being played (user bug, 2026-11-22: after an
    /// interrupt the old answer's audio kept playing over the new one).
    private var admission = AudioAdmission()
    private let admissionLock = NSLock()
    private var droppedAudioBytes = 0

    /// Reply audio accepted for playback and dropped as stale, in bytes (for tests and logs).
    var audioCounts: (accepted: Int, dropped: Int) {
        statsLock.lock(); defer { statsLock.unlock() }
        return (receivedAudioBytes, droppedAudioBytes)
    }

    private var gate = EarconGate()
    /// Main thread only.
    private var thinking = ThinkingCues()
    private var pulseTimer: Timer?
    /// Which part of the current reply has been heard; fed on the receive queue, read by the UI.
    private var karaoke = KaraokeTimeline()
    private let karaokeLock = NSLock()

    /// Sentences of the current reply paragraph, characters heard (smooth and per-chunk), and
    /// the sentence being spoken.
    func karaokeState() -> (texts: [String], spoken: Double, stepped: Double, current: Int?) {
        let played = audio.playedSamples
        karaokeLock.lock(); defer { karaokeLock.unlock() }
        return (karaoke.texts, karaoke.spoken(played: played, stepped: false), karaoke.spoken(played: played, stepped: true),
                karaoke.currentSegment(played: played))
    }
    /// Playback started or drained (main queue): the UI keeps 말하는 중 until the queue is empty.
    var onPlayback: ((Bool) -> Void)?
    /// A cue actually played (main queue): the UI shows its visual twin (§14.3).
    var onCuePlayed: ((Cue) -> Void)?
    private var wasPlaying = false

    /// What the call UI shows: the server may report listening/turn_done before the reply's last
    /// audio has played, so playing audio wins over those.
    static func displayState(server: String, playing: Bool) -> String {
        playing && (server == "listening" || server.isEmpty) ? "speaking" : server
    }

    /// Plays a work sound if the gate allows; cues that would land on the user's voice wait up to
    /// 300 ms, then drop. Main thread.
    func cue(_ cue: Cue, listening: Bool, waited: Double = 0) {
        let now = ProcessInfo.processInfo.systemUptime
        switch gate.decide(cue, now: now, listening: listening, userLoudAt: audio.levels.micLoudAt, waited: waited, enabled: Earcon.enabled) {
        case .play:
            audio.playCue(cue)
            onCuePlayed?(cue)
        case .drop: break
        case .wait:
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.cue(cue, listening: listening, waited: waited + 0.05) }
        }
    }

    /// Before a flush resets the playback clock, keep what was heard.
    private func freezeKaraoke() {
        let played = audio.playedSamples
        karaokeLock.lock(); karaoke.flush(heard: played); karaokeLock.unlock()
    }

    /// Push-to-talk key-up: ~700 ms of zero PCM so the server VAD closes the turn (DESIGN §12.5).
    func sendSilence(milliseconds: Int = 700) {
        socket?.send(.data(Self.silence(milliseconds: milliseconds))) { _ in }
    }

    /// Zero PCM16 at 16 kHz mono.
    static func silence(milliseconds: Int) -> Data { Data(count: 16 * milliseconds * 2) }

    /// Tap-to-interrupt (DESIGN §12.3): tell the server and drop local playback at once, without
    /// waiting for its `interrupt` echo.
    func interrupt() {
        socket?.send(.string(#"{"type":"interrupt"}"#)) { _ in }
        cutPlayback()
        recorder?.interrupt()
    }

    /// Starts writing both sides to Documents/Recordings. Only on the user's tap.
    func startRecording(session: String) throws {
        guard recorder == nil else { return }
        recorder = try CallRecorder(url: Recordings.directory.appendingPathComponent(Recordings.fileName(session: session, start: Date())))
    }

    func stopRecording() {
        let r = recorder
        recorder = nil
        r?.finish()
    }

    private var routeGate = RouteRestartGate()

    /// The route changed under the engine: restart it once (a restart can itself post a change).
    private func routeChanged() {
        guard socket != nil, routeGate.shouldRestart(now: ProcessInfo.processInfo.systemUptime) else { return }
        do { try restartAudio() } catch { onWarning?("오디오 장치를 바꾸지 못했어요") }
    }

    /// iOS: CallKit gave the audio session back (after a hold or interruption); macOS: device change.
    func restartAudio() throws {
        routeGate.restarted(now: ProcessInfo.processInfo.systemUptime)
        audio.stop()
        do { try audio.start() } catch { throw StartError.audio }
        if held { audio.pause() }
    }

    /// Says goodbye, closes the socket and stops audio. Safe to call twice.
    func stop() {
        _ = beginCall()            // the old socket's late frames are dropped from here on
        silenceCheck?.cancel()
        pulseTimer?.invalidate()
        pulseTimer = nil
        thinking = ThinkingCues()
        karaokeLock.lock(); karaoke = KaraokeTimeline(); karaokeLock.unlock()
        admissionLock.lock(); admission = AudioAdmission(); admissionLock.unlock()
        playedLock.lock(); played = PlayedTracker(); playedLock.unlock()
        if wasPlaying { wasPlaying = false; onPlayback?(false) }
        if socket != nil {
            statsLock.lock(); let bytes = sentBytes, got = receivedAudioBytes; statsLock.unlock()
            Log.call.info("call stop: sent \(bytes, privacy: .public) B, received audio \(got, privacy: .public) B")
        }
        stopRecording()
        if let task = socket {
            task.send(.string(#"{"type":"hangup"}"#)) { _ in task.cancel(with: .normalClosure, reason: nil) }
        }
        socket = nil
        audio.onMic = nil
        audio.stop()
        muted = false
        held = false
        playback = PlaybackGate()
        sending = true
    }

    /// Audio the user already said (the wake listener's recording) sent as mic frames, 100 ms each,
    /// ahead of the live mic. The caller keeps `sending` off until this returns.
    func sendUserAudio(_ pcm16: Data) {
        guard let task = socket else { return }
        let chunk = 3_200
        var i = pcm16.startIndex
        while i < pcm16.endIndex {
            let end = min(i + chunk, pcm16.endIndex)
            task.send(.data(pcm16.subdata(in: i..<end))) { _ in }
            i = end
        }
        statsLock.lock(); sentBytes += pcm16.count; sentPeak = max(sentPeak, Self.peak(pcm16)); statsLock.unlock()
    }

    /// Mid-call brain switch (`{"type":"mode","value":…}`); the server confirms with a mode event.
    func switchMode(_ mode: String) {
        socket?.send(.string(#"{"type":"mode","value":"\#(mode)"}"#)) { _ in }
    }

    /// The mode an event reports: a `mode` event's value, or a `mode` field on any event.
    static func reportedMode(_ e: CallEvent) -> String? { e.type == "mode" ? (e.value ?? e.mode) : e.mode }

    /// iOS `didDeactivate`: the system took the audio session away.
    func stopAudio() { audio.stop() }
}

/// Who may stop playback: the user (hold) and the server (`pause` on a tentative barge-in).
/// `pause` keeps the queued audio; `resume` continues it; `interrupt` drops it and clears pause.
struct PlaybackGate: Equatable {
    var held = false
    private(set) var serverPaused = false

    var playing: Bool { !held && !serverPaused }

    mutating func apply(_ eventType: String) {
        switch eventType {
        case "pause": serverPaused = true
        case "resume", "interrupt": serverPaused = false
        default: break
        }
    }
}

/// Audio frames are accepted unless an interrupt happened; then nothing is accepted until the next
/// `reply_start` (or, from a server that doesn't send it, the next `state: speaking`).
struct AudioAdmission: Equatable {
    private(set) var admits = true
    private(set) var replyID: Int?

    mutating func interrupt() { admits = false }

    /// `reply_start`: true when it starts a different reply than the last one (no id counts as new).
    mutating func replyStart(_ id: Int?) -> Bool {
        defer { replyID = id }
        return id == nil || id != replyID
    }

    mutating func apply(type: String, value: String?) {
        switch type {
        case "interrupt": admits = false
        case "reply_start": admits = true
        case "state" where value == "speaking": admits = true
        default: break
        }
    }
}

/// A route change restarts the engine at most once per half second: restarting can post another
/// configuration change, which must not loop.
struct RouteRestartGate {
    static let quiet = 0.5
    private var last: Double?

    mutating func restarted(now: Double) { last = now }

    mutating func shouldRestart(now: Double) -> Bool {
        if let last, now - last < Self.quiet { return false }
        last = now
        return true
    }
}

/// Where each reply ends on the playback clock (samples scheduled since the last flush, the same
/// clock as `AudioIO.playedSamples`), to tell the server when one has really finished playing.
struct PlayedTracker: Equatable {
    private struct Reply: Equatable { var id: Int; var begin: Int; var end: Int; var closed = false }
    private var replies: [Reply] = []
    private var scheduled = 0
    private var lastAudio = 0.0
    /// The server sends `turn_done` only after `played` (gateway 9e2ecc8), so the latest reply is
    /// reported once everything queued has played and no audio came for this long (a short
    /// streaming stall must not end the turn).
    static let settle = 0.25

    /// `reply_start`: closes the reply before it (no more audio comes for it).
    mutating func start(_ id: Int) {
        close()
        replies.append(Reply(id: id, begin: scheduled, end: scheduled))
    }

    mutating func audio(samples: Int, now: Double = ProcessInfo.processInfo.systemUptime) {
        scheduled += samples
        lastAudio = now
        if let i = replies.indices.last, !replies[i].closed { replies[i].end = scheduled }
    }

    /// `turn_done`: the open reply is complete.
    mutating func close() { if let i = replies.indices.last { replies[i].closed = true } }

    /// Interrupt or hangup: the clock restarts and nothing cut is reported.
    mutating func flush() { replies = []; scheduled = 0 }

    /// Replies whose last sample has played, oldest first: closed ones (a later reply started or
    /// turn_done) as soon as they played; the open one once the queue ran dry and stayed dry
    /// `settle` seconds.
    mutating func due(played: Int, now: Double = ProcessInfo.processInfo.systemUptime) -> [Int] {
        var out: [Int] = []
        if let i = replies.indices.last, !replies[i].closed, replies[i].end > replies[i].begin,
           played >= replies[i].end, played >= scheduled, now - lastAudio >= Self.settle {
            replies[i].closed = true
        }
        while let r = replies.first, r.closed {
            if r.end > r.begin {
                guard played >= r.end else { break }
                out.append(r.id)
            }
            replies.removeFirst()
        }
        return out
    }
}
