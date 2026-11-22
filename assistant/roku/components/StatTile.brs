' One of the four summary tiles, 414x136 (SPEC.md 5.2): label, value, then a sub text right after the value.
sub init()
    m.border = mkPoster(m.top, "pkg:/images/card.9.png", -2, -2, 418, 140, pal("stroke"))
    m.fill = mkPoster(m.top, "pkg:/images/card.9.png", 0, 0, 414, 136, pal("card"))
    m.k = mkLabel(m.top, 26, 18, 360, 32, 26, "r", pal("text2"))
    m.v = mkLabel(m.top, 26, 60, 240, 56, 44, "sb", pal("text"))
    m.v.width = 0 ' auto-size, so boundingRect() is the text
    m.subL = mkLabel(m.top, 200, 72, 190, 36, 28, "r", pal("text2"))
end sub

sub onData()
    d = m.top.data
    if d = invalid then return
    m.k.text = d.k
    m.v.text = d.v
    m.subL.text = d.s2
    fill = pal("card")
    border = pal("stroke")
    m.v.color = pal("text")
    if d.hot
        m.v.color = pal(d.acc)
        if d.acc = "danger"
            fill = pal("hotDanger")
            border = pal("borderDanger")
        else
            fill = pal("hotAmber")
            border = pal("borderAmber")
        end if
    end if
    m.fill.blendColor = fill
    m.border.blendColor = border
    m.subL.translation = [26 + m.v.boundingRect().width + 10, 72]
end sub
