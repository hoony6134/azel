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
        v = mkLabel(m.top, 28, 68 + i * 27, 150, 30, 24, "sb", pal("text"))
        v.horizAlign = "right"
        m.rowV.Push(v)
    end for
    m.barT = []
    m.barF = []
    for i = 0 to 3
        m.barT.Push(mkRect(m.top, 28, 0, 150, 6, pal("rule")))
        m.barF.Push(mkRect(m.top, 28, 0, 10, 6, pal("text2")))
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
    ' charts: 12 bars with every third hour labelled, and a five-point line (rotated thin rectangles) for a stock
    m.bars = []
    for i = 0 to 11
        m.bars.Push(mkRect(m.top, 0, 0, 30, 10, pal("text3")))
    end for
    m.xl = []
    for i = 0 to 11
        l = mkLabel(m.top, 0, 196, 80, 30, 24, "r", pal("text3"))
        l.horizAlign = "center"
        m.xl.Push(l)
    end for
    ' over each bar: its value, and on the temperature chart the hour's weather icon
    m.bv = []
    m.bi = []
    for i = 0 to 11
        l = mkLabel(m.top, 0, 82, 42, 26, 24, "r", pal("text2"))
        l.horizAlign = "center"
        m.bv.Push(l)
        m.bi.Push(mkPoster(m.top, "pkg:/images/wx_clear.png", 0, 64, 32, 32, pal("text2")))
    end for
    m.wxIcon = mkPoster(m.top, "pkg:/images/wx_clear.png", 28, 24, 48, 48, pal("text"))
    m.seg = []
    for i = 0 to 3
        r = mkRect(m.top, 0, 0, 10, 3, pal("text2"))
        r.scaleRotateCenter = [0, 1.5]
        m.seg.Push(r)
    end for
    m.endDot = mkPoster(m.top, "pkg:/images/card.9.png", 0, 0, 10, 10, pal("text"))
    m.tri = mkPoster(m.top, "pkg:/images/tri.png", 0, 0, 16, 14, pal("text2"))
    m.tri.scaleRotateCenter = [8, 7]
    m.all = [m.kind, m.big, m.tail, m.nowL, m.sent, m.meta, m.name, m.down, m.dot, m.stat, m.endDot, m.tri]
    m.all.Append(m.bars)
    m.all.Append(m.xl)
    m.all.Append(m.seg)
    m.all.Append(m.bv)
    m.all.Append(m.bi)
    m.all.Push(m.wxIcon)
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
    for i = 0 to 3
        m.barT[i].visible = false
        m.barF[i].visible = false
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
sub setFig(i as Integer, label as String, text as String, amber as Boolean, pct as Integer)
    ' meter i of a host card: a 2x2 grid; label left, value right, and a thin bar under them
    x = 28 + (i mod 2) * 208
    y = 78 + Int(i / 2) * 52
    m.rowL[i].translation = [x, y]
    m.rowL[i].text = label
    m.rowL[i].visible = true
    m.rowV[i].translation = [x, y]
    m.rowV[i].text = text
    if amber then m.rowV[i].color = pal("amber") else m.rowV[i].color = pal("text")
    m.rowV[i].visible = true
    m.barT[i].translation = [x, y + 34]
    m.barT[i].visible = true
    if pct >= 0
        w = pct
        if w > 100 then w = 100
        m.barF[i].width = Int(w * 1.5) + 1
        m.barF[i].translation = [x, y + 34]
        if amber then m.barF[i].color = pal("amber") else m.barF[i].color = pal("text2")
        m.barF[i].visible = true
    end if
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
    ch = c.d_h
    if ch = invalid or ch < 100 then ch = 200
    m.fill.height = ch
    inner = w - 56
    t = c.d_t
    m.meta.width = inner
    m.meta.text = c.d_meta
    m.sent.width = inner
    m.kind.width = inner
    m.stat.width = inner
    m.name.width = 212
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
        show(m.sent, 28, 74)
        show(m.meta, 28, 150)
    else if t = "due"
        m.big.text = c.d_when
        if c.d_soon then m.big.color = pal("amber") else m.big.color = pal("text")
        show(m.big, 28, 24)
        m.tail.text = c.d_tail
        show(m.tail, 28 + m.big.boundingRect().width + 10, 38)
        m.sent.text = c.d_sent
        m.sent.height = 80
        show(m.sent, 28, 74)
        show(m.meta, 28, 150)
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
            setFig(0, "CPU", pctText(c.d_v1, "%"), c.d_v1 >= 85, c.d_v1)
            setFig(1, "메모리", pctText(c.d_v2, "%"), c.d_v2 >= 90, c.d_v2)
            setFig(2, "디스크", pctText(c.d_v3, "%"), c.d_v3 >= 85, c.d_v3)
            setFig(3, "온도", pctText(c.d_v4, "°"), c.d_v4 >= 80, c.d_v4)
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
    else if t = "wx"
        ' the sky as an icon, now in big figures, the sky word after it; high, low and rain on the second line
        x = 28
        if c.d_icon <> ""
            m.wxIcon.uri = "pkg:/images/wx_" + c.d_icon + ".png"
            show(m.wxIcon, 28, 28)
            x = 28 + 48 + 16
        end if
        m.big.text = c.d_when
        m.big.color = pal("text")
        show(m.big, x, 28)
        m.tail.text = c.d_tail
        show(m.tail, x + m.big.boundingRect().width + 14, 42)
        m.sent.text = c.d_sent
        if c.d_soon then m.sent.color = pal("amber") else m.sent.color = pal("text")
        m.sent.height = 40
        show(m.sent, 28, 102)
        show(m.meta, 28, ch - 54)
    else if t = "chart"
        m.kind.text = c.d_title
        m.kind.color = pal("text2")
        show(m.kind, 28, 24)
        m.stat.text = c.d_tail
        m.stat.color = pal("text3")
        m.stat.width = inner
        show(m.stat, 28, 24)
        drawBars(c.d_series, c.d_labels, c.d_unit, inner, c.d_icons)
    else if t = "stock"
        m.name.text = c.d_name
        m.name.width = inner
        m.name.color = pal("text")
        ' name, price, change, then the source at the bottom; the 5-day line sits right of the price with a margin
        show(m.name, 28, 26)
        if c.d_status = "unlisted" or c.d_status = "noprice"
            m.sent.text = c.d_sent
            m.sent.color = pal("text3")
            m.sent.height = 40
            show(m.sent, 28, 78)
            return
        end if
        m.big.text = c.d_when
        m.big.color = pal("text")
        show(m.big, 28, 70)
        m.tail.text = c.d_tail
        ' Korean market colours: up red, down blue; the arrow says it too
        col = pal("text2")
        if c.d_hl = "up" then col = "0xFF7A6EFF"
        if c.d_hl = "down" then col = "0x8FB1F0FF"
        m.tail.color = col
        tx = 28
        if c.d_hl = "up" or c.d_hl = "down"  ' the arrow is drawn (the font has no ▲/▼), so the figure never shifts
            m.tri.blendColor = col
            m.tri.rotation = 0
            if c.d_hl = "down" then m.tri.rotation = 3.14159
            show(m.tri, 28, 150)
            tx = 52
        end if
        show(m.tail, tx, 142)
        show(m.meta, 28, ch - 46)
        drawSpark(c.d_series, w - 36 - 96, 84, 96, 52, col)
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

' 12 bars across the card: temperature scaled between its own low and high (never flat), percentages from 0 to 100
sub drawBars(series as Object, labels as Object, unit as String, inner as Integer, icons as Object)
    n = series.Count()
    if n = 0 then return
    if n > 12 then n = 12
    ' a column per value: icon (y 60), value (y 98), bar (128-178), hour (y 188), each centred in its column
    stp = Int(inner / n)
    bw = 36
    if bw > stp - 16 then bw = stp - 16
    top = 128
    h = 50
    lo = 1000.0
    hi = -1000.0
    for i = 0 to n - 1
        v = series[i]
        if v <> invalid
            if v < lo then lo = v
            if v > hi then hi = v
        end if
    end for
    if unit = "%"
        lo = 0
        hi = 100
    end if
    span = hi - lo
    if span <= 0 then span = 1
    li = 0
    for i = 0 to n - 1
        v = series[i]
        if v = invalid then v = lo
        bh = Int(8 + (v - lo) / span * (h - 8))
        b = m.bars[i]
        b.width = bw
        b.height = bh
        cx = 28 + i * stp + Int(stp / 2)
        b.translation = [cx - Int(bw / 2), top + h - bh]
        b.color = pal("text3")
        if unit = "%" and v >= 60 then b.color = pal("amber")
        if unit <> "%" and i = 0 then b.color = pal("text2")
        b.visible = true
        ' the value over the bar ("15°", "60"), centred on it
        vl = m.bv[i]
        if unit = "%" then vl.text = Int(v).ToStr() else vl.text = degShort(v)
        vl.width = stp
        vl.translation = [28 + i * stp, 98]
        vl.color = pal("text2")
        if unit = "%" and v >= 60 then vl.color = pal("amber")
        vl.visible = true
        if i < icons.Count() and icons[i] <> ""
            ic = m.bi[i]
            ic.uri = "pkg:/images/wx_" + icons[i] + ".png"
            ic.translation = [cx - 16, 60]
            ic.visible = true
        end if
        if i < labels.Count()
            l = m.xl[li]
            l.text = labels[i] + "시"
            l.width = stp
            l.translation = [28 + i * stp, 188]
            l.visible = true
            li = li + 1
        end if
    end for
end sub

function degShort(v as Float) as String
    n = Int(v + 0.5)
    if v < 0 then n = -Int(-v + 0.5)
    return n.ToStr() + "°"
end function

' a five-point line in the box (x, y, w, h): each step a thin rectangle rotated to meet the next point
sub drawSpark(series as Object, x as Integer, y as Integer, w as Integer, h as Integer, color as String)
    n = series.Count()
    if n < 2 then return
    lo = series[0]
    hi = series[0]
    for each v in series
        if v < lo then lo = v
        if v > hi then hi = v
    end for
    span = hi - lo
    if span <= 0 then span = 1
    pts = []
    for i = 0 to n - 1
        px = x + i * w / (n - 1)
        py = y + h - (series[i] - lo) / span * h
        pts.Push([px, py])
    end for
    for i = 0 to n - 2
        if i > 3 then exit for
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        r = m.seg[i]
        r.width = Sqr(dx * dx + dy * dy) + 1
        r.translation = [pts[i][0], pts[i][1] - 1.5]
        r.rotation = -Atn(dy / dx)
        r.color = color
        r.visible = true
    end for
    last = pts[n - 1]
    m.endDot.translation = [last[0] - 5, last[1] - 5]
    m.endDot.blendColor = color
    m.endDot.visible = true
end sub

sub onFocus()
    if m.top.focusPercent > 0.5
        m.fill.blendColor = pal("cardFocus")
    else
        m.fill.blendColor = pal("card")
    end if
end sub
