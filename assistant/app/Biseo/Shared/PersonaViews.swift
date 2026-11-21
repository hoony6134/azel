import AVFoundation
import SwiftUI

/// Small name + colour dot for a persona (history rows, schedule, call screen).
struct PersonaChip: View {
    let persona: Persona
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(persona.tint).frame(width: 8, height: 8)
            Text(persona.name).font(.footnote.weight(.semibold)).foregroundStyle(Theme.text2).lineLimit(1)
        }
        .padding(.horizontal, Theme.s).padding(.vertical, 2)
        .overlay(Capsule().strokeBorder(Theme.stroke))
        .accessibilityElement(children: .combine)
    }
}

/// Who the next call goes to (home, Mac popover). Shown once there is more than one persona;
/// with one, a quiet "비서 추가".
struct PersonaSwitcher: View {
    @ObservedObject var model: PersonaModel
    @State private var managing = false

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Theme.s) {
                if model.personas.count > 1 {
                    ForEach(model.personas) { p in
                        let on = model.selected?.id == p.id
                        Button { model.selectedID = p.id } label: {
                            HStack(spacing: 6) {
                                Circle().fill(p.tint).frame(width: 10, height: 10)
                                Text(p.name).font(.subheadline.weight(on ? .semibold : .regular))
                                    .foregroundStyle(on ? Theme.text : Theme.text2)
                            }
                            .padding(.horizontal, Theme.m).frame(minHeight: 36)
                            .background(on ? p.tint.opacity(0.18) : .clear, in: Capsule())
                            .overlay(Capsule().strokeBorder(on ? p.tint : Theme.stroke))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(p.name + (p.primary ? ", 기본 비서" : ""))
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                Button { managing = true } label: {
                    Label(model.personas.count > 1 ? "관리" : "비서 추가", systemImage: model.personas.count > 1 ? "slider.horizontal.3" : "plus")
                        .font(.subheadline).foregroundStyle(Theme.text2)
                        .padding(.horizontal, Theme.m).frame(minHeight: 36)
                        .overlay(Capsule().strokeBorder(Theme.stroke))
                }
                .buttonStyle(.plain)
            }
        }
        .sheet(isPresented: $managing) {
            NavigationStack { PersonaListView(model: model) }
                #if os(macOS)
                .frame(width: 520, height: 620)
                #endif
        }
    }
}

/// All personas: open to edit, make primary, delete, add (up to 8).
struct PersonaListView: View {
    @ObservedObject var model: PersonaModel
    @Environment(\.dismiss) private var dismiss
    @State private var editing: Persona?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.m) {
                SectionNote(status: model.status) { Task { await model.refresh() } }
                if let error { Text(error).font(.footnote).foregroundStyle(Theme.danger) }
                ForEach(model.personas) { p in
                    Button { editing = p } label: {
                        Card {
                            HStack(spacing: Theme.m) {
                                Circle().fill(p.tint).frame(width: 14, height: 14)
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack {
                                        Text(p.name).font(.headline).foregroundStyle(Theme.text)
                                        if p.primary { Text("기본").font(.footnote.weight(.semibold)).foregroundStyle(Theme.celadon) }
                                    }
                                    Text([p.voice, p.share_memory ? "기억 공유" : "기억 따로"].compactMap { $0 }.joined(separator: " · "))
                                        .font(.footnote).foregroundStyle(Theme.text3)
                                }
                                Spacer()
                                Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        if !p.primary { Button("기본 비서로") { act { try await model.makePrimary(p) } } }
                        if !p.primary { Button("삭제", role: .destructive) { act { try await model.delete(p) } } }
                    }
                }
                if model.personas.count < Persona.limit {
                    Button { editing = Persona(name: "", color: Persona.palette[model.personas.count % Persona.palette.count]) } label: {
                        Label("새 비서", systemImage: "plus").frame(maxWidth: .infinity).frame(height: 44)
                    }
                    .buttonStyle(.bordered).tint(Theme.celadon)
                } else {
                    Text("비서는 \(Persona.limit)명까지 만들 수 있어요").font(.footnote).foregroundStyle(Theme.text3)
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("비서")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        .task { await model.refresh() }
        .sheet(item: $editing) { p in
            NavigationStack { PersonaEditor(model: model, persona: p) }
                #if os(macOS)
                .frame(width: 520, height: 680)
                #endif
        }
    }

    private func act(_ op: @escaping () async throws -> Void) {
        Task { do { try await op(); error = nil } catch { self.error = FriendlyError.describe(error).title } }
    }
}

/// Create or edit one persona, with a voice sample.
struct PersonaEditor: View {
    @ObservedObject var model: PersonaModel
    @State var persona: Persona
    @Environment(\.dismiss) private var dismiss
    @State private var message: String?
    @State private var saving = false
    @State private var sampling = false
    @State private var player: AVAudioPlayer?

    private static let models = [("", "기본"), ("qwen", "로컬 Qwen"), ("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("auto", "자동")]

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.l) {
                Card("이름") {
                    TextField(AssistantName.fallback, text: $persona.name).textFieldStyle(.roundedBorder)
                    HStack(spacing: Theme.s) {
                        ForEach(Persona.palette, id: \.self) { hex in
                            Button { persona.color = hex } label: {
                                Circle().fill(Persona.color(hex: hex)!).frame(width: 26, height: 26)
                                    .overlay(Circle().strokeBorder(Theme.text, lineWidth: persona.color == hex ? 2 : 0))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("색 \(hex)")
                            .accessibilityAddTraits(persona.color == hex ? .isSelected : [])
                        }
                    }
                }
                Card("목소리") {
                    TextField("목소리 (예: sohee)", text: optional($persona.voice)).textFieldStyle(.roundedBorder).urlInput()
                    TextField("말투 (예: 차분하게, 밝게)", text: optional($persona.voice_style)).textFieldStyle(.roundedBorder)
                    Button { Task { await sample() } } label: {
                        Label(sampling ? "만드는 중…" : "목소리 들어보기", systemImage: "play.circle")
                    }
                    .disabled(sampling).tint(Theme.celadon)
                }
                Card("성격") {
                    TextField("기본 성격", text: Binding(get: { persona.personality ?? "" },
                                                     set: { persona.personality = String($0.prefix(AssistantName.personalityLimit)) }),
                              axis: .vertical)
                        .lineLimit(3...8).textFieldStyle(.roundedBorder)
                    Text("\((persona.personality ?? "").count)/\(AssistantName.personalityLimit)")
                        .font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                }
                Card("동작") {
                    HStack {
                        Text("기본 모델").foregroundStyle(Theme.text)
                        Spacer()
                        Picker("", selection: Binding(get: { persona.default_model ?? "" }, set: { persona.default_model = $0.isEmpty ? nil : $0 })) {
                            ForEach(Self.models, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        .pickerStyle(.menu).labelsHidden().tint(Theme.celadon).fixedSize()
                    }
                    Toggle("다른 비서와 기억 공유", isOn: $persona.share_memory).tint(Theme.celadon).foregroundStyle(Theme.text)
                }
                if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
                if !persona.id.isEmpty && !persona.primary {
                    Button("이 비서 삭제", role: .destructive) {
                        Task { do { try await model.delete(persona); dismiss() } catch { message = FriendlyError.describe(error).title } }
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle(persona.id.isEmpty ? "새 비서" : persona.name)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("취소") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) { Button(saving ? "저장 중…" : "저장") { Task { await save() } }.disabled(saving) }
        }
    }

    private func optional(_ b: Binding<String?>) -> Binding<String> {
        Binding(get: { b.wrappedValue ?? "" }, set: { b.wrappedValue = $0.isEmpty ? nil : $0 })
    }

    private func save() async {
        guard let name = AssistantName.validated(persona.name) else { message = "이름은 한글, 영문, 공백으로 12자까지예요"; return }
        persona.name = name
        persona.personality = persona.personality.map(AssistantName.personality)
        saving = true
        defer { saving = false }
        do { try await model.save(persona); dismiss() } catch { message = "저장하지 못했어요: " + FriendlyError.describe(error).title }
    }

    private func sample() async {
        sampling = true
        defer { sampling = false }
        let name = AssistantName.validated(persona.name) ?? AssistantName.fallback
        let voice = persona.voice, style = persona.voice_style
        switch await attempt({ try await PersonaAPI.voiceSample(voice: voice, style: style, text: "안녕하세요, \(AssistantName.josa(name, "이에요", "예요")). 무엇을 도와드릴까요?") }) {
        case .success(let wav):
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            #endif
            player = try? AVAudioPlayer(data: wav)
            player?.play()
            message = player == nil ? "샘플을 재생할 수 없어요" : nil
        case .failure(let e):
            guard let s = SectionStatus.from(e) else { return }
            message = s == .unavailable ? "목소리 샘플은 서버 업데이트 후 들을 수 있어요" : "샘플을 만들지 못했어요"
        }
    }
}
