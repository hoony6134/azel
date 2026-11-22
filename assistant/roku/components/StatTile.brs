' One figure of the summary strip (SPEC.md 5.2): label, a big number, and a tail on the number's baseline.
' No box: the strip's hairline rules sit around it.
sub init()
    m.k = mkLabel(m.top, 0, 18, 380, 30, 24, "r", pal("text2"))
    m.v = mkLabel(m.top, 0, 54, 0, 64, 56, "sb", pal("text"))
    m.v.width = 0 ' auto-size, so boundingRect() is the text
    m.tl = mkLabel(m.top, 0, 72, 300, 40, 30, "r", pal("text2"))
end sub

sub onData()
    d = m.top.data
    if d = invalid then return
    m.k.text = d.k
    m.v.text = d.v
    m.tl.text = d.t
    m.v.color = pal("text")
    if d.acc <> "" then m.v.color = pal(d.acc)
    x = m.v.boundingRect().width + 14
    m.tl.translation = [x, 72]
    avail = m.top.colW - 24 - x
    if avail < 40 then avail = 40
    m.tl.width = avail
end sub
