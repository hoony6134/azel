import Intents
import XCTest
@testable import Biseo
#if os(iOS)
import AppIntents
import UserNotifications
#endif

@MainActor
final class SiriIntentTests: XCTestCase {
    private var saved: ((Persona?) async -> String)!

    override func setUp() async throws { saved = StartAzelCallIntent.startCall }
    override func tearDown() async throws { StartAzelCallIntent.startCall = saved }

    func testTheIntentStartsOneCallAndNeverOpensTheApp() async throws {
        var started = 0
        StartAzelCallIntent.startCall = { _ in started += 1; return "곧 전화드릴게요." }
        _ = try await StartAzelCallIntent(persona: PersonaEntity(id: "x", name: "미아")).perform()
        _ = try await StartAzelCallIntent().perform()
        XCTAssertEqual(started, 2)
        XCTAssertFalse(StartAzelCallIntent.openAppWhenRun, "Siri must not ask to unlock and open the app")
        XCTAssertEqual(StartAzelCallIntent.resolve(nil)?.id, PersonaModel.shared.primary?.id)
    }

    /// Locked or in the background: the gateway rings back (an incoming CallKit call works on the
    /// lock screen); open and unlocked: the local outgoing call.
    func testRouting() {
        XCTAssertEqual(StartAzelCallIntent.route(appActive: true, protectedDataAvailable: true), .local)
        XCTAssertEqual(StartAzelCallIntent.route(appActive: false, protectedDataAvailable: true), .ringback, "app closed / background")
        XCTAssertEqual(StartAzelCallIntent.route(appActive: false, protectedDataAvailable: false), .ringback, "locked in a pocket")
        XCTAssertEqual(StartAzelCallIntent.route(appActive: true, protectedDataAvailable: false), .ringback)
    }

    func testRingbackRequest() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil; FakeServer.routes = [:] }
        FakeServer.seen = []
        FakeServer.routes = ["POST /v1/calls": (200, #"{"ok":true,"spoken":"지금 전화할게요.","data":{}}"#)]
        let said = try await StartAzelCallIntent.ringback(persona: nil)
        XCTAssertEqual(said, "곧 전화드릴게요.")
        let body = try JSONSerialization.jsonObject(with: FakeServer.seen.last!.body) as! [String: Any]
        XCTAssertEqual(body["reason"] as? String, "사용자가 Siri로 불렀어요")
        XCTAssertEqual(body["intent"] as? String, "siri")
    }

    func testAlternativeNamesAreInTheInfoPlist() throws {
        let names = (Bundle.main.object(forInfoDictionaryKey: "INAlternativeAppNames") as? [[String: String]])?.compactMap { $0["INAlternativeAppName"] }
        XCTAssertEqual(Set(names ?? []), ["아젤", "비서"])
    }

    func testLockedFlagOnTheCallURL() throws {
        let locked = try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "s", callID: nil, locked: true)
        XCTAssertTrue(locked.url!.absoluteString.contains("locked=1"))
        XCTAssertFalse(try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "s", callID: nil).url!.absoluteString.contains("locked"))
    }

    #if os(iOS)
    func testCallIntentIsDeclaredAndHandled() {
        let supported = Bundle.main.object(forInfoDictionaryKey: "INIntentsSupported") as? [String]
        XCTAssertEqual(supported, ["INStartCallIntent"])
        XCTAssertNotNil(PushAppDelegate().application(UIApplication.shared, handlerFor: INStartCallIntent(callRecordFilter: nil, callRecordToCallBack: nil, audioRoute: .unknown, destinationType: .normal, contacts: nil, callCapability: .audioCall)))
    }

    func testTokenItemIsReadableAfterFirstUnlock() {
        let account = "test-locked-\(UUID().uuidString)"
        defer { Keychain.write(nil, account: account) }
        Keychain.write("secret", account: account)
        XCTAssertEqual(Keychain.accessibility(account: account), kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
        // An older item with a stricter class is upgraded.
        Keychain.write(nil, account: account)
        let strict: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: Keychain.service,
                                     kSecAttrAccount as String: account, kSecValueData as String: Data("old".utf8),
                                     kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlocked]
        SecItemAdd(strict as CFDictionary, nil)
        XCTAssertEqual(Keychain.accessibility(account: account), kSecAttrAccessibleWhenUnlocked as String)
        Keychain.ensureReadableWhileLocked(account: account)
        XCTAssertEqual(Keychain.accessibility(account: account), kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
        XCTAssertEqual(Keychain.read(account: account), "old", "the value survives")
    }

    /// Plain 허용 and 거부 work while locked; 항상 허용, and 허용 on forced-once or risky approvals,
    /// ask for Face ID / passcode / an unlocked Apple Watch.
    func testApprovalActionOptions() {
        func options(_ category: String) -> [String: UNNotificationActionOptions] {
            let c = PushAction.categories.first { $0.identifier == category }!
            return Dictionary(uniqueKeysWithValues: c.actions.map { ($0.identifier, $0.options) })
        }
        let plain = options("APPROVAL")
        XCTAssertFalse(plain["APPROVE_ONCE"]!.contains(.authenticationRequired))
        XCTAssertFalse(plain["DENY"]!.contains(.authenticationRequired))
        XCTAssertTrue(plain["APPROVE_ALWAYS"]!.contains(.authenticationRequired))
        let once = options("APPROVAL_ONCE")
        XCTAssertTrue(once["APPROVE_ONCE"]!.contains(.authenticationRequired))
        XCTAssertNil(once["APPROVE_ALWAYS"])
        let risky = options("APPROVAL_RISKY")
        XCTAssertTrue(risky["APPROVE_ONCE"]!.contains(.authenticationRequired))
        XCTAssertTrue(risky["APPROVE_ALWAYS"]!.contains(.authenticationRequired))
    }

    func testLockScreenButtonsUseTheAuthenticatedIntentWhenNeeded() {
        XCTAssertFalse(PendingApproval(id: "a", device: "m", command: "ls", forcedOnce: false).onceNeedsAuth)
        XCTAssertTrue(PendingApproval(id: "a", device: "m", command: "rm x", forcedOnce: true).onceNeedsAuth)
        XCTAssertTrue(PendingApproval(id: "a", device: "m", command: "x", forcedOnce: false, risky: true).onceNeedsAuth)
        XCTAssertTrue(ApprovalAuthDecisionIntent.authenticationPolicy == .requiresAuthentication)
        XCTAssertTrue(ApprovalRequest(["id": "1", "tier": "risky"]).risky)
    }
    #endif
}
