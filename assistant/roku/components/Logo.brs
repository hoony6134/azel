' The living logo (SPEC.md 5.1): a porcelain jar that breathes (+-2.5%), sways (+-1.5 degrees) and, every so often,
' catches a drifting highlight. In a call it breathes and shimmers twice as fast and the rim takes the persona's tint.
' `active` = false stops everything (error screen, settings).
sub init()
    m.rim = m.top.findNode("rim")
    m.breath = m.top.findNode("breath")
    m.sway = m.top.findNode("sway")
    m.shim = m.top.findNode("shim")
    m.timer = m.top.findNode("shimT")
    m.timer.observeField("fire", "onShim")
    onMode()
end sub

sub onTint()
    m.rim.blendColor = m.top.tint
end sub

sub onShim()
    m.shim.control = "stop"
    m.shim.control = "start"
end sub

sub onMode()
    m.breath.control = "stop"
    m.sway.control = "stop"
    m.timer.control = "stop"
    if not m.top.active then return
    if m.top.inCall
        m.breath.duration = 2.3
        m.timer.duration = 5
    else
        m.breath.duration = 4.6
        m.timer.duration = 10
    end if
    m.breath.control = "start"
    m.sway.control = "start"
    m.timer.control = "start"
end sub
