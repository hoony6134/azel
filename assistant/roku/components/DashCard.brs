' One card (SPEC.md 5.4): a plain fill, no border, no tint. Colour lives only in the words (run-in head, a deadline's
' figure, a value over its threshold). Padding 24 top, 28 sides; the meta line's bottom sits at y 176.

sub init()
    m.fill = mkPoster(m.top, "pkg:/images/card.9.png", 0, 0, 414, 200, pal("card"))
    m.head = CreateObject("roSGNode", "MultiStyleLabel")
    m.head.translation = [28, 24]
    m.head.wrap = true
    m.head.maxLines = 2
    m.head.lineSpacing = 6
    m.head.drawingStyles = {
        default: {fontUri: "pkg:/fonts/PretendardKR-Regular.otf", fontSize: 30, color: "#ECF0ECFF"}
        crit: {fontUri: "pkg:/fonts/PretendardKR-SemiBold.otf", fontSize: 30, color: "#FF7A6EFF"}
        soon: {fontUri: "pkg:/fonts/PretendardKR-SemiBold.otf", fontSize: 30, color: "#E9BC6EFF"}
    }
    m.top.appendChild(m.head)
    m.big = mkLabel(m.top, 28, 24, 0, 48, 44, "sb", pal("text"))
    m.big.width = 0
    m.tail = mkLabel(m.top, 0, 38, 0, 30, 24, "r", pal("text2"))
    m.tail.width = 0
    m.nowL = mkLabel(m.top, 0, 38, 0, 30, 24, "sb", pal("celadon"))
    m.nowL.width = 0
    m.nowL.text = "지금"
    m.sent = mkLabel(m.top, 28, 24, 358, 80, 30, "r", pal("text"))
    m.sent.wrap = true
    m.sent.maxLines = 2
    m.sent.lineSpacing = 0
    m.sent.ellipsisText = "…"
    m.meta = mkLabel(m.top, 28, 146, 358, 30, 24, "r", pal("text3"))
    m.name = mkLabel(m.top, 28, 24, 212, 40, 30, "sb", pal("text"))
    m.down = mkLabel(m.top, 28, 68, 212, 30, 24, "sb", pal("danger"))
    m.rowL = []
    m.rowV = []
    for i = 0 to 3
        m.rowL.Push(mkLabel(m.top, 28, 68 + i * 27, 120, 30, 24, "r", pal("text2")))
        v = mkLabel(m.top, 28, 68 + i * 27, 212, 30, 24, "sb", pal("text"))
        v.horizAlign = "right"
        m.rowV.Push(v)
    end for
    m.bn = []
    m.bl = []
    m.bm = []
    for i = 0 to 1
        x = 28 + i * 268
        m.bn.Push(mkLabel(m.top, x, 24, 0, 64, 56, "sb", pal("text")))
        m.bn[i].width = 0
        m.bl.Push(mkLabel(m.top, x, 42, 0, 40, 30, "r", pal("text2")))
        m.bl[i].width = 0
        m.bm.Push(mkLabel(m.top, x, 146, 216, 30, 24, "r", pal("text3")))
    end for
    m.all = [m.head, m.big, m.tail, m.nowL, m.sent, m.meta, m.name, m.down]
    m.fillBase = pal("card")
end sub

sub hideAll()
    for each n in m.all
        n.visible = false
    end for
    for i = 0 to 3
        m.rowL[i].visible = false
        m.rowV[i].visible = false
    end for
    for i = 0 to 1
        m.bn[i].visible = false
        m.bl[i].visible = false
        m.bm[i].visible = false
    end for
end sub

sub show(n as Object, x as Integer, y as Integer)
    n.translation = [x, y]
    n.visible = true
end sub

sub setRow(i as Integer, label as String, v as Integer, warnAt as Integer, suffix as String)
    m.rowL[i].text = label
    m.rowL[i].visible = true
    m.rowV[i].visible = true
    if v < 0
        m.rowV[i].text = "—"
        m.rowV[i].color = pal("text2")
        return
    end if
    m.rowV[i].text = v.ToStr() + suffix
    if v >= warnAt then m.rowV[i].color = pal("amber") else m.rowV[i].color = pal("text")
end sub

sub onContent()
    c = m.top.itemContent
    if c = invalid then return
    hideAll()
    w = c.d_w
    if w < 100 then w = 414
    m.fill.width = w
    inner = w - 56
    t = c.d_t
    m.meta.width = inner
    m.meta.text = c.d_meta
    m.sent.width = inner
    m.head.width = inner
    m.head.height = 90
    if t = "urgent"
        s = "<" + c.d_hl + ">" + c.d_head + "</" + c.d_hl + "> " + c.d_sent
        m.head.text = s
        m.head.visible = true
        show(m.meta, 28, 146)
    else if t = "slot"
        m.big.text = c.d_when
        m.big.color = pal("text")
        show(m.big, 28, 24)
        x = 28 + m.big.boundingRect().width + 2
        if c.d_tail <> ""
            m.tail.text = c.d_tail
            show(m.tail, x, 38)
            x = x + m.tail.boundingRect().width + 12
        end if
        if c.d_now then show(m.nowL, x, 38)
        m.sent.text = c.d_sent
        m.sent.height = 80
        show(m.sent, 28, 84)
        show(m.meta, 28, 146)
    else if t = "due"
        m.big.text = c.d_when
        if c.d_soon then m.big.color = pal("amber") else m.big.color = pal("text")
        show(m.big, 28, 24)
        m.tail.text = c.d_tail
        show(m.tail, 28 + m.big.boundingRect().width + 10, 38)
        m.sent.text = c.d_sent
        m.sent.height = 80
        show(m.sent, 28, 84)
        show(m.meta, 28, 146)
    else if t = "node"
        m.name.text = c.d_name
        m.name.color = pal("text")
        show(m.name, 28, 24)
        setRow(0, "CPU", c.d_v1, 85, "%")
        setRow(1, "메모리", c.d_v2, 90, "%")
        setRow(2, "디스크", c.d_v3, 85, "%")
        setRow(3, "온도", c.d_v4, 80, "°")
    else if t = "nodedown"
        m.name.text = c.d_name
        m.name.color = pal("text2")
        show(m.name, 28, 24)
        m.down.text = c.d_down
        show(m.down, 28, 68)
        show(m.meta, 28, 146)
    else if t = "count"
        for i = 0 to 1
            n = c.d_n1
            l = c.d_l1
            mt = c.d_m1
            if i = 1
                n = c.d_n2
                l = c.d_l2
                mt = c.d_m2
            end if
            if n <> ""
                x = 28 + i * 268
                m.bn[i].text = n
                show(m.bn[i], x, 24)
                m.bl[i].text = l
                show(m.bl[i], x + m.bn[i].boundingRect().width + 12, 42)
                m.bm[i].text = mt
                show(m.bm[i], x, 146)
            end if
        end for
        m.meta.visible = false
    else if t = "fail" or t = "empty"
        m.sent.text = c.d_sent
        m.sent.color = pal("text2")
        m.sent.height = 80
        show(m.sent, 28, 24)
        if t = "fail" then show(m.meta, 28, 146)
    else
        ' task, todo: the sentence on the top line, the meta line at the bottom
        m.sent.text = c.d_sent
        m.sent.height = 80
        show(m.sent, 28, 24)
        show(m.meta, 28, 146)
    end if
    if t <> "fail" and t <> "empty" then m.sent.color = pal("text")
end sub

sub onFocus()
    if m.top.focusPercent > 0.5
        m.fill.blendColor = pal("cardFocus")
    else
        m.fill.blendColor = pal("card")
    end if
end sub
