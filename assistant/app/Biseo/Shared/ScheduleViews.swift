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
                    Text(KST.dayAndTime(item.when)).font(.body.monospacedDigit()).foregroundStyle(Theme.text)
                    if PersonaModel.shared.personas.count > 1, let p = PersonaModel.shared.persona(id: item.personaID) { PersonaChip(persona: p) }
                    if item.byVoice {
                        Image(systemName: "mic.fill").font(.caption).foregroundStyle(Theme.text3).accessibilityLabel("음성으로 예약함")
                    }
                }
                let meta = [item.isWake ? "깨우기" : "알림", item.notify == "push" ? "푸시로" : nil,
                            item.ruleText ?? KST.repeatLabel(item.repeatRule), (item.attempts ?? 0) > 0 ? "시도 \(item.attempts!)회" : nil]
                Text(meta.compactMap { $0 }.joined(separator: " · ")).font(.footnote).foregroundStyle(Theme.text3)
                if let text = [item.purpose, item.note].compactMap({ $0 }).first(where: { !$0.isEmpty }) {
                    Text(text).font(.subheadline).foregroundStyle(Theme.text2).lineLimit(2)
                }
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
            } else if schedule.status == .ok {
                Text("예약된 전화가 없어요").foregroundStyle(Theme.text2)
            }
            SectionNote(status: schedule.status) { Task { await schedule.refresh() } }
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
    @State private var personaID: String? = PersonaModel.shared.selected?.id
    @State private var note = ""
    @State private var saving = false
    @State private var message: String?
    @State private var showFocusHint = false
    @State private var editing: ScheduledCall?
    @State private var deleting: ScheduledCall?
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
                    if PersonaModel.shared.personas.count > 1 {
                        HStack {
                            Text("비서").foregroundStyle(Theme.text)
                            Spacer()
                            Picker("", selection: $personaID) {
                                ForEach(PersonaModel.shared.personas) { Text($0.name).tag(Optional($0.id)) }
                            }
                            .pickerStyle(.menu).labelsHidden().tint(Theme.celadon).fixedSize()
                        }
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
                SectionNote(status: schedule.status) { Task { await schedule.refreshAll() } }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("예약 전화")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        .refreshable { await schedule.refreshAll() }
        .task { await schedule.refreshAll() }
        .sheet(item: $editing) { item in
            NavigationStack { ScheduleEditor(item: item, schedule: schedule) }
                #if os(macOS)
                .frame(width: 480, height: 600)
                #endif
        }
        .confirmationDialog("반복 예약을 취소할까요?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                            titleVisibility: .visible, presenting: deleting) { item in
            Button("이번만 취소") { Task { do { try await schedule.skip(item) } catch { message = "취소하지 못했어요: " + FriendlyError.describe(error).title } } }
            Button("모든 반복 취소", role: .destructive) { Task { await schedule.cancel(item) } }
            Button("닫기", role: .cancel) {}
        } message: { item in Text([item.ruleText, "다음: " + KST.dayAndTime(item.when)].compactMap { $0 }.joined(separator: "\n")) }
        .alert("집중 모드에서도 울리게 하려면", isPresented: $showFocusHint) {
            Button("확인") {}
        } message: {
            Text("설정 → 집중 모드에서 허용된 앱에 Azel을 추가해 주세요. 그래야 방해 금지 중에도 깨우기 전화가 울려요.")
        }
    }

    private func row(_ item: ScheduledCall) -> some View {
        let editable = schedule.canEdit && item.isActive
        return ScheduleRow(item: item)
            .contentShape(Rectangle())
            .onTapGesture { if editable { editing = item } }
            .contextMenu {
                if editable { Button("편집") { editing = item } }
                Button("예약 취소", role: .destructive) { remove(item) }
            }
            .overlay(alignment: .bottomTrailing) {
                Button("취소") { remove(item) }.font(.footnote).tint(Theme.danger).buttonStyle(.borderless)
                    .accessibilityLabel("예약 취소")
            }
            .accessibilityAction(named: "예약 취소") { remove(item) }
            .accessibilityAction(named: "편집") { if editable { editing = item } }
    }

    /// A repeating item asks whether to drop only the next call or the whole rule.
    private func remove(_ item: ScheduledCall) {
        if item.isRule && schedule.canEdit { deleting = item } else { Task { await schedule.cancel(item) } }
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
            try await schedule.create(at: at, repeatRule: rule, kind: kind, note: note,
                                      persona: PersonaModel.shared.personas.count > 1 ? personaID : nil)
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

/// Edit one scheduled call: time, 용건, call or push, and for rules the repeat. Repeating items ask
/// "이번만 / 모든 반복" on save.
struct ScheduleEditor: View {
    let item: ScheduledCall
    @ObservedObject var schedule: ScheduleModel
    @Environment(\.dismiss) private var dismiss

    private enum Kind: String, CaseIterable { case daily, weekdays, days, hours, monthly
        var label: String {
            switch self { case .daily: "매일"; case .weekdays: "평일"; case .days: "요일 선택"; case .hours: "N시간마다"; case .monthly: "매월" }
        }
    }

    @State private var time: Date
    @State private var purpose: String
    @State private var notify: String
    @State private var kind = Kind.daily
    @State private var days: Set<Int> = []          // cron dow, 0 = Sunday
    @State private var everyN = 2
    @State private var fromHour = 9
    @State private var toHour = 18
    @State private var weekdaysOnly = false
    @State private var monthDay = 1
    /// The rule couldn't be shown as a preset: saving the whole rule replaces it.
    @State private var custom = false
    @State private var askScope = false
    @State private var saving = false
    @State private var message: String?

    init(item: ScheduledCall, schedule: ScheduleModel) {
        self.item = item
        self.schedule = schedule
        _time = State(initialValue: item.when)
        _purpose = State(initialValue: item.purpose ?? item.note ?? "")
        _notify = State(initialValue: item.notify ?? "call")
        if let cron = item.cron, item.isRule {
            if let p = RepeatPreset.parse(cron) {
                var cal = Calendar(identifier: .gregorian); cal.timeZone = KST.zone
                _time = State(initialValue: cal.date(bySettingHour: p.hour, minute: p.minute, second: 0, of: item.when) ?? item.when)
                switch p.preset {
                case .daily: _kind = State(initialValue: .daily)
                case .weekdays: _kind = State(initialValue: .weekdays)
                case .days(let d): _kind = State(initialValue: .days); _days = State(initialValue: d)
                case let .everyHours(n, from, to, wk):
                    _kind = State(initialValue: .hours); _everyN = State(initialValue: n)
                    _fromHour = State(initialValue: from); _toHour = State(initialValue: to); _weekdaysOnly = State(initialValue: wk)
                case .monthly(let d): _kind = State(initialValue: .monthly); _monthDay = State(initialValue: d)
                }
            } else {
                _custom = State(initialValue: true)
            }
        }
    }

    private var preset: RepeatPreset {
        switch kind {
        case .daily: .daily
        case .weekdays: .weekdays
        case .days: .days(days)
        case .hours: .everyHours(n: everyN, from: fromHour, to: toHour, weekdaysOnly: weekdaysOnly)
        case .monthly: .monthly(day: monthDay)
        }
    }

    private var hm: (Int, Int) {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = KST.zone
        let c = cal.dateComponents([.hour, .minute], from: time)
        return (c.hour ?? 0, c.minute ?? 0)
    }

    var body: some View {
        Form {
            Section {
                if let t = item.ruleText { LabeledContent("반복", value: t) }
                LabeledContent("다음", value: KST.dayAndTime(item.when))
            }
            Section("시각") {
                if item.isRule && kind == .hours {
                    Stepper("\(everyN)시간마다", value: $everyN, in: 1...12)
                    Stepper("\(fromHour)시부터", value: $fromHour, in: 0...23)
                    Stepper("\(toHour)시까지", value: $toHour, in: fromHour...23)
                    Toggle("평일만", isOn: $weekdaysOnly)
                } else {
                    DatePicker("시각", selection: $time, displayedComponents: item.isRule ? [.hourAndMinute] : [.date, .hourAndMinute])
                        .environment(\.timeZone, KST.zone)
                }
            }
            if item.isRule {
                Section("반복") {
                    Picker("반복", selection: $kind) { ForEach(Kind.allCases, id: \.self) { Text($0.label).tag($0) } }
                    if kind == .days {
                        HStack {
                            // Shown Monday first; cron numbers Sunday 0.
                            ForEach([1, 2, 3, 4, 5, 6, 0], id: \.self) { d in
                                let on = days.contains(d)
                                Button(KST.weekdays[(d + 6) % 7]) { if on { days.remove(d) } else { days.insert(d) } }
                                    .buttonStyle(.bordered).tint(on ? Theme.celadon : Theme.text3)
                                    .accessibilityAddTraits(on ? .isSelected : [])
                            }
                        }
                    }
                    if kind == .monthly { Stepper("매월 \(monthDay)일", value: $monthDay, in: 1...31) }
                    if custom { Text("지금 규칙은 여기서 그대로 보여 줄 수 없어요. 모든 반복을 저장하면 고른 반복으로 바뀌어요.").font(.footnote).foregroundStyle(Theme.text3) }
                }
            }
            Section("용건") { TextField("무엇을 알려 드릴까요?", text: $purpose, axis: .vertical).lineLimit(1...4) }
            Section {
                Picker("방법", selection: $notify) { Text("전화").tag("call"); Text("푸시 알림").tag("push") }.pickerStyle(.segmented)
            }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .formStyle(.grouped)
        .navigationTitle("예약 편집")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("취소") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button(saving ? "저장 중…" : "저장") { if item.isRule { askScope = true } else { Task { await save(all: true) } } }
                    .disabled(saving || (item.isRule && kind == .days && days.isEmpty))
            }
        }
        .confirmationDialog("어떤 예약을 바꿀까요?", isPresented: $askScope, titleVisibility: .visible) {
            Button("이번만") { Task { await save(all: false) } }
            Button("모든 반복") { Task { await save(all: true) } }
            Button("닫기", role: .cancel) {}
        } message: { Text("이번만 바꾸면 반복은 그대로 두고 다음 한 번만 바꿔요.") }
    }

    /// The one-off that replaces the next fire: that day, at the picked time.
    private var occurrenceTime: Date {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = KST.zone
        return cal.date(bySettingHour: hm.0, minute: hm.1, second: 0, of: item.when) ?? item.when
    }

    private func save(all: Bool) async {
        saving = true
        defer { saving = false }
        do {
            if item.isRule && !all {
                try await ScheduleAPI.replaceOccurrence(of: item, at: occurrenceTime, notify: notify, purpose: purpose)
            } else if item.isRule {
                try await ScheduleAPI.update(item.id, cron: preset.cron(hour: hm.0, minute: hm.1), at: nil, notify: notify, purpose: purpose)
            } else {
                try await ScheduleAPI.update(item.id, cron: nil, at: time, notify: notify, purpose: purpose)
            }
            await schedule.refreshAll()
            dismiss()
        } catch GatewayError.rejected(let why) {
            message = why                       // e.g. "30분보다 촘촘한 반복은 만들 수 없어요"
        } catch {
            message = "저장하지 못했어요: " + FriendlyError.describe(error).title
        }
    }
}
