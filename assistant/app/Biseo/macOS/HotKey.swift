import Carbon.HIToolbox

/// One global push-to-talk hotkey via Carbon `RegisterEventHotKey`: works inside the App Sandbox and
/// needs no Accessibility permission (unlike an NSEvent global monitor).
enum HotKey {
    struct Combo { let key: Int; let mods: Int; let label: String }

    // shortcut: a fixed list instead of a key recorder; add a recorder if these all clash with something.
    static let presets = [
        Combo(key: kVK_Space, mods: optionKey, label: "⌥Space"),
        Combo(key: kVK_Space, mods: controlKey | optionKey, label: "⌃⌥Space"),
        Combo(key: kVK_Space, mods: cmdKey | shiftKey, label: "⇧⌘Space"),
        Combo(key: kVK_F5, mods: 0, label: "F5"),
    ]

    static var index: Int {
        get { min(UserDefaults.standard.integer(forKey: "hotkey"), presets.count - 1) }
        set { UserDefaults.standard.set(newValue, forKey: "hotkey"); register() }
    }
    static var label: String { presets[index].label }

    static var action: () -> Void = {}
    private static var ref: EventHotKeyRef?
    private static var handler: EventHandlerRef?

    static func register() {
        if handler == nil {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
                DispatchQueue.main.async { HotKey.action() }
                return noErr
            }, 1, &spec, nil, &handler)
        }
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        let combo = presets[index]
        let status = RegisterEventHotKey(UInt32(combo.key), UInt32(combo.mods), EventHotKeyID(signature: 0x4253_454F, id: 1),
                                         GetApplicationEventTarget(), 0, &ref)
        if status != noErr { NSLog("biseo: hotkey %@ is taken (%d)", combo.label, status) }
    }
}
