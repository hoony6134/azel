import AppIntents
import Intents
import SwiftUI
import WatchConnectivity

@main
struct AzelWatchApp: App {
    init() { WatchLink.shared.start() }

    var body: some Scene {
        WindowGroup {
            WatchRoot()
                // the complication and the iPhone widget open azel://call
                .onOpenURL { url in if url.host == "call" { WatchCall.shared.start() } }
                // "아젤한테 전화해줘" through Siri's calling intent
                .onContinueUserActivity(NSStringFromClass(INStartCallIntent.self)) { _ in WatchCall.shared.start() }
        }
    }
}

/// One persona as the iPhone sent it (WatchSync): enough for the picker, the orb and `persona=`.
struct WatchPersona: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var glaze: UInt32
    var primary: Bool

    var tint: Color { Color(dark: glaze, light: glaze) }

    static let listKey = "watch.personas", selectedKey = "watch.persona"

    static var all: [WatchPersona] {
        (UserDefaults.standard.data(forKey: listKey)).flatMap { try? JSONDecoder().decode([WatchPersona].self, from: $0) } ?? []
    }

    /// The one picked on the watch, else the primary; nil before the iPhone sent any.
    static var selected: WatchPersona? {
        let list = all, id = UserDefaults.standard.string(forKey: selectedKey)
        return list.first { $0.id == id } ?? list.first(where: \.primary) ?? list.first
    }
}

struct WatchRoot: View {
    @ObservedObject private var call = WatchCall.shared

    var body: some View {
        Group {
            if call.inCall { WatchCallScreen() } else { WatchHome() }
        }
        .animation(Theme.state, value: call.inCall)
    }
}

/// The companion at rest: one jar per persona, turned with the Digital Crown; tap the jar to call.
struct WatchHome: View {
    @ObservedObject private var call = WatchCall.shared
    @ObservedObject private var link = WatchLink.shared
    @AppStorage(WatchPersona.selectedKey) private var selectedID = ""
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback

    var body: some View {
        if link.personas.isEmpty {
            page(nil)
        } else {
            TabView(selection: $selectedID) {
                ForEach(link.personas) { p in page(p).tag(p.id) }
            }
            .tabViewStyle(.verticalPage)
            .onAppear { if !link.personas.contains(where: { $0.id == selectedID }) { selectedID = WatchPersona.selected?.id ?? "" } }
        }
    }

    private func page(_ p: WatchPersona?) -> some View {
        let tint = p?.tint ?? Theme.glazeCeladon
        return ZStack {
            GlazeBackground(state: .idle, tint: tint)
            VStack(spacing: Theme.s) {
                Button { call.start(persona: p) } label: {
                    MoonJar(state: .idle, levels: call.levels, tint: tint, breathAmplitude: 0.6)
                        .frame(width: 84, height: 84)
                        .padding(Theme.s)
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(p?.name ?? name)에게 전화하기")
                Text(p?.name ?? name).font(.headline).foregroundStyle(Theme.text)
                if let e = call.error {
                    Text(e).font(.caption2).foregroundStyle(Theme.text2).multilineTextAlignment(.center)
                } else {
                    Label("탭해서 부르기", systemImage: "phone.fill").font(.caption2).foregroundStyle(Theme.text2)
                }
            }
            .padding(.horizontal)
        }
    }
}

/// The iPhone call screen, shrunk: state glaze, the jar reacting to the voice, one caption, two buttons.
struct WatchCallScreen: View {
    @ObservedObject private var call = WatchCall.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback

    private var tint: Color { call.persona?.tint ?? Theme.glazeCeladon }
    private var jar: JarState { JarState(callState: call.displayState, inCall: true, held: false) }

    var body: some View {
        ZStack {
            GlazeBackground(state: jar, tint: tint)
            VStack(spacing: Theme.xs) {
                HStack(spacing: Theme.xs) {
                    Text(call.persona?.name ?? name).font(.footnote.weight(.semibold)).foregroundStyle(Theme.text)
                    Text(jar.spokenLabel).font(.footnote).foregroundStyle(jar.color)
                    if call.muted { Image(systemName: "mic.slash.fill").font(.footnote).foregroundStyle(Theme.amber).accessibilityLabel("음소거됨") }
                }
                MoonJar(state: jar, levels: call.levels, tint: tint, muted: call.muted)
                    .frame(width: 64, height: 64)
                    .padding(.vertical, Theme.xs)
                if let line = call.log.lines.last {
                    // The reply brightens as it is heard, like the iPhone's captions.
                    TimelineView(.animation(minimumInterval: 1 / 10, paused: line.mine || !call.playing)) { _ in
                        let k = call.karaoke(stepped: reduceMotion)
                        Group {
                            if !line.mine, k.texts.joined(separator: " ") == line.text {
                                KaraokeText(text: line.text, heard: k.spoken)
                            } else {
                                Text(line.text).foregroundStyle(line.mine ? Theme.text2 : Theme.text)
                            }
                        }
                        .font(.caption2)
                        .lineLimit(3)
                        .multilineTextAlignment(.center)
                    }
                    .animation(Theme.caption, value: line.text)
                    // Cards: one line each on the Watch, no charts.
                    ForEach(Array(line.cards.enumerated()), id: \.offset) { _, c in
                        Text(c.summary()).font(.caption2.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.celadon)
                            .lineLimit(1).minimumScaleFactor(0.8)
                    }
                }
                Spacer(minLength: 0)
                HStack(spacing: Theme.l) {
                    Button { call.toggleMute() } label: {
                        Image(systemName: call.muted ? "mic.slash.fill" : "mic.fill").font(.body)
                            .frame(width: 44, height: 44)
                            .background(Circle().fill(call.muted ? Theme.porcelain : Theme.card))
                            .foregroundStyle(call.muted ? Theme.onAccent : Theme.text)
                    }
                    .accessibilityLabel(call.muted ? "음소거 해제" : "음소거")
                    Button { call.hangUp() } label: {
                        Image(systemName: "phone.down.fill").font(.body)
                            .frame(width: 44, height: 44)
                            .background(Circle().fill(Theme.hangup))
                            .foregroundStyle(.white)
                    }
                    .accessibilityLabel("통화 종료")
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal)
        }
        .sensoryFeedback(.impact(weight: .light), trigger: jar) { old, new in old == .listening && new == .thinking }
    }
}

/// "시리야, 아젤 불러줘" on the watch: opens the app and calls the persona picked on the watch.
struct StartWatchCallIntent: AppIntent {
    static let title: LocalizedStringResource = "Azel 부르기"
    static let description = IntentDescription("Azel에게 전화를 걸어요.")
    static let openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult {
        WatchCall.shared.start()
        return .result()
    }
}

struct WatchShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartWatchCallIntent(),
                    phrases: ["\(.applicationName)", "\(.applicationName) 불러", "\(.applicationName) 불러줘",
                              "\(.applicationName)이랑 통화해줘", "\(.applicationName)한테 전화해줘", "\(.applicationName)에게 전화해"],
                    shortTitle: "Azel 부르기", systemImageName: "phone.fill")
    }
}

/// The iPhone sends the token, addresses and personas over WatchConnectivity; the token goes to the watch's Keychain.
final class WatchLink: NSObject, ObservableObject, WCSessionDelegate {
    static let shared = WatchLink()
    @Published private(set) var personas = WatchPersona.all

    func start() {
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    private func apply(_ ctx: [String: Any]) {
        if let t = ctx["token"] as? String, !t.isEmpty, t != Config.token { Config.token = t }
        if let v = ctx["voiceURL"] as? String { Config.voiceURL = v }
        if let g = ctx["gatewayURL"] as? String { Config.gatewayURL = g }
        if let n = ctx["name"] as? String { AssistantName.current = n }
        let list = (ctx["personas"] as? [[String: Any]])?.compactMap { d -> WatchPersona? in
            guard let id = d["id"] as? String, let name = d["name"] as? String else { return nil }
            return WatchPersona(id: id, name: name, glaze: UInt32((d["glaze"] as? Int) ?? 0x82CBB4), primary: d["primary"] as? Bool ?? false)
        }
        if let list, let data = try? JSONEncoder().encode(list) { UserDefaults.standard.set(data, forKey: WatchPersona.listKey) }
        DispatchQueue.main.async {
            if let list { self.personas = list }
            if WatchCall.shared.error != nil, Config.token != nil { WatchCall.shared.error = nil }
        }
    }

    func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?) {
        apply(session.receivedApplicationContext)
    }

    func session(_ session: WCSession, didReceiveApplicationContext ctx: [String: Any]) { apply(ctx) }
}
