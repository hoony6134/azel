import AVFoundation
import SwiftUI

/// Persona dot + name (history meta, schedule rows, detail header).
struct PersonaChip: View {
    let name: String
    let tint: Color
    var outlined = true

    init(persona: Persona) { name = persona.name; tint = persona.tint }
    init(name: String, tint: Color, outlined: Bool = true) { self.name = name; self.tint = tint; self.outlined = outlined }

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(tint).frame(width: 8, height: 8)
            Text(name).font(.footnote.weight(.semibold)).foregroundStyle(tint).lineLimit(1)
        }
        .padding(.horizontal, outlined ? Theme.s : 0).padding(.vertical, outlined ? 2 : 0)
        .background(outlined ? tint.opacity(0.12) : .clear, in: Capsule())
        .overlay(Capsule().strokeBorder(outlined ? tint.opacity(0.6) : .clear, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

/// §17.3 / §17.8: a row of breathing mini orbs, the selected one ringed. Hidden with one persona.
struct MiniOrbRow: View {
    @ObservedObject var model: PersonaModel
    var size: CGFloat = 44
    var showNames = true
    @State private var editing: Persona?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if model.personas.count > 1 {
            // Centered when it fits, scrolls when it doesn't; vertical room for the selection ring
            // (it was clipped on iPhone, user screenshot 2026-11-21).
            ViewThatFits(in: .horizontal) {
                row
                ScrollView(.horizontal, showsIndicators: false) { row }
            }
            .sheet(item: $editing) { p in PersonaEditorSheet(model: model, persona: p) }
        }
    }

    private var row: some View {
                HStack(spacing: Theme.m) {
                    ForEach(Array(model.ordered.enumerated()), id: \.element.id) { index, p in
                        orb(p, index: index)
                    }
                    if model.personas.count < Persona.limit && showNames {
                        Button { editing = Persona(name: "", color: nextTint.rawValue) } label: {
                            VStack(spacing: 4) {
                                Circle().strokeBorder(Theme.text2, style: StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                                    .frame(width: size, height: size)
                                    .overlay(Image(systemName: "plus").foregroundStyle(Theme.text2))
                                Text(" ").font(.footnote)
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("비서 추가")
                    }
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 6)
    }

    private var nextTint: PersonaTint {
        PersonaTint.allCases.first { t in !model.personas.contains { $0.tintKey == t } } ?? .celadon
    }

    private func orb(_ p: Persona, index: Int) -> some View {
        let selected = model.selected?.id == p.id
        return Button {
            model.selectedID = p.id
            AccessibilityNotification.Announcement("\(AssistantName.josa(p.name, "으로", "로")) 바꿨어요").post()
        } label: {
            VStack(spacing: 4) {
                MoonJar(state: .idle, levels: .silent, paused: reduceMotion, tint: p.glaze, breathAmplitude: 0.5)
                    .frame(width: size, height: size)
                    .overlay(Circle().strokeBorder(selected ? p.tint : .clear, lineWidth: 2).padding(-3))
                    .contentShape(Circle())   // the orb's canvas ignores hits; clicks on the orb itself must select
                if showNames {
                    Text(p.name).font(.footnote.weight(selected ? .semibold : .regular))
                        .foregroundStyle(selected ? Theme.text : Theme.text2).lineLimit(1)
                }
            }
        }
        .buttonStyle(.plain)
        #if os(iOS)
        .sensoryFeedback(.selection, trigger: model.selectedID)
        #endif
        #if os(macOS)
        .help(p.name)
        .keyboardShortcut(KeyEquivalent(Character("\(min(index + 1, 9))")), modifiers: .command) // ⌘1…⌘8 (8 personas max)
        #endif
        .contextMenu {
            Button("편집") { editing = p }
            if !p.primary { Button("기본 비서로 지정") { Task { try? await model.makePrimary(p) } } }
        }
        .accessibilityLabel(p.name)
        .accessibilityValue(selected ? "선택됨" : "")
        .accessibilityHint("두 번 탭하면 이 비서로 바꿉니다")
        .accessibilityAction(named: "편집") { editing = p }
    }
}

/// §17.4: the 비서 card in 설정.
struct PersonaListCard: View {
    @ObservedObject var model: PersonaModel
    @State private var editing: Persona?
    @State private var voices: [Voice] = []

    var body: some View {
        Card("비서") {
            SectionNote(status: model.status) { Task { await model.refresh() } }
            ForEach(Array(model.ordered.enumerated()), id: \.element.id) { i, p in
                Button { editing = p } label: {
                    HStack(spacing: Theme.m) {
                        MoonJar(state: .idle, levels: .silent, paused: true, tint: p.glaze).frame(width: 24, height: 24)
                        Text(p.name).foregroundStyle(Theme.text)
                        if p.primary {
                            Text("기본").font(.footnote.weight(.semibold)).foregroundStyle(p.tint)
                                .padding(.horizontal, 6).padding(.vertical, 1).overlay(Capsule().strokeBorder(p.tint.opacity(0.6)))
                        }
                        Spacer()
                        Text(voices.first { $0.id == p.voice }?.label ?? p.voice ?? "").font(.subheadline).foregroundStyle(Theme.text2)
                        Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .contextMenu {
                    if i > 0 { Button("위로") { model.move(p, by: -1) } }
                    if i < model.personas.count - 1 { Button("아래로") { model.move(p, by: 1) } }
                }
                .accessibilityAction(named: "위로") { model.move(p, by: -1) }
                .accessibilityAction(named: "아래로") { model.move(p, by: 1) }
            }
            if model.personas.count <= 1 {
                Text("성격과 목소리가 다른 비서를 8명까지 만들 수 있어요.").font(.footnote).foregroundStyle(Theme.text3)
            }
            let full = model.personas.count >= Persona.limit
            Button { editing = Persona(name: "", color: (PersonaTint.allCases.first { t in !model.personas.contains { $0.tintKey == t } } ?? .celadon).rawValue) } label: {
                Label("비서 추가", systemImage: "plus").foregroundStyle(full ? Theme.text3 : Theme.celadon)
            }
            .buttonStyle(.plain)
            .disabled(full || model.status == .unavailable)
            if full { Text("비서는 8명까지예요. 하나를 지우면 새로 만들 수 있어요.").font(.footnote).foregroundStyle(Theme.text3) }
        }
        .task {
            await model.refresh()
            if case .success(let v) = await attempt({ try await PersonaAPI.voices() }) { voices = v }
        }
        .sheet(item: $editing) { p in PersonaEditorSheet(model: model, persona: p) }
    }
}

/// §17.5 wrapped for both platforms.
struct PersonaEditorSheet: View {
    @ObservedObject var model: PersonaModel
    let persona: Persona

    var body: some View {
        NavigationStack { PersonaEditor(model: model, original: persona) }
            #if os(iOS)
            .presentationDetents([.large])
            #else
            .frame(width: 540, height: 720)
            #endif
    }
}

/// §17.5: preview, 이름, 색, 목소리 (+들어보기), 말투, 성격, 기억 공유, 기본 비서, 지우기.
struct PersonaEditor: View {
    @ObservedObject var model: PersonaModel
    let original: Persona
    @State private var p: Persona
    @Environment(\.dismiss) private var dismiss
    @State private var voices: [Voice] = []
    @State private var voicesStatus: SectionStatus = .ok
    @State private var message: String?
    @State private var saving = false
    @State private var confirmDiscard = false
    @State private var confirmPrimary = false
    @State private var confirmDelete = false
    @StateObject private var sampler = VoiceSampler()

    init(model: PersonaModel, original: Persona) {
        self.model = model
        self.original = original
        _p = State(initialValue: original)
    }

    private var trimmedName: String { p.name.trimmingCharacters(in: .whitespaces) }
    private var nameProblem: String? {
        if trimmedName.isEmpty { return "이름을 입력해 주세요" }
        if AssistantName.validated(trimmedName) == nil { return "한글, 영문, 공백으로 12자까지예요" }
        if model.personas.contains(where: { $0.id != p.id && $0.name == trimmedName }) { return "이미 있는 이름이에요" }
        return nil
    }
    private var tooLong: Bool { (p.personality ?? "").count > Persona.personalityLimit }
    private var valid: Bool { nameProblem == nil && !tooLong }
    private var changed: Bool { p != original }

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.l) {
                // 1. preview
                VStack(spacing: Theme.s) {
                    MoonJar(state: .idle, levels: .silent, tint: p.glaze).frame(width: 96, height: 96)
                    Text(trimmedName.isEmpty ? " " : trimmedName).font(.headline).foregroundStyle(Theme.text)
                }
                .animation(Theme.state, value: p.color)
                // 2. 이름
                Card("이름") {
                    TextField("이름", text: $p.name).textFieldStyle(.roundedBorder)
                    if let nameProblem, !p.name.isEmpty || p != original { Text(nameProblem).font(.footnote).foregroundStyle(Theme.danger) }
                }
                // 3. 색
                Card("색") {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 4), spacing: Theme.m) {
                        ForEach(PersonaTint.allCases) { t in
                            let on = p.tintKey == t
                            Button { p.color = t.rawValue } label: {
                                Circle().fill(t.glaze).frame(width: 44, height: 44)
                                    .overlay(Circle().strokeBorder(on ? t.accent : .clear, lineWidth: 2).padding(-3))
                                    .overlay(on ? Image(systemName: "checkmark").font(.headline).foregroundStyle(Theme.onAccent) : nil)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(t.name)
                            .accessibilityValue(on ? "선택됨" : "")
                        }
                    }
                }
                // 4. 목소리
                Card("목소리") {
                    SectionNote(status: voicesStatus) { Task { await loadVoices() } }
                    ForEach(voices) { v in
                        HStack(spacing: Theme.m) {
                            Button { p.voice = v.id } label: {
                                HStack(spacing: Theme.m) {
                                    Image(systemName: p.voice == v.id ? "largecircle.fill.circle" : "circle")
                                        .foregroundStyle(p.voice == v.id ? p.tint : Theme.text3)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(v.label).foregroundStyle(Theme.text)
                                        if let d = v.desc { Text(d).font(.footnote).foregroundStyle(Theme.text3) }
                                    }
                                    Spacer()
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            sampleButton(v)
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel(v.label)
                        .accessibilityAction(named: "들어보기") { sampler.toggle(voice: v.id, style: p.voice_style, name: trimmedName) }
                    }
                    if let m = sampler.message { Text(m).font(.footnote).foregroundStyle(Theme.amber) }
                }
                // 5. 말투
                Card("말투") {
                    TextField("예: 차분하고 천천히, 밝고 빠르게", text: Binding(get: { p.voice_style ?? "" },
                                                                   set: { p.voice_style = $0.isEmpty ? nil : String($0.prefix(80)) }))
                        .textFieldStyle(.roundedBorder)
                    Text("목소리에 주는 짧은 지시예요.").font(.footnote).foregroundStyle(Theme.text3)
                }
                // 6. 성격
                Card("성격") {
                    ZStack(alignment: .topLeading) {
                        TextEditor(text: Binding(get: { p.personality ?? "" }, set: { p.personality = $0.isEmpty ? nil : $0 }))
                            .frame(minHeight: 96, maxHeight: 190).scrollContentBackground(.hidden)
                            .padding(4).background(Theme.bg, in: RoundedRectangle(cornerRadius: Theme.rowRadius))
                        if (p.personality ?? "").isEmpty {
                            Text("예: 꼼꼼하고 다정하게, 결론부터 말해 줘").foregroundStyle(Theme.text3).padding(10).allowsHitTesting(false)
                        }
                    }
                    Text("\((p.personality ?? "").count)/\(Persona.personalityLimit)").font(.footnote.monospacedDigit())
                        .foregroundStyle(tooLong ? Theme.danger : Theme.text3)
                }
                // 7. 기억 공유
                Card {
                    Toggle("기억 공유", isOn: $p.share_memory).tint(p.tint).foregroundStyle(Theme.text)
                    Text(p.share_memory ? "다른 비서와 같은 기억을 써요." : "이 비서만의 기억을 따로 가져요.").font(.footnote).foregroundStyle(Theme.text3)
                }
                // 8. 기본 비서
                if !p.id.isEmpty {
                    Card {
                        if p.primary {
                            Text("기본 비서예요").foregroundStyle(Theme.text2)
                        } else {
                            Button("기본 비서로 지정") { confirmPrimary = true }.tint(p.tint)
                        }
                    }
                }
                if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
                // 9. 지우기
                if !p.id.isEmpty && !p.primary {
                    Button("비서 지우기", role: .destructive) { confirmDelete = true }.padding(.top, Theme.s)
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle(p.id.isEmpty ? "새 비서" : original.name)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("취소") { changed ? (confirmDiscard = true) : dismiss() } }
            ToolbarItem(placement: .confirmationAction) { Button(saving ? "저장 중…" : "저장") { Task { await save() } }.disabled(!valid || saving) }
        }
        .interactiveDismissDisabled(changed)
        .confirmationDialog("변경 사항을 버릴까요?", isPresented: $confirmDiscard, titleVisibility: .visible) {
            Button("버리기", role: .destructive) { dismiss() }
            Button("계속 편집", role: .cancel) {}
        }
        .confirmationDialog("\(AssistantName.josa(p.name, "을", "를")) 기본 비서로 할까요?", isPresented: $confirmPrimary, titleVisibility: .visible) {
            Button("기본 비서로 지정") { Task { do { try await model.makePrimary(p); p.primary = true } catch { message = FriendlyError.describe(error).title } } }
            Button("취소", role: .cancel) {}
        } message: {
            Text("아침 브리핑과 먼저 거는 전화를 \(AssistantName.josa(p.name, "이", "가")) 해요. 호출어는 ‘\(AssistantName.fallback)’ 그대로예요.")
        }
        .sheet(isPresented: $confirmDelete) {
            PersonaDeleteSheet(persona: original) { deleteHistory in
                Task {
                    do { try await model.delete(original, deleteHistory: deleteHistory); confirmDelete = false; dismiss() }
                    catch { message = "지우지 못했어요: " + FriendlyError.describe(error).title; confirmDelete = false }
                }
            }
        }
        .task { await loadVoices() }
        .onDisappear { sampler.stop() }
    }

    private func sampleButton(_ v: Voice) -> some View {
        Button { sampler.toggle(voice: v.id, style: p.voice_style, name: trimmedName) } label: {
            ZStack {
                if sampler.loading == v.id { ProgressView().controlSize(.small) }
                else { Image(systemName: sampler.playing == v.id ? "stop.circle" : "play.circle").font(.title2).foregroundStyle(p.tint) }
            }
            .frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .disabled(sampler.coolingDown && sampler.playing != v.id)
        .accessibilityLabel(sampler.playing == v.id ? "멈춤" : "들어보기")
    }

    private func loadVoices() async {
        switch await attempt({ try await PersonaAPI.voices() }) {
        case .success(let v): voices = v; voicesStatus = .ok; if p.voice == nil { p.voice = v.first?.id }
        case .failure(let e): voicesStatus = SectionStatus.from(e) ?? voicesStatus
        }
    }

    private func save() async {
        p.name = trimmedName
        saving = true
        defer { saving = false }
        do { try await model.save(p); dismiss() } catch { message = "저장하지 못했어요: " + FriendlyError.describe(error).title }
    }
}

/// §17.5 delete confirm with the "통화 기록도 함께 삭제" checkbox (default off).
struct PersonaDeleteSheet: View {
    let persona: Persona
    let onDelete: (Bool) -> Void
    @State private var deleteHistory = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.l) {
            Text("‘\(persona.name)’\(WakeEnrollModel.particle(persona.name, "을", "를")) 지울까요?").font(.headline).foregroundStyle(Theme.text)
            Text("이 비서의 메모와 통화 요약이 지워져요. " + (deleteHistory ? "통화 기록도 모두 지워져요." : "통화 기록은 ‘삭제된 비서’로 남아요."))
                .foregroundStyle(Theme.text2)
            Toggle("통화 기록도 함께 삭제", isOn: $deleteHistory)
                #if os(macOS)
                .toggleStyle(.checkbox)
                #endif
                .tint(Theme.danger)
            HStack {
                Button("취소") { dismiss() }.buttonStyle(.bordered)
                Spacer()
                Button("지우기", role: .destructive) { onDelete(deleteHistory) }.buttonStyle(.borderedProminent).tint(Theme.danger)
            }
        }
        .padding(24)
        .background(Theme.bgRaised.ignoresSafeArea())
        #if os(iOS)
        .presentationDetents([.height(280)])
        #else
        .frame(width: 420)
        #endif
    }
}

/// 들어보기: one sample at a time, 2 s cooldown after each (the server's rate limit), 429 handled.
/// Uses the preview `.playback` session and keeps the player alive while it plays.
@MainActor
final class VoiceSampler: ObservableObject {
    @Published private(set) var loading: String?
    @Published private(set) var playing: String?
    @Published private(set) var coolingDown = false
    @Published var message: String?
    private var player: AVAudioPlayer?

    /// "안녕하세요, {이름}이에요. 무엇을 도와드릴까요?" kept under 60 characters.
    static func sampleText(name: String) -> String {
        let n = name.isEmpty ? AssistantName.fallback : name
        return String("안녕하세요, \(AssistantName.josa(n, "이에요", "예요")). 무엇을 도와드릴까요?".prefix(60))
    }

    func toggle(voice: String, style: String?, name: String) {
        if playing == voice { stop(); return }
        guard !coolingDown, loading == nil else { return }
        stop()
        loading = voice
        message = nil
        Task {
            let text = Self.sampleText(name: name)
            let result = await attempt { try await PersonaAPI.voiceSample(voice: voice, style: style, text: text) }
            loading = nil
            cooldown()
            switch result {
            case .success(let wav):
                PreviewAudio.activate()
                player = try? AVAudioPlayer(data: wav)
                player?.prepareToPlay()
                if player?.play() == true {
                    playing = voice
                    let length = player?.duration ?? 3
                    Task { try? await Task.sleep(nanoseconds: UInt64(length * 1e9)); if playing == voice { playing = nil } }
                } else {
                    Log.call.error("voice sample could not play (\(wav.count, privacy: .public) bytes)")
                    message = "샘플을 재생할 수 없어요"
                }
            case .failure(let e):
                if case GatewayError.http(429) = e { message = "잠시 후 다시 들어 보세요" }
                else if let s = SectionStatus.from(e) { message = s == .unavailable ? "목소리 샘플은 서버 업데이트 후 들을 수 있어요" : "샘플을 만들지 못했어요" }
            }
        }
    }

    func stop() { player?.stop(); player = nil; playing = nil }

    private func cooldown() {
        coolingDown = true
        Task { try? await Task.sleep(nanoseconds: 2_000_000_000); coolingDown = false }
    }
}
