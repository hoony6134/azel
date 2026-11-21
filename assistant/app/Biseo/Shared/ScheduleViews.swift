import SwiftUI

/// Status badge: 예정 / 거는 중 / 받음 / 놓침 / 취소됨, word plus colour (never colour alone).
struct ScheduleBadge: View {
    let status: String?

    var body: some View {
        let color: Color = switch status {
        case nil, "pending": Theme.celadon
        case "fired": Theme.amber
        case "missed": Theme.danger
        default: Theme.text3
        }
        Text(KST.statusLabel(status)).font(.footnote.weight(.semibold)).foregroundStyle(color)
            .padding(.horizontal, Theme.s).padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
            .overlay(Capsule().strokeBorder(color.opacity(0.6), lineWidth: 1))
    }
}

struct ScheduleRow: View {
    let item: ScheduledCall

    var body: some View {
        HStack(alignment: .top, spacing: Theme.m) {
            Image(systemName: item.isWake ? "alarm" : "bell").foregroundStyle(item.isWake ? Theme.amber : Theme.celadon).frame(width: 20)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: Theme.xs) {
                    Text(KST.dayAndTime(item.at)).font(.body.monospacedDigit()).foregroundStyle(Theme.text)
                    if item.byVoice {
                        Image(systemName: "mic.fill").font(.caption).foregroundStyle(Theme.text3).accessibilityLabel("음성으로 예약함")
                    }
                }
                let meta = [item.isWake ? "깨우기" : "알림", KST.repeatLabel(item.repeatRule), (item.attempts ?? 0) > 0 ? "시도 \(item.attempts!)회" : nil]
                Text(meta.compactMap { $0 }.joined(separator: " · ")).font(.footnote).foregroundStyle(Theme.text3)
                if let note = item.note, !note.isEmpty { Text(note).font(.subheadline).foregroundStyle(Theme.text2).lineLimit(2) }
            }
            Spacer()
            ScheduleBadge(status: item.status)
        }
        .accessibilityElement(children: .combine)
    }
}

/// Home "예약" card: the next call, or a prompt to add one.
struct ScheduleCard: View {
    @ObservedObject var schedule: ScheduleModel
    @State private var showList = false

    var body: some View {
        Card("예약", trailing: AnyView(Button("모두 보기") { showList = true }.font(.subheadline).tint(Theme.celadon))) {
            if let next = schedule.next {
                ScheduleRow(item: next)
            } else {
                Text("예약된 전화가 없어요").foregroundStyle(Theme.text2)
            }
        }
        .sheet(isPresented: $showList) {
            NavigationStack { ScheduleScreen(schedule: schedule) }
                #if os(macOS)
                .frame(width: 520, height: 620)
                #endif
        }
        .task { await schedule.refresh() }
    }
}

/// The full list with cancel, and the create form.
struct ScheduleScreen: View {
    @ObservedObject var schedule: ScheduleModel
    @Environment(\.dismiss) private var dismiss
    @State private var at = Date().addingTimeInterval(3600)
    @State private var kind = "wake"
    @State private var repeatChoice = "none"      // none | daily | weekdays | weekly
    @State private var weekday = 0                // 0 = Monday
    @State private var note = ""
    @State private var saving = false
    @State private var message: String?
    @State private var showFocusHint = false
    @AppStorage("focusHintShown") private var focusHintShown = false

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.l) {
                Card("새 예약") {
                    Picker("종류", selection: $kind) {
                        Text("깨우기").tag("wake")
                        Text("알림").tag("reminder")
                    }
                    .pickerStyle(.segmented)
                    DatePicker("시각", selection: $at, in: Date()...Date().addingTimeInterval(30 * 86_400), displayedComponents: [.date, .hourAndMinute])
                        .environment(\.timeZone, KST.zone)
                        .foregroundStyle(Theme.text)
                    HStack {
                        Text("반복").foregroundStyle(Theme.text)
                        Spacer()
                        Picker("", selection: $repeatChoice) {
                            Text("안 함").tag("none"); Text("매일").tag("daily"); Text("주중").tag("weekdays"); Text("매주").tag("weekly")
                        }
                        .pickerStyle(.menu).labelsHidden().tint(Theme.celadon).fixedSize()
                        .onChange(of: repeatChoice) { if repeatChoice == "weekly" { weekday = KST.dow(at) } }
                    }
                    if repeatChoice == "weekly" {
                        Picker("요일", selection: $weekday) {
                            ForEach(KST.weekdays.indices, id: \.self) { Text(KST.weekdays[$0]).tag($0) }
                        }
                        .pickerStyle(.segmented)
                    }
                    TextField(kind == "wake" ? "메모 (예: 첫 수업)" : "무엇을 알려 드릴까요?", text: $note).textFieldStyle(.roundedBorder)
                    Text("시각은 한국 시간(KST)이에요.").font(.footnote).foregroundStyle(Theme.text3)
                    if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
                    Button { Task { await save() } } label: {
                        Text(saving ? "예약하는 중…" : "예약").font(.headline).foregroundStyle(Theme.onAccent)
                            .frame(maxWidth: .infinity).frame(height: 44)
                            .background(Theme.celadon, in: RoundedRectangle(cornerRadius: Theme.rowRadius, style: .continuous))
                    }
                    .buttonStyle(.plain).disabled(saving || (kind == "reminder" && note.trimmingCharacters(in: .whitespaces).isEmpty))
                }
                Card("예약된 전화") {
                    if schedule.upcoming.isEmpty { Text("예약된 전화가 없어요").foregroundStyle(Theme.text2) }
                    ForEach(schedule.upcoming) { item in row(item) }
                }
                if !schedule.missed.isEmpty {
                    Card("놓친 전화") { ForEach(schedule.missed) { item in row(item) } }
                }
                if let e = schedule.error { Text(FriendlyError.describe(e).title).font(.footnote).foregroundStyle(Theme.danger) }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("예약 전화")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        .refreshable { await schedule.refreshAll() }
        .task { await schedule.refreshAll() }
        .alert("집중 모드에서도 울리게 하려면", isPresented: $showFocusHint) {
            Button("확인") {}
        } message: {
            Text("설정 → 집중 모드에서 허용된 앱에 Azel을 추가해 주세요. 그래야 방해 금지 중에도 깨우기 전화가 울려요.")
        }
    }

    private func row(_ item: ScheduledCall) -> some View {
        ScheduleRow(item: item)
            .contextMenu { Button("예약 취소", role: .destructive) { Task { await schedule.cancel(item) } } }
            .overlay(alignment: .bottomTrailing) {
                Button("취소") { Task { await schedule.cancel(item) } }.font(.footnote).tint(Theme.danger).buttonStyle(.borderless)
                    .accessibilityLabel("예약 취소")
            }
            .accessibilityAction(named: "예약 취소") { Task { await schedule.cancel(item) } }
    }

    /// The contract's repeat value: nil once, daily, weekdays, weekly:<0 = Monday>.
    private var rule: String? {
        switch repeatChoice {
        case "daily", "weekdays": return repeatChoice
        case "weekly": return "weekly:\(weekday)"
        default: return nil
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            try await schedule.create(at: at, repeatRule: rule, kind: kind, note: note)
            message = nil
            note = ""
            #if os(iOS)
            if kind == "wake" && !focusHintShown { focusHintShown = true; showFocusHint = true }
            #endif
        } catch GatewayError.rejected(let why) {
            message = why                                // e.g. past time, or more than 30 days ahead
        } catch {
            message = "예약하지 못했어요: " + FriendlyError.describe(error).title
        }
    }
}
