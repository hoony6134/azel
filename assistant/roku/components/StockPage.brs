' The stock page (StockPage.xml). The chart is drawn like the dashboard's bars: a pool of Rectangles, one column per
' point (at most 150): a faint area column under a 4 px line piece that joins this point to the next, so the line has no
' gaps. The previous close (1일) is a row of small dashes. Up is red, down is blue (Korean markets).

sub init()
    m.ranges = ["1d", "7d", "1mo", "3mo", "1y"]
    m.names = ["1일", "7일", "30일", "3개월", "1년"]
    m.idx = 0
    m.task = invalid
    m.shown = false
    m.px = 96
    m.py = 404
    m.pw = 1560
    m.ph = 440
    m.name = mkLabel(m.top, 96, 138, 1000, 56, 44, "sb", pal("text"))
    m.tag = mkLabel(m.top, 96, 198, 1000, 30, 24, "r", pal("text2"))
    m.price = mkLabel(m.top, 96, 238, 800, 90, 72, "sb", pal("text"))
    m.chg = mkLabel(m.top, 96, 328, 800, 44, 34, "sb", pal("text"))
    m.fl = []
    m.fv = []
    for i = 0 to 2
        m.fl.Push(mkLabel(m.top, 1180, 246 + i * 44, 200, 36, 26, "r", pal("text3")))
        v = mkLabel(m.top, 1390, 246 + i * 44, 434, 36, 28, "sb", pal("text"))
        v.horizAlign = "right"
        m.fv.Push(v)
    end for
    m.cols = []
    m.lines = []
    for i = 0 to 149
        m.cols.Push(mkRect(m.top, 0, 0, 8, 2, pal("text")))
        m.lines.Push(mkRect(m.top, 0, 0, 8, 4, pal("text")))
    end for
    m.dash = []
    for i = 0 to 97
        m.dash.Push(mkRect(m.top, m.px + i * 16, 0, 9, 2, pal("text3")))
    end for
    m.hi = mkLabel(m.top, m.px + m.pw + 16, m.py - 14, 160, 28, 22, "r", pal("text3"))
    m.lo = mkLabel(m.top, m.px + m.pw + 16, m.py + m.ph - 14, 160, 28, 22, "r", pal("text3"))
    m.bl = mkLabel(m.top, m.px + m.pw + 16, m.py, 160, 28, 22, "r", pal("text3"))
    m.t0 = mkLabel(m.top, m.px, m.py + m.ph + 12, 400, 28, 22, "r", pal("text3"))
    m.t1 = mkLabel(m.top, m.px + m.pw - 400, m.py + m.ph + 12, 400, 28, 22, "r", pal("text3"))
    m.t1.horizAlign = "right"
    m.msg = mkLabel(m.top, m.px, m.py + 180, m.pw, 40, 30, "r", pal("text2"))
    m.msg.horizAlign = "center"
    m.pills = []
    m.pillT = []
    for i = 0 to 4
        m.pills.Push(mkPoster(m.top, "pkg:/images/card.9.png", 96 + i * 150, 924, 134, 56, pal("raised")))
        t = mkLabel(m.top, 96 + i * 150, 924, 134, 56, 28, "sb", pal("text2"))
        t.horizAlign = "center"
        t.vertAlign = "center"
        m.pillT.Push(t)
        m.pillT[i].text = m.names[i]
    end for
    m.hint = mkLabel(m.top, 96, 996, 1200, 30, 24, "r", pal("text3"))
    m.hint.text = "뒤로  닫기   ·   좌우  기간   ·   되감기  새로고침"
    m.stamp = mkLabel(m.top, 1324, 996, 500, 30, 24, "r", pal("text3"))
    m.stamp.horizAlign = "right"
    m.live = m.top.findNode("live")
    m.live.observeField("fire", "onLive")
    m.top.observeField("visible", "onVisible")
end sub

sub onOpen()
    m.idx = 0
    m.shown = false
    m.name.text = m.top.title
    m.tag.text = m.top.symbol
    clearAll()
    drawPills()
    m.top.setFocus(true)
    fetch()
end sub

sub onVisible()
    if m.top.visible
        if m.shown then m.live.control = "start"
    else
        m.live.control = "stop"
        cancel()
    end if
end sub

sub cancel()
    if m.task <> invalid then m.task.unobserveField("result")
    m.task = invalid
end sub

sub fetch()
    cancel()
    t = CreateObject("roSGNode", "FetchTask")
    t.baseUrl = m.top.baseUrl
    t.token = m.top.token
    t.path = "/v1/tv?detail=stock&symbol=" + m.top.symbol.EncodeUriComponent() + "&range=" + m.ranges[m.idx]
    t.observeField("result", "onResult")
    m.task = t
    t.control = "RUN"
end sub

sub onResult(ev as Object)
    r = ev.getData()
    m.task = invalid
    if r.ok and r.data.DoesExist("points")
        m.shown = true
        draw(r.data)
        if m.top.visible then m.live.control = "start"
    else if not m.shown
        showMsg("불러오지 못했어요. 되감기 키로 다시 불러올 수 있어요.")
    end if
end sub

' the page refreshes itself live on 1일 only; the other ranges do not move within a minute
sub onLive()
    if m.idx = 0 and m.task = invalid then fetch()
end sub

sub showMsg(t as String)
    clearChart()
    m.msg.text = t
    m.msg.visible = true
end sub

sub clearChart()
    for i = 0 to 149
        m.cols[i].visible = false
        m.lines[i].visible = false
    end for
    for each d in m.dash
        d.visible = false
    end for
    m.hi.text = ""
    m.lo.text = ""
    m.bl.text = ""
    m.t0.text = ""
    m.t1.text = ""
end sub

sub clearAll()
    clearChart()
    m.price.text = ""
    m.chg.text = ""
    for i = 0 to 2
        m.fl[i].text = ""
        m.fv[i].text = ""
    end for
    m.stamp.text = ""
    showMsg("불러오는 중이에요")
end sub

sub drawPills()
    for i = 0 to 4
        if i = m.idx
            m.pills[i].blendColor = pal("celadon")
            m.pillT[i].color = "0x0D1113FF"
        else
            m.pills[i].blendColor = pal("raised")
            m.pillT[i].color = pal("text2")
        end if
    end for
end sub

' ---------- drawing ----------

function upColor() as String
    return "0xFF5C5CFF"
end function

function downColor() as String
    return "0x5C9BFFFF"
end function

function stateWord(s as String) as String
    w = {open: "장중", closed: "장 마감", pre: "프리마켓", post: "애프터마켓"}
    if w[s] <> invalid then return w[s]
    return ""
end function

function volText(v as Dynamic) as String
    f = Cdbl(v)
    if f >= 100000000 then return fmt2(f / 100000000) + "억"
    if f >= 10000 then return Int(f / 10000).ToStr() + "만"
    return Int(f).ToStr()
end function

sub draw(d as Object)
    m.msg.visible = false
    cur = sv(d.currency)
    m.name.text = sv(d.name)
    if m.top.title <> "" then m.name.text = m.top.title
    tag = sv(d.symbol)
    if stateWord(sv(d.state)) <> "" then tag = tag + "  ·  " + stateWord(sv(d.state))
    m.tag.text = tag
    if d.price <> invalid then m.price.text = money(d.price, cur) else m.price.text = ""
    col = pal("text")
    m.chg.text = ""
    if d.pct <> invalid and d.change <> invalid
        sign = ""
        if d.pct > 0
            col = upColor()
            sign = "+"
        else if d.pct < 0
            col = downColor()
            sign = "-"
        end if
        ch = d.change
        if ch < 0 then ch = -ch
        p = d.pct
        if p < 0 then p = -p
        m.chg.text = sign + plainNum(ch, cur) + "  (" + sign + fmt2(p) + "%)"
    end if
    m.chg.color = col
    ' day high / low / volume: absent for an index (the level only)
    for i = 0 to 2
        m.fl[i].text = ""
        m.fv[i].text = ""
    end for
    if d.high <> invalid and d.low <> invalid and d.volume <> invalid
        m.fl[0].text = "고가"
        m.fv[0].text = money(d.high, cur)
        m.fl[1].text = "저가"
        m.fv[1].text = money(d.low, cur)
        m.fl[2].text = "거래량"
        m.fv[2].text = volText(d.volume)
    end if
    m.stamp.text = "기준 " + fmtHM(isoEpoch(d.at))
    drawChart(arr(d.points), d.base, col, cur)
end sub

' a price without the currency sign for the change figure: 1,200 / 4.37
function plainNum(v as Float, cur as String) as String
    if cur = "KRW" or cur = "JPY" then return grouped(Int(v + 0.5).ToStr())
    return grouped(fmt2(v))
end function

sub drawChart(pts as Object, base as Dynamic, col as String, cur as String)
    clearChart()
    n = pts.Count()
    if n < 2
        showMsg("차트 데이터가 없어요")
        return
    end if
    ' what the line is colored against: the baseline (previous close on 1일, else the range's first close)
    last = pts[n - 1][1]
    if base <> invalid
        if last > base
            col = upColor()
        else if last < base
            col = downColor()
        end if
    end if
    cn = 150  ' always 150 columns, the series interpolated, so a 79-point day is as smooth as a 200-point year
    lo = pts[0][1]
    hi = lo
    for each p in pts
        if p[1] < lo then lo = p[1]
        if p[1] > hi then hi = p[1]
    end for
    if base <> invalid
        if base < lo then lo = base
        if base > hi then hi = base
    end if
    span = hi - lo
    if span <= 0 then span = 1
    ' a 6 % pad so the line never touches the frame
    pad = span * 0.06
    lo = lo - pad
    span = span + pad * 2
    cw = m.pw / cn
    area = dimmed(col)
    prevY = invalid
    ys = []
    for i = 0 to cn - 1
        f = i * (n - 1) / (cn - 1)
        a = Int(f)
        if a >= n - 1 then a = n - 2
        v = pts[a][1] + (pts[a + 1][1] - pts[a][1]) * (f - a)
        ys.Push(m.py + Int(m.ph - (v - lo) / span * m.ph))
    end for
    for i = 0 to cn - 1
        y = ys[i]
        y2 = y
        if i < cn - 1 then y2 = ys[i + 1]
        x = m.px + Int(i * cw)
        w = Int((i + 1) * cw) - Int(i * cw)
        if w < 1 then w = 1
        top = y
        if y2 < top then top = y2
        h = y - y2
        if h < 0 then h = -h
        c = m.cols[i]
        c.translation = [x, y + 2]
        c.width = w + 1  ' overlaps its neighbour: a solid tint, so no seam shows
        c.height = m.py + m.ph - y - 2
        if c.height < 1 then c.height = 1
        c.color = area
        c.visible = true
        l = m.lines[i]
        l.translation = [x, top]
        l.width = w
        l.height = h + 4
        l.color = col
        l.visible = true
    end for
    m.hi.text = plainNum(hi - pad, cur)
    m.lo.text = plainNum(lo + pad, cur)
    if m.idx = 0 and base <> invalid
        by = m.py + Int(m.ph - (base - lo) / span * m.ph)
        for each dd in m.dash
            dd.translation = [dd.translation[0], by]
            dd.visible = true
        end for
        m.bl.translation = [m.px + m.pw + 16, by - 14]
        m.bl.text = plainNum(base, cur)
        ' do not print over the high/low labels
        if by < m.py + 30 then m.bl.text = ""
        if by > m.py + m.ph - 30 then m.bl.text = ""
    end if
    m.t0.text = axisTime(pts[0][0])
    m.t1.text = axisTime(pts[n - 1][0])
end sub

' the line's color at about a fifth over the page background, solid (so overlapping columns leave no seam)
function dimmed(col as String) as String
    out = "0x"
    bg = [13, 17, 19]
    for i = 0 to 2
        v = Val(Mid(col, 3 + i * 2, 2), 16)
        out = out + Right("0" + StrI(Int(bg[i] + (v - bg[i]) * 0.22), 16), 2)
    end for
    return out + "FF"
end function

function axisTime(t as Integer) as String
    if m.idx <= 1 then return dateShort(t) + " " + fmtHM(t)
    return dateShort(t)
end function

function dateShort(t as Integer) as String
    dt = localDt(t)
    return dt.GetMonth().ToStr() + "월 " + dt.GetDayOfMonth().ToStr() + "일"
end function

' ---------- input ----------

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    if key = "back"
        m.top.closed = true
        return true
    else if key = "left" or key = "right"
        if key = "left" and m.idx > 0
            m.idx = m.idx - 1
        else if key = "right" and m.idx < 4
            m.idx = m.idx + 1
        else
            return true
        end if
        drawPills()
        cancel()
        showMsg("불러오는 중이에요")
        fetch()
        return true
    else if key = "replay"
        fetch()
        return true
    else if key = "up" or key = "down" or key = "OK"
        return true
    end if
    return false
end function
