import Contacts
import CoreLocation
import CoreMotion
import EventKit
import HealthKit
import MediaPlayer
import SwiftUI

/// iOS permissions: asked only when a toggle is turned on, and read back as `PhoneAuth` for each row.
enum PhonePermissions {
    private static let location = CLLocationManager()  // kept alive so its prompt stays up

    static func request(_ c: PhoneCategory) async {
        switch c {
        case .health, .sleep, .workouts, .cycle:
            guard HKHealthStore.isHealthDataAvailable() else { return }
            try? await AppleStores.shared.health.requestAuthorization(toShare: [], read: AppleStores.readTypes(c))
        case .calendar: _ = try? await AppleStores.shared.events.requestFullAccessToEvents()
        case .reminders: _ = try? await AppleStores.shared.events.requestFullAccessToReminders()
        case .contacts: _ = try? await AppleStores.shared.contactStore.requestAccess(for: .contacts)
        case .location: await MainActor.run { location.requestWhenInUseAuthorization() }
        case .motion:  // the first query shows the prompt
            if CMMotionActivityManager.isActivityAvailable() {
                CMMotionActivityManager().queryActivityStarting(from: Date().addingTimeInterval(-60), to: Date(), to: .main) { _, _ in }
            }
        case .music: _ = await withCheckedContinuation { c in MPMediaLibrary.requestAuthorization { c.resume(returning: $0) } }
        case .sync: break
        }
    }

    static func auth(_ c: PhoneCategory) async -> PhoneAuth {
        switch c {
        case .health, .sleep, .workouts, .cycle:
            guard HKHealthStore.isHealthDataAvailable() else { return .denied }
            let s = try? await AppleStores.shared.health.statusForAuthorizationRequest(toShare: [], read: AppleStores.readTypes(c))
            return of(health: s ?? .unknown)
        case .calendar: return of(EKEventStore.authorizationStatus(for: .event))
        case .reminders: return of(EKEventStore.authorizationStatus(for: .reminder))
        case .contacts: return of(CNContactStore.authorizationStatus(for: .contacts))
        case .location: return of(location.authorizationStatus)
        case .motion: return of(CMMotionActivityManager.authorizationStatus())
        case .music: return of(MPMediaLibrary.authorizationStatus())
        case .sync: return .granted
        }
    }

    // The mappings (unit tested): every framework says "not asked", "allowed" or "refused" in its own words.
    static func of(health s: HKAuthorizationRequestStatus) -> PhoneAuth { s == .unnecessary ? .granted : .notAsked }
    static func of(_ s: EKAuthorizationStatus) -> PhoneAuth {
        switch s {
        case .notDetermined: .notAsked
        case .fullAccess: .granted
        default: .denied  // writeOnly can't read; denied, restricted
        }
    }
    static func of(_ s: CNAuthorizationStatus) -> PhoneAuth {
        switch s {
        case .notDetermined: .notAsked
        case .authorized, .limited: .granted
        default: .denied
        }
    }
    static func of(_ s: CLAuthorizationStatus) -> PhoneAuth {
        switch s {
        case .notDetermined: .notAsked
        case .authorizedWhenInUse, .authorizedAlways: .granted
        default: .denied
        }
    }
    static func of(_ s: CMAuthorizationStatus) -> PhoneAuth {
        switch s {
        case .notDetermined: .notAsked
        case .authorized: .granted
        default: .denied
        }
    }
    static func of(_ s: MPMediaLibraryAuthorizationStatus) -> PhoneAuth {
        switch s {
        case .notDetermined: .notAsked
        case .authorized: .granted
        default: .denied
        }
    }
}

/// Toggles and permission states for one screen; the same model backs Settings and the first-run sheet.
@MainActor
final class PhoneDataModel: ObservableObject {
    @Published var on: [PhoneCategory: Bool] = [:]
    @Published var auth: [PhoneCategory: PhoneAuth] = [:]
    @Published var note: String?

    init() { reload() }

    func reload() {
        on = Dictionary(uniqueKeysWithValues: PhoneCategory.allCases.map { ($0, PhoneData.isOn($0)) })
        Task { for c in PhoneCategory.allCases { auth[c] = await PhonePermissions.auth(c) } }
    }

    func binding(_ c: PhoneCategory) -> Binding<Bool> {
        Binding(get: { self.on[c] ?? false }, set: { v in Task { await self.set(c, v) } })
    }

    /// On: the iOS prompt (only if never asked), then an upload. Off: stops at once and deletes what was synced.
    func set(_ c: PhoneCategory, _ value: Bool) async {
        on[c] = value
        PhoneData.set(c, value)
        if value {
            await PhonePermissions.request(c)
        } else {
            do {
                if c == .sync { try await PhoneDataSync.deleteSynced() } else if let k = c.syncedKind { try await PhoneDataSync.deleteSynced(kind: k) }
            } catch { note = "서버에 올린 요약을 지우지 못했어요. 아래 '모든 데이터 삭제'로 다시 지워 주세요." }
        }
        auth[c] = await PhonePermissions.auth(c)
        PhoneDataSync.observe()
        PhoneDataSync.schedule()
        if value && (PhoneCategory.synced.contains(c) || c == .sync) { await PhoneDataSync.upload(force: true) }
    }

    func setAll(_ value: Bool) async {
        for c in PhoneCategory.setup where (on[c] ?? false) != value { await set(c, value) }
    }

    func deleteAll() async {
        do { try await PhoneDataSync.deleteSynced(); note = "서버에 올린 요약을 모두 지웠어요." }
        catch { note = "지우지 못했어요: \(error.localizedDescription)" }
    }
}

private struct PhoneCategoryRow: View {
    let c: PhoneCategory
    @ObservedObject var model: PhoneDataModel

    var body: some View {
        let s = PhoneAuth.status(on: model.on[c] ?? false, auth: model.auth[c] ?? .notAsked)
        Toggle(isOn: model.binding(c)) {
            VStack(alignment: .leading, spacing: 2) {
                Text(c.title)
                Text(c.detail).font(.caption).foregroundStyle(Theme.text2)
                if c != .sync {
                    Text(s.text).font(.caption).foregroundStyle(s.openSettings ? Theme.amber : Theme.text3)
                }
                if s.openSettings {
                    Button("설정 앱에서 허용") { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) }
                        .font(.caption)
                }
            }
        }
    }
}

/// Settings → "아이폰 데이터": the one row on the settings screen.
struct PhoneDataCard: View {
    var body: some View {
        Card("아이폰 데이터") {
            NavigationLink { PhoneDataScreen() } label: {
                HStack {
                    Text("건강·수면·캘린더·위치 등").foregroundStyle(Theme.text)
                    Spacer()
                    Text("\(PhoneCategory.allCases.filter { $0 != .sync && PhoneData.isOn($0) }.count)개 켬").foregroundStyle(Theme.text3)
                    Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }
}

struct PhoneDataScreen: View {
    @StateObject private var model = PhoneDataModel()
    @State private var confirmDelete = false

    var body: some View {
        Form {
            Section {
                ForEach(PhoneCategory.allCases.filter { $0 != .sync }) { PhoneCategoryRow(c: $0, model: model) }
            } header: { Text("통화 중에 읽기") } footer: {
                Text("켜 둔 항목만 아젤이 물어볼 때 읽어요. 끄면 바로 읽지 않고, 서버에 올린 요약도 지워요.")
            }
            Section {
                PhoneCategoryRow(c: .sync, model: model)
                Button("모든 데이터 삭제", role: .destructive) { confirmDelete = true }
            } header: { Text("백그라운드 동기화") } footer: {
                Text("건강·수면·운동의 하루 요약만 게이트웨이에 올리고 30일 뒤 지워요. 원래 기록은 아이폰에만 있어요." + lastUpload)
            }
            if let note = model.note { Section { Text(note).foregroundStyle(Theme.text2) } }
        }
        .navigationTitle("아이폰 데이터")
        .onAppear { model.reload() }
        .confirmationDialog("서버에 올린 아이폰 요약을 모두 지울까요?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("모두 지우기", role: .destructive) { Task { await model.deleteAll() } }
        }
    }

    private var lastUpload: String {
        let t = PhoneData.defaults.double(forKey: PhoneDataSync.lastUploadKey)
        return t > 0 ? "\n마지막으로 올린 시각: " + Date(timeIntervalSince1970: t).formatted(date: .abbreviated, time: .shortened) : ""
    }
}

/// The first-run sheet: every category with what Azel reads, 모두 켜기 / 모두 끄기, and a way to skip it.
struct PhoneDataSetup: View {
    @StateObject private var model = PhoneDataModel()
    var done: () -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("아젤이 아이폰에서 무엇을 읽을지 고르세요. 켠 항목만 iOS가 허락을 물어요. 나중에 설정 → 아이폰 데이터에서 언제든 바꿀 수 있어요.")
                        .foregroundStyle(Theme.text2)
                    HStack {
                        Button("모두 켜기") { Task { await model.setAll(true) } }
                        Spacer()
                        Button("모두 끄기") { Task { await model.setAll(false) } }
                    }
                    .buttonStyle(.borderless)
                }
                Section { ForEach(PhoneCategory.setup) { PhoneCategoryRow(c: $0, model: model) } }
            }
            .navigationTitle("아이폰 데이터")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("건너뛰기", action: done) }
                ToolbarItem(placement: .confirmationAction) { Button("완료", action: done) }
            }
        }
    }
}

extension View {
    /// Shows `PhoneDataSetup` once, the first time the app's main screen appears.
    func phoneDataFirstRun() -> some View { modifier(PhoneDataFirstRun()) }
}

private struct PhoneDataFirstRun: ViewModifier {
    @AppStorage("phone.setupSeen") private var seen = false
    func body(content: Content) -> some View {
        content.sheet(isPresented: Binding(get: { !seen }, set: { if !$0 { seen = true } })) {
            PhoneDataSetup { seen = true }
        }
    }
}
