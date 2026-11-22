' One 402x220 card (SPEC.md 5.4). Static children are built once; onContent only sets text, colours and visibility.

sub init()
    m.border = mkPoster(m.top, "pkg:/images/card.9.png", -2, -2, 406, 224, pal("stroke"))
    m.fill = mkPoster(m.top, "pkg:/images/card.9.png", 0, 0, 402, 220, pal("card"))
    m.kind = mkLabel(m.top, 26, 22, 350, 32, 24, "sb", pal("text2"))
    m.kindR = mkLabel(m.top, 26, 22, 350, 32, 24, "r", pal("text2"))
    m.title = mkLabel(m.top, 26, 22, 260, 40, 32, "sb", pal("text"))
    m.title.width = 0 ' auto-size: its bounding rect places the chip
    m.chipB = mkPoster(m.top, "pkg:/images/pill.9.png", 0, 0, 10, 38, pal("celadon"))
    m.chipF = mkPoster(m.top, "pkg:/images/pill.9.png", 0, 0, 10, 34, pal("celadon"))
    m.chipL = mkLabel(m.top, 0, 0, 200, 34, 24, "sb", pal("celadon"))
    m.chipL.vertAlign = "center"
    m.body = mkLabel(m.top, 26, 62, 350, 100, 30, "r", pal("text"))
    m.body.wrap = true
    m.body.maxLines = 2
    m.body.ellipsisText = "…"
    m.bodyS = mkLabel(m.top, 26, 70, 350, 96, 28, "r", pal("text2"))
    m.bodyS.wrap = true
    m.bodyS.maxLines = 2
    m.bodyS.ellipsisText = "…"
    m.big = mkLabel(m.top, 26, 64, 350, 72, 64, "sb", pal("text"))
    m.meta = mkLabel(m.top, 26, 166, 350, 30, 24, "r", pal("text3"))
    m.mLab = []
    m.mVal = []
    m.mTrack = []
    m.mFill = []
    for i = 0 to 3
        x = 26 + (i mod 2) * 190
        y = 72 + Int(i / 2) * 50
        m.mLab.Push(mkLabel(m.top, x, y, 100, 30, 24, "r", pal("text2")))
        v = mkLabel(m.top, x, y, 160, 30, 24, "sb", pal("text"))
        v.horizAlign = "right"
        m.mVal.Push(v)
        m.mTrack.Push(mkRect(m.top, x, y + 34, 160, 6, pal("stroke")))
        m.mFill.Push(mkRect(m.top, x, y + 34, 10, 6, pal("celadon")))
    end for
    m.fillBase = pal("card")
    m.all = [m.kind, m.kindR, m.title, m.chipB, m.chipF, m.chipL, m.body, m.bodyS, m.big, m.meta]
end sub

sub hideAll()
    for each n in m.all
        n.visible = false
    end for
    for i = 0 to 3
        m.mLab[i].visible = false
        m.mVal[i].visible = false
        m.mTrack[i].visible = false
        m.mFill[i].visible = false
    end for
end sub

sub place(n as Object, y as Integer)
    n.translation = [26, y]
    n.visible = true
end sub

sub showChip(text as String, color as String, x as Integer, y as Integer)
    m.chipL.width = 0
    m.chipL.text = text
    m.chipL.visible = true
    w = m.chipL.boundingRect().width + 28
    m.chipL.width = w
    m.chipL.horizAlign = "center"
    m.chipL.translation = [x, y]
    m.chipL.color = color
    m.chipF.width = w
    m.chipF.translation = [x, y]
    m.chipF.blendColor = withAlpha(color, "1F")
    m.chipB.width = w + 4
    m.chipB.translation = [x - 2, y - 2]
    m.chipB.blendColor = withAlpha(color, "99")
    m.chipF.visible = true
    m.chipB.visible = true
end sub

sub setMeter(i as Integer, label as String, v as Integer, warnAt as Integer, suffix as String)
    m.mLab[i].text = label
    m.mLab[i].visible = true
    m.mVal[i].visible = true
    m.mTrack[i].visible = true
    if v < 0
        m.mVal[i].text = "—"
        m.mFill[i].visible = false
        return
    end if
    m.mVal[i].text = v.ToStr() + suffix
    m.mVal[i].color = pal("text")
    w = v
    if w > 100 then w = 100
    m.mFill[i].width = Int(w * 1.6) + 1
    if v >= warnAt
        m.mFill[i].color = pal("amber")
        m.mVal[i].color = pal("amber")
    else
        m.mFill[i].color = pal("celadon")
    end if
    m.mFill[i].visible = true
end sub

sub onContent()
    c = m.top.itemContent
    if c = invalid then return
    hideAll()
    t = c.d_t
    acc = c.d_acc
    fill = pal("card")
    border = pal("stroke")
    if c.d_hot
        if acc = "danger"
            fill = pal("hotDanger")
            border = pal("borderDanger")
        else
            fill = pal("hotAmber")
            border = pal("borderAmber")
        end if
    end if
    m.fillBase = fill
    m.fill.blendColor = fill
    m.border.blendColor = border
    m.border.visible = true
    m.meta.text = c.d_meta
    m.meta.color = pal("text3")
    if t = "node"
        m.title.text = c.d_title
        m.title.color = pal("text")
        place(m.title, 22)
        setMeter(0, "CPU", c.d_m1, 85, "%")
        setMeter(1, "메모리", c.d_m2, 90, "%")
        setMeter(2, "디스크", c.d_m3, 85, "%")
        setMeter(3, "온도", c.d_m4, 80, "°")
    else if t = "nodedown"
        m.title.text = c.d_title
        m.title.color = pal("text2")
        place(m.title, 22)
        showChip(c.d_chip, pal(c.d_chipc), 26 + m.title.boundingRect().width + 16, 24)
        m.bodyS.text = c.d_body
        place(m.bodyS, 70)
        place(m.meta, 166)
    else if t = "task"
        showChip(c.d_chip, pal(c.d_chipc), 26, 22)
        m.body.text = c.d_body
        place(m.body, 70)
        place(m.meta, 166)
    else if t = "count"
        m.kindR.text = c.d_kind
        place(m.kindR, 22)
        m.big.text = c.d_num
        place(m.big, 64)
        place(m.meta, 166)
    else if t = "class" or t = "event" or t = "call"
        m.title.text = c.d_title
        m.title.color = pal("text")
        place(m.title, 22)
        if c.d_chip <> ""
            showChip(c.d_chip, pal(c.d_chipc), 26 + m.title.boundingRect().width + 16, 24)
        end if
        m.body.text = c.d_body
        place(m.body, 70)
        place(m.meta, 166)
    else if t = "fail" or t = "empty"
        m.kind.text = c.d_kind
        m.kind.color = pal("text2")
        place(m.kind, 22)
        m.bodyS.text = c.d_body
        place(m.bodyS, 62)
        place(m.meta, 166)
    else
        ' alert, due, todo: kind line (in its accent), body, meta
        m.kind.text = c.d_kind
        m.kind.color = accentColor(acc, "text2")
        place(m.kind, 22)
        m.body.text = c.d_body
        place(m.body, 62)
        if acc <> ""
            m.meta.color = pal(acc)
        end if
        place(m.meta, 166)
    end if
end sub

sub onFocus()
    if m.top.focusPercent > 0.5
        m.fill.blendColor = pal("cardFocus")
        m.border.visible = false
    else
        m.fill.blendColor = m.fillBase
        m.border.visible = true
    end if
end sub
