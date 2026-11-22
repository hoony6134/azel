import XCTest
@testable import Biseo

/// iPhone data (PHONE_DATA.md): fake stores → day summaries and `phone_result` frames, toggles and the secret pause.
final class PhoneDataTests: XCTestCase {
    private let cal = PhoneData.calendar
    private func at(_ d: String, _ h: Int, _ m: Int = 0) -> Date {
        cal.date(byAdding: DateComponents(hour: h, minute: m), to: PhoneData.day(d)!)!
    }

    final class FakeStores: PhoneStores {
        var stats: [HealthQuantity: [Date: DayStat]] = [:]
        var sleepSamples: [SleepSample] = []
        var workoutSamples: [WorkoutSample] = []
        var flow: [Date: String] = [:]
        var eventItems: [EventItem] = []
        var reminderItems: [ReminderItem] = []
        var people: [ContactItem] = []
        var fix = PlaceFix(name: "GIST", place: "광주광역시 북구 오룡동", activity: "walking")
        var deny = false
        var asked: [String] = []

        func daily(_ q: HealthQuantity, from: Date, to: Date) async throws -> [Date: DayStat] {
            asked.append(q.rawValue); if deny { throw PhoneDataError.denied }; return stats[q] ?? [:]
        }
        func sleep(from: Date, to: Date) async throws -> [SleepSample] { sleepSamples }
        func workouts(from: Date, to: Date) async throws -> [WorkoutSample] { workoutSamples }
        func cycle(from: Date, to: Date) async throws -> [Date: String] { flow }
        func events(from: Date, to: Date) async throws -> [EventItem] { if deny { throw PhoneDataError.denied }; return eventItems }
        func reminders(list: String?) async throws -> [ReminderItem] { reminderItems.filter { list == nil || $0.list == list } }
        func contacts(named: String) async throws -> [ContactItem] { people.filter { $0.name.contains(named) } }
        func place(withMotion: Bool) async throws -> PlaceFix { withMotion ? fix : PlaceFix(name: fix.name, place: fix.place) }
        func music() async throws -> MusicNow { MusicNow(title: "밤편지", artist: "아이유", album: nil, playing: true, recent: [("Love wins all", "아이유")]) }
    }

    private var defaults: UserDefaults!

    override func setUp() {
        defaults = UserDefaults(suiteName: "PhoneDataTests")!
        defaults.removePersistentDomain(forName: "PhoneDataTests")
        PhoneData.defaults = defaults
    }

    override func tearDown() { PhoneData.defaults = .standard }

    private func responder(_ s: FakeStores, on: Set<PhoneCategory>) -> PhoneResponder {
        PhoneResponder(stores: s, isOn: { on.contains($0) })
    }

    func testUnionCountsOverlapsOnce() {
        let a = at("2026-11-22", 1), b = at("2026-11-22", 3), c = at("2026-11-22", 2), d = at("2026-11-22", 4)
        XCTAssertEqual(PhoneSummary.unionMinutes([(a, b), (c, d)]), 180)   // iPhone 1–3 and Watch 2–4
        XCTAssertEqual(PhoneSummary.unionMinutes([(a, b), (b, d)]), 180)
        XCTAssertEqual(PhoneSummary.unionMinutes([]), 0)
    }

    func testNightBelongsToItsWakeDay() throws {
        let s = [SleepSample(start: at("2026-11-21", 23), end: at("2026-11-22", 7), stage: .inBed),
                 SleepSample(start: at("2026-11-22", 1, 10), end: at("2026-11-22", 3), stage: .core),
                 SleepSample(start: at("2026-11-22", 3), end: at("2026-11-22", 4), stage: .deep),
                 SleepSample(start: at("2026-11-22", 4), end: at("2026-11-22", 5, 30), stage: .rem),
                 SleepSample(start: at("2026-11-22", 5, 30), end: at("2026-11-22", 5, 40), stage: .awake),
                 SleepSample(start: at("2026-11-22", 5, 40), end: at("2026-11-22", 8, 5), stage: .core),
                 SleepSample(start: at("2026-11-22", 14), end: at("2026-11-22", 14, 30), stage: .asleep)]  // a nap, same day
        let n = try XCTUnwrap(PhoneSummary.night(s, wakeDay: PhoneData.day("2026-11-22")!))
        XCTAssertEqual(n["asleep_min"] as? Double, 405 + 30)  // 110 + 60 + 90 + 145, plus the nap
        XCTAssertEqual(n["deep_min"] as? Double, 60)
        XCTAssertEqual(n["rem_min"] as? Double, 90)
        XCTAssertEqual(n["awake_min"] as? Double, 10)
        XCTAssertEqual(n["in_bed_min"] as? Double, 480)
        XCTAssertEqual(n["start"] as? String, "2026-11-22T01:10:00+09:00")
        XCTAssertEqual(n["end"] as? String, "2026-11-22T08:05:00+09:00", "the nap counts as sleep but not as the wake-up")
        XCTAssertNil(PhoneSummary.night(s, wakeDay: PhoneData.day("2026-11-21")!))
    }

    func testHealthQueryAsksOnlyForTheMetricAndShapesDays() async throws {
        let s = FakeStores()
        let d22 = PhoneData.day("2026-11-22")!
        s.stats[.steps] = [d22: DayStat(sum: 8532.4)]
        let r = responder(s, on: [.health])
        let out = await r.answer(kind: "health", args: ["from": "2026-11-21", "to": "2026-11-22", "metrics": ["steps"]])
        XCTAssertEqual(out["ok"] as? Bool, true)
        XCTAssertEqual(s.asked, ["steps"])
        let days = try XCTUnwrap((out["data"] as? [String: Any])?["days"] as? [[String: Any]])
        XCTAssertEqual(days.map { $0["date"] as? String }, ["2026-11-21", "2026-11-22"])
        XCTAssertNil(days[0]["steps"])
        XCTAssertEqual(days[1]["steps"] as? Double, 8532)
        let all = await r.answer(kind: "health", args: ["metrics": ["summary"]])
        XCTAssertEqual(all["ok"] as? Bool, true)
        XCTAssertEqual(Set(s.asked.dropFirst()), ["steps", "activeEnergy", "exercise", "distance"])
    }

    func testWorkoutsAndCycleNeedTheirOwnToggle() async throws {
        let s = FakeStores()
        s.workoutSamples = [WorkoutSample(type: "running", start: at("2026-11-20", 7), minutes: 30.4, kcal: 300.2, distanceM: nil)]
        s.flow = [PhoneData.day("2026-11-20")!: "light"]
        let r = responder(s, on: [.workouts, .health])
        let w = await r.answer(kind: "workouts", args: ["from": "2026-11-20", "to": "2026-11-20"])
        let day = try XCTUnwrap(((w["data"] as? [String: Any])?["days"] as? [[String: Any]])?.first)
        let item = try XCTUnwrap((day["workouts"] as? [[String: Any]])?.first)
        XCTAssertEqual(item["type"] as? String, "running")
        XCTAssertEqual(item["minutes"] as? Double, 30)
        XCTAssertEqual(item["kcal"] as? Double, 300)
        let c = await r.answer(kind: "health", args: ["from": "2026-11-20", "metrics": ["cycle"]])
        XCTAssertEqual(c["error"] as? String, "off")   // health on is not enough: cycle is its own opt-in
        let c2 = await responder(s, on: [.cycle]).answer(kind: "health", args: ["from": "2026-11-20", "metrics": ["cycle"]])
        XCTAssertEqual((((c2["data"] as? [String: Any])?["days"] as? [[String: Any]])?.first)?["cycle"] as? String, "light")
    }

    func testOffAndDenied() async {
        let s = FakeStores()
        let off = await responder(s, on: []).answer(kind: "calendar", args: [:])
        XCTAssertEqual(off["error"] as? String, "off")
        s.deny = true
        let denied = await responder(s, on: [.calendar]).answer(kind: "calendar", args: [:])
        XCTAssertEqual(denied["error"] as? String, "denied")
    }

    func testRowStatus() {
        XCTAssertEqual(PhoneAuth.status(on: true, auth: .granted).text, "켜짐")
        XCTAssertEqual(PhoneAuth.status(on: true, auth: .notAsked).text, "아직 안 물어봄")
        XCTAssertEqual(PhoneAuth.status(on: false, auth: .granted).text, "꺼짐")
        for on in [true, false] {
            let s = PhoneAuth.status(on: on, auth: .denied)
            XCTAssertEqual(s.text, "iOS에서 거부됨")
            XCTAssertTrue(s.openSettings, "iOS never asks twice: offer the Settings app")
        }
        XCTAssertFalse(PhoneAuth.status(on: true, auth: .notAsked).openSettings)
        XCTAssertEqual(PhoneCategory.setup.count, 8)
        XCTAssertFalse(PhoneCategory.setup.contains(.cycle), "생리 주기 is its own opt-in, never in 모두 켜기")
        XCTAssertEqual(PhoneCategory.sleep.syncedKind, "sleep")
        XCTAssertNil(PhoneCategory.location.syncedKind, "location is never uploaded")
        XCTAssertTrue(PhoneCategory.allCases.allSatisfy { !$0.detail.isEmpty })
    }

    func testLiveOnlyKinds() async throws {
        let s = FakeStores()
        s.eventItems = [EventItem(title: "랩미팅", start: at("2026-11-23", 10), end: at("2026-11-23", 11), allDay: false, calendar: "학교", location: "")]
        s.reminderItems = [ReminderItem(title: "책 반납", due: nil, list: "할 일", priority: 0),
                           ReminderItem(title: "우유 사기", due: at("2026-11-22", 18), list: "장보기", priority: 1)]
        s.people = [ContactItem(name: "김철수", phones: [(label: "휴대전화", number: "010-1234-5678")], emails: ["c@x.kr"],
                                birthday: DateComponents(month: 3, day: 7), organization: nil)]
        let r = responder(s, on: [.calendar, .reminders, .contacts, .location, .music])
        let ev = await r.answer(kind: "calendar", args: ["from": "2026-11-23", "to": "2026-11-23"])
        let e = try XCTUnwrap(((ev["data"] as? [String: Any])?["events"] as? [[String: Any]])?.first)
        XCTAssertEqual(e["start"] as? String, "2026-11-23T10:00:00+09:00")
        XCTAssertNil(e["location"])
        let rem = await r.answer(kind: "reminders", args: [:])
        XCTAssertEqual(((rem["data"] as? [String: Any])?["items"] as? [[String: Any]])?.map { $0["title"] as? String }, ["우유 사기", "책 반납"])
        let con = await r.answer(kind: "contacts", args: ["name": "철수"])
        let m = try XCTUnwrap(((con["data"] as? [String: Any])?["matches"] as? [[String: Any]])?.first)
        XCTAssertEqual(m["birthday"] as? String, "0001-03-07")
        let loc = await r.answer(kind: "location", args: [:])
        XCTAssertNil((loc["data"] as? [String: Any])?["activity"], "motion is its own toggle")
        let music = await r.answer(kind: "now_playing", args: [:])
        XCTAssertEqual((music["data"] as? [String: Any])?["title"] as? String, "밤편지")
    }

    func testReplyFrameAndHello() async throws {
        let r = responder(FakeStores(), on: [.location])
        let frame = Data(#"{"type":"phone_query","id":"q1","kind":"location","args":{}}"#.utf8)
        let raw = await r.reply(to: frame)
        let reply = try XCTUnwrap(raw)
        let obj = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(reply.utf8)) as? [String: Any])
        XCTAssertEqual(obj["type"] as? String, "phone_result")
        XCTAssertEqual(obj["id"] as? String, "q1")
        XCTAssertEqual((obj["data"] as? [String: Any])?["name"] as? String, "GIST")
        let none = await r.reply(to: Data(#"{"type":"state","value":"listening"}"#.utf8))
        XCTAssertNil(none)

        XCTAssertEqual(PhoneData.kinds(), [], "everything starts off")
        PhoneData.set(.sleep, true); PhoneData.set(.cycle, true); PhoneData.set(.music, true)
        XCTAssertEqual(PhoneData.kinds(), ["health", "sleep", "now_playing"])
        XCTAssertTrue(PhoneData.helloFrame().contains("\"phone_hello\""))
    }
}

#if os(iOS)
import Contacts
import CoreLocation
import CoreMotion
import EventKit
import HealthKit
import MediaPlayer

/// Each framework's own status words → 켜짐 / iOS에서 거부됨 / 아직 안 물어봄.
final class PhonePermissionMappingTests: XCTestCase {
    func testMappings() {
        XCTAssertEqual(PhonePermissions.of(health: .shouldRequest), .notAsked)
        XCTAssertEqual(PhonePermissions.of(health: .unnecessary), .granted)
        XCTAssertEqual(PhonePermissions.of(health: .unknown), .notAsked)
        XCTAssertEqual(PhonePermissions.of(EKAuthorizationStatus.notDetermined), .notAsked)
        XCTAssertEqual(PhonePermissions.of(EKAuthorizationStatus.fullAccess), .granted)
        XCTAssertEqual(PhonePermissions.of(EKAuthorizationStatus.writeOnly), .denied, "write-only can't read")
        XCTAssertEqual(PhonePermissions.of(EKAuthorizationStatus.denied), .denied)
        XCTAssertEqual(PhonePermissions.of(CNAuthorizationStatus.authorized), .granted)
        XCTAssertEqual(PhonePermissions.of(CNAuthorizationStatus.restricted), .denied)
        XCTAssertEqual(PhonePermissions.of(CLAuthorizationStatus.authorizedWhenInUse), .granted)
        XCTAssertEqual(PhonePermissions.of(CLAuthorizationStatus.denied), .denied)
        XCTAssertEqual(PhonePermissions.of(CLAuthorizationStatus.notDetermined), .notAsked)
        XCTAssertEqual(PhonePermissions.of(CMAuthorizationStatus.authorized), .granted)
        XCTAssertEqual(PhonePermissions.of(MPMediaLibraryAuthorizationStatus.restricted), .denied)
        XCTAssertEqual(PhonePermissions.of(MPMediaLibraryAuthorizationStatus.notDetermined), .notAsked)
    }
}
#endif
