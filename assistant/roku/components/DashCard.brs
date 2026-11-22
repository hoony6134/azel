' One card (SPEC.md 5.4): a plain fill, no border, no tint. Colour lives only in the words (run-in head, a deadline's
' figure, a value over its threshold). Padding 24 top, 28 sides; the meta line's bottom sits at y 176.

sub init()
    m.fill = mkPoster(m.top, "pkg:/images/card.9.png", 0, 0, 414, 200, pal("card"))
    m.kind = mkLabel(m.top, 28, 24, 358, 30, 24, "sb", pal("danger"))
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
    for i = 0 to 5
        m.rowL.Push(mkLabel(m.top, 28, 68 + i * 27, 100, 30, 24, "r", pal("text2")))
        v = mkLabel(m.top, 28, 68 + i * 27, 172, 30, 24, "sb", pal("text"))
        v.horizAlign = "right"
        m.rowV.Push(v)
    end for
    m.dot = mkLabel(m.top, 28, 25, 0, 30, 24, "sb", pal("text2"))
    m.dot.width = 0
    m.stat = mkLabel(m.top, 28, 28, 358, 30, 24, "r", pal("text3"))
    m.stat.horizAlign = "right"
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
    m.all = [m.kind, m.big, m.tail, m.nowL, m.sent, m.meta, m.name, m.down, m.dot, m.stat]
    m.fillBase = pal("card")
end sub

sub hideAll()
    for each n in m.all
        n.visible = false
    end for
    for i = 0 to 5
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

' figure i of a host card: two columns of three, label on the left, value on the right
sub setFig(i as Integer, label as String, text as String, amber as Boolean)
    x = 28 + Int(i / 3) * 186
    y = 76 + (i mod 3) * 32
    m.rowL[i].translation = [x, y]
    m.rowL[i].text = label
    m.rowL[i].visible = true
    m.rowV[i].translation = [x, y]
    m.rowV[i].text = text
    if amber then m.rowV[i].color = pal("amber") else m.rowV[i].color = pal("text")
    m.rowV[i].visible = true
end sub

function pctText(v as Integer, suffix as String) as String
    if v < 0 then return "—"
    return v.ToStr() + suffix
end function

function loadHot(load as String) as Boolean
    p = Instr(1, load, "/")
    if p < 2 then return false
    cores = Val(Mid(load, p + 1))
    if cores <= 0 then return false
    return Val(Left(load, p - 1)) / cores > 1
end function

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
    m.kind.width = inner
    if t = "urgent"
        ' the kind on its own line in its colour, then the sentence
        m.kind.text = c.d_head
        if c.d_hl = "crit" then m.kind.color = pal("danger") else m.kind.color = pal("amber")
        show(m.kind, 28, 24)
        m.sent.text = c.d_sent
        m.sent.height = 80
        show(m.sent, 28, 58)
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
    else if t = "host"
        st = c.d_status
        m.name.text = c.d_name
        if st = "off" then m.name.color = pal("text2") else m.name.color = pal("text")
        show(m.name, 56, 24)
        ' the status icon: a filled dot, hollow when the host is off; colour only when it needs a look
        m.dot.text = "●"
        dc = pal("text3")
        word = "정상"
        wc = pal("text3")
        if st = "warn"
            dc = pal("amber")
            word = "주의"
            wc = pal("amber")
        else if st = "problem"
            dc = pal("danger")
            word = "문제"
            wc = pal("danger")
        else if st = "off"
            m.dot.text = "○"
            dc = pal("danger")
            word = "꺼짐"
            wc = pal("danger")
        end if
        m.dot.color = dc
        show(m.dot, 28, 25)
        m.stat.text = word
        m.stat.color = wc
        show(m.stat, 28, 28)
        if st = "off"
            show(m.meta, 28, 146)
        else
            setFig(0, "CPU", pctText(c.d_v1, "%"), c.d_v1 >= 85)
            setFig(1, "메모리", pctText(c.d_v2, "%"), c.d_v2 >= 90)
            setFig(2, "디스크", pctText(c.d_v3, "%"), c.d_v3 >= 85)
            setFig(3, "온도", pctText(c.d_v4, "°"), c.d_v4 >= 80)
            lt = c.d_load
            if lt = "" then lt = "—"
            setFig(4, "부하", lt, loadHot(c.d_load))
            ut = c.d_upt
            if ut = "" then ut = "—"
            setFig(5, "가동", ut, false)
        end if
    else if t = "item"
        m.kind.text = c.d_title
        if c.d_soon then m.kind.color = pal("amber") else m.kind.color = pal("text2")
        m.sent.text = c.d_sent
        m.sent.height = 80
        if c.d_title <> ""
            show(m.kind, 28, 24)
            show(m.sent, 28, 58)
        else
            show(m.sent, 28, 24)
        end if
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
