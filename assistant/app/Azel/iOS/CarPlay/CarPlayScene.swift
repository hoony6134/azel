import CarPlay
import UIKit

/// What the CarPlay list shows: voice-first, short lines, no typing (CarPlay's communication rules).
/// Built as plain data so it can be tested without a car.
enum CarPlayMenu {
    enum Action: Equatable { case call(personaID: String?), briefing, resume(historyID: String) }
    struct Item: Equatable { var text: String; var detail: String?; var action: Action }
    struct Section: Equatable { var title: String; var items: [Item] }

    static func sections(personas: [Persona], assistant: String, recent: [HistoryItem], now: Date = Date()) -> [Section] {
        let call = personas.isEmpty
            ? [Item(text: "\(assistant) 부르기", detail: nil, action: .call(personaID: nil))]
            : personas.map { Item(text: "\($0.name) 부르기", detail: $0.primary ? "기본 비서" : nil, action: .call(personaID: $0.id)) }
        var out = [Section(title: "비서", items: call),
                   Section(title: "지금", items: [Item(text: "오늘 브리핑", detail: nil, action: .briefing)])]
        let resume = recent.filter { !($0.session ?? "").contains("secret-") }.prefix(3).map { h in
            Item(text: clip(h.title ?? h.reason ?? "지난 통화", 24),
                 detail: KST.dayAndTime(Date(timeIntervalSince1970: h.started_at), now: now), action: .resume(historyID: h.id))
        }
        if !resume.isEmpty { out.append(Section(title: "이어서 통화", items: Array(resume))) }
        return out
    }

    static func clip(_ s: String, _ n: Int) -> String { s.count <= n ? s : String(s.prefix(n - 1)) + "…" }
}

/// The CarPlay screen (CPTemplateApplicationScene). It appears in the car only once Apple grants the
/// communication entitlement (com.apple.developer.carplay-communication); until then, simulator
/// builds carry it (Azel-iOS-CarPlay.entitlements) so it can be tried in the CarPlay simulator.
/// Calls themselves already show in CarPlay without it, through CallKit.
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var interface: CPInterfaceController?
    private let list = CPListTemplate(title: "Azel", sections: [])
    @MainActor private lazy var history = HistoryModel()

    func templateApplicationScene(_ scene: CPTemplateApplicationScene, didConnect interfaceController: CPInterfaceController) {
        interface = interfaceController
        MainActor.assumeIsolated {
            reload()
            interfaceController.setRootTemplate(list, animated: false, completion: nil)
            Task { await PersonaModel.shared.refresh(); await history.refresh(); reload() }
        }
    }

    func templateApplicationScene(_ scene: CPTemplateApplicationScene, didDisconnectInterfaceController interfaceController: CPInterfaceController) {
        interface = nil
    }

    @MainActor private func reload() {
        let sections = CarPlayMenu.sections(personas: PersonaModel.shared.ordered, assistant: AssistantName.current, recent: history.recent)
        list.updateSections(sections.map { s in
            CPListSection(items: s.items.map { item in
                let row = CPListItem(text: item.text, detailText: item.detail)
                row.handler = { [weak self] _, done in
                    MainActor.assumeIsolated { self?.run(item.action) }
                    done()
                }
                return row
            }, header: s.title, sectionIndexTitle: nil)
        })
    }

    /// CarPlay shows the CallKit call screen itself once the call starts.
    @MainActor private func run(_ action: CarPlayMenu.Action) {
        let calls = CallManager.shared, personas = PersonaModel.shared
        switch action {
        case .call(let id): calls.startCall(persona: personas.persona(id: id) ?? personas.primary)
        case .briefing: calls.startCall(prefix: "briefing", persona: personas.primary)
        case .resume(let id): calls.startCall(resume: history.recent.first { $0.id == id })
        }
    }
}
