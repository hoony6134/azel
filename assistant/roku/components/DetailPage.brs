' The detail page (DetailPage.xml). A flat list of lines (host headers and services, or section headers and tasks)
' drawn into a fixed pool of row slots, so 60 services cost the same nodes as 14. ↑↓ move the focus ring line by line,
' Back closes, ⟲ (replay) asks the scene to fetch again. Nothing here talks to the network.

sub init()
    m.lines = []
    m.focus = 0
    m.offset = 0
    m.pinned = false
    m.title = mkLabel(m.top, 96, 146, 1000, 56, 44, "sb", pal("text"))
    m.sub = mkLabel(m.top, 824, 166, 1000, 30, 24, "r", pal("text2"))
    m.sub.horizAlign = "right"
    m.summary = mkLabel(m.top, 96, 214, 1728, 40, 30, "r", pal("text"))
    m.cols = createObject("roSGNode", "Group")
    m.top.appendChild(m.cols)
    m.rule = mkPoster(m.top, "pkg:/images/rule.png", 96, 316, 1728, 2, pal("rule"))
    m.list = createObject("roSGNode", "Group")
    m.list.translation = [0, 326]
    m.top.appendChild(m.list)
    m.msg = mkLabel(m.top, 96, 360, 1728, 40, 30, "r", pal("text2"))
    m.hint = mkLabel(m.top, 96, 996, 1200, 30, 24, "r", pal("text3"))
    m.hint.text = "뒤로  닫기   ·   위아래  이동   ·   되감기  새로고침"
    m.pos = mkLabel(m.top, 1324, 996, 500, 30, 24, "r", pal("text3"))
    m.pos.horizAlign = "right"
    m.slots = []
end sub

' ---------- slots ----------

' one row slot: focus fill + ring, a status dot and the labels both page kinds need
function newSlot(h as Integer) as Object
    g = createObject("roSGNode", "Group")
    m.list.appendChild(g)
    s = {g: g, h: h}
    ' the ring sits 24 px outside the text edge (x 96), so a focused row reads as the same column, framed
    s.fill = mkPoster(g, "pkg:/images/card.9.png", 72, 0, 1776, h - 8, pal("cardFocus"))
    s.ring = mkPoster(g, "pkg:/images/focus.9.png", 72, 0, 1776, h - 8, pal("text"))
    s.dot = mkPoster(g, "pkg:/images/card.9.png", 104, Int((h - 8) / 2) - 7, 14, 14, pal("celadon"))
    s.a = mkLabel(g, 96, 0, 700, h - 8, 30, "sb", pal("text"))
    s.a.vertAlign = "center"
    s.b = mkLabel(g, 0, 0, 600, h - 8, 24, "r", pal("text2"))
    s.b.vertAlign = "center"
    s.c = []
    for i = 0 to 4
        l = mkLabel(g, 0, 0, 100, h - 8, 24, "r", pal("text2"))
        l.vertAlign = "center"
        s.c.Push(l)
    end for
    s.right = createObject("roSGNode", "MultiStyleLabel")
    s.right.translation = [824, 0]
    s.right.width = 1000
    s.right.height = h - 8
    s.right.horizAlign = "right"
    s.right.vertAlign = "center"
    s.right.drawingStyles = styles()
    g.appendChild(s.right)
    ' host rows: CPU and memory over the last 24 h as two rows of 8 small bars; service rows: a 24 h uptime bar
    s.hl = []
    s.hb = []
    for k = 0 to 1
        l = mkLabel(g, 0, 0, 80, h - 8, 24, "r", pal("text3"))
        l.vertAlign = "center"
        s.hl.Push(l)
        for i = 0 to 7
            s.hb.Push(mkRect(g, 0, 0, 6, 4, pal("text3")))
        end for
    end for
    s.ut = mkRect(g, 1066, Int((h - 8) / 2) - 4, 56, 8, pal("rule"))
    s.uf = mkRect(g, 1066, Int((h - 8) / 2) - 4, 10, 8, pal("text2"))
    s.meta = createObject("roSGNode", "MultiStyleLabel")
    s.meta.translation = [112, 50]
    s.meta.width = 1700
    s.meta.height = 30
    s.meta.drawingStyles = styles()
    g.appendChild(s.meta)
    return s
end function

function styles() as Object
    r = "pkg:/fonts/PretendardKR-Regular.otf"
    b = "pkg:/fonts/PretendardKR-SemiBold.otf"
    return {
        default: {fontUri: r, fontSize: 24, color: "#A9B4B0FF"}
        dim: {fontUri: r, fontSize: 24, color: "#8A9692FF"}
        amb: {fontUri: b, fontSize: 24, color: "#E9BC6EFF"}
        bad: {fontUri: b, fontSize: 24, color: "#FF7A6EFF"}
        ok: {fontUri: b, fontSize: 24, color: "#82CBB4FF"}
        run: {fontUri: b, fontSize: 24, color: "#ECF0ECFF"}
    }
end function

sub makeSlots(n as Integer, h as Integer)
    for each s in m.slots
        m.list.removeChild(s.g)
    end for
    m.slots = []
    for i = 0 to n - 1
        s = newSlot(h)
        s.g.translation = [0, i * h]
        m.slots.Push(s)
    end for
end sub

' ---------- render ----------

sub render()
    k = m.top.kind
    m.title.text = "인프라"
    if k = "tasks" then m.title.text = "Azel 작업"
    clearCols()
    m.lines = []
    m.summary.text = ""
    m.sub.text = ""
    m.msg.visible = false
    if m.top.state = "loading"
        showMsg("불러오는 중이에요")
    else if m.top.state = "error"
        showMsg("불러오지 못했어요. 되감기 키로 다시 불러올 수 있어요.")
    else if m.top.data <> invalid
        if k = "tasks" then buildTasks(m.top.data) else buildInfra(m.top.data)
    end if
    m.rule.visible = m.lines.Count() > 0
    if m.lines.Count() = 0 and m.top.state = "ok" and not m.msg.visible then showMsg("보여 줄 내용이 없어요")
    m.focus = firstFocus()
    m.offset = 0
    m.pinned = false
    ' opened from a host card: that host's header is the top line, as if the page scrolled to it
    if m.top.focusName <> "" and m.focus < m.lines.Count() and m.lines[m.focus].t = "host" and m.lines[m.focus].name = m.top.focusName
        m.offset = m.focus  ' even near the end: the host on top, the rows under it empty
        m.pinned = true
    end if
    draw()
end sub

sub showMsg(t as String)
    m.msg.text = t
    m.msg.visible = true
    for each s in m.slots
        s.g.visible = false
    end for
end sub

sub clearCols()
    while m.cols.getChildCount() > 0
        m.cols.removeChildIndex(0)
    end while
end sub

function firstFocus() as Integer
    want = m.top.focusName
    first = -1
    for i = 0 to m.lines.Count() - 1
        ln = m.lines[i]
        if ln.f
            if first < 0 then first = i
            if want <> "" and ln.t = "host" and ln.name = want then return i
        end if
    end for
    if want = ""
        for i = 0 to m.lines.Count() - 1
            if m.lines[i].t = "svc" and m.lines[i].bad then return i
        end for
    end if
    if first < 0 then return 0
    return first
end function

' ---------- infra ----------

sub buildInfra(d as Object)
    makeSlotsFor("infra")
    hosts = arr(d.hosts)
    groups = arr(d.groups)
    if d.hosts = invalid and d.groups = invalid
        showMsg("지금은 Cygnus를 볼 수 없어요")
        return
    end if
    nSvc = 0
    nBad = 0
    for each g in groups
        for each s in arr(g.services)
            nSvc = nSvc + 1
            if sv(s.state) <> "up" then nBad = nBad + 1
        end for
    end for
    nOff = 0
    for each h in hosts
        if sv(h.status) = "off" then nOff = nOff + 1
    end for
    t = "노드 " + hosts.Count().ToStr() + "대 "
    if nOff = 0 then t = t + "모두 켜짐" else t = t + "중 " + nOff.ToStr() + "대 응답 없음"
    t = t + "  ·  서비스 " + nSvc.ToStr() + "개 "
    if nBad = 0 then t = t + "모두 정상" else t = t + "중 " + nBad.ToStr() + "개 문제"
    na = arr(d.alerts).Count()
    if na = 0 then t = t + "  ·  경보 없음" else t = t + "  ·  경보 " + na.ToStr() + "개"
    m.summary.text = t
    if sv(d.asof) <> "" then m.sub.text = sv(d.asof) + " 기준" else m.sub.text = ""
    colHead(["서비스", "종류", "상태", "24시간", "응답", "마지막 장애"])
    byNode = {}
    for each h in hosts
        byNode[sv(h.name)] = h
    end for
    for each g in groups
        if m.lines.Count() > 0 then m.lines.Push({t: "gap", f: false})
        m.lines.Push({t: "host", f: true, name: sv(g.node), role: sv(g.role), h: byNode[sv(g.node)]})
        for each s in arr(g.services)
            st = sv(s.state)
            m.lines.Push({t: "svc", f: true, s: s, bad: st = "down" or st = "unknown"})
        end for
    end for
    ' hosts with no monitors still get their line
    for each h in hosts
        found = false
        for each g in groups
            if sv(g.node) = sv(h.name) then found = true
        end for
        if not found
            m.lines.Push({t: "gap", f: false})
            m.lines.Push({t: "host", f: true, name: sv(h.name), role: sv(h.role), h: h})
        end if
    end for
end sub

sub makeSlotsFor(kind as String)
    if kind = "infra" and (m.slots.Count() <> 12) then makeSlots(12, 52)  ' ends above the key hint
    if kind = "tasks" and (m.slots.Count() <> 6) then makeSlots(6, 104)  ' ends above the key hint
    ' no column header on the task page: its list starts right under the summary
    y = 316
    if kind = "tasks" then y = 276
    m.rule.translation = [96, y]
    m.list.translation = [0, y + 10]
end sub

' column x positions of a service line (SPEC: the status page's columns, on the 1920 grid)
function svcX() as Object
    return [{x: 136, w: 640}, {x: 796, w: 130}, {x: 936, w: 120}, {x: 1066, w: 150}, {x: 1236, w: 130}, {x: 1400, w: 424}]
end function

sub colHead(names as Object)
    xs = svcX()
    for i = 0 to names.Count() - 1
        l = mkLabel(m.cols, xs[i].x, 276, xs[i].w, 30, 24, "r", pal("text3"))
        l.text = names[i]
        if i = 3 or i = 4 then l.horizAlign = "right"
    end for
end sub

sub drawInfraLine(s as Object, ln as Object)
    for each l in s.c
        l.text = ""
    end for
    s.right.text = ""
    s.meta.text = ""
    s.b.text = ""
    s.dot.visible = false
    hideCharts(s)
    if ln.t = "gap"
        s.a.text = ""
        return
    end if
    if ln.t = "host"
        s.a.translation = [96, 0]
        s.a.width = 0
        s.a.font = mkFont("sb", 30)
        s.a.color = pal("text")
        s.a.text = ln.name
        s.b.translation = [96 + s.a.boundingRect().width + 18, 2]
        s.b.text = ln.role
        s.right.text = hostStats(ln.h)
        if ln.h <> invalid
            spark(s, 0, 680, "CPU", ln.h.cpu_24h, 85)
            spark(s, 1, 860, "메모리", ln.h.mem_24h, 90)
        end if
        return
    end if
    sv_ = ln.s
    xs = svcX()
    st = sv(sv_.state)
    s.dot.visible = true
    s.dot.blendColor = stateColor(st)
    s.a.translation = [xs[0].x, 0]
    s.a.width = xs[0].w
    s.a.font = mkFont("r", 30)
    s.a.color = pal("text")
    s.a.text = sv(sv_.name)
    put(s.c[0], xs[1], sv(sv_.type), pal("text3"), "r", "left")
    put(s.c[1], xs[2], stateWord(st), stateText(st), weightOf(st), "left")
    up = sv_.uptime
    if up <> invalid
        col = pal("text2")
        if up < 99.5 then col = pal("amber")
        if up < 95 then col = pal("danger")
        put(s.c[2], xs[3], fmtPct(up), col, "r", "right")
        if up < 99.9  ' only a flaky or down service gets its bar: a full one says nothing
            s.ut.visible = true
            s.uf.width = Int(56 * up / 100) + 1
            s.uf.color = col
            s.uf.visible = true
        end if
    end if
    if sv_.ms <> invalid
        col = pal("text2")
        if sv_.ms >= 500 then col = pal("amber")
        put(s.c[3], xs[4], Int(sv_.ms).ToStr() + "ms", col, "r", "right")
    end if
    if sv(sv_.last_down) <> "" then put(s.c[4], xs[5], koStamp(sv(sv_.last_down)), pal("text3"), "r", "left")
end sub

sub put(l as Object, x as Object, t as String, col as String, w as String, align as String)
    l.translation = [x.x, 0]
    l.width = x.w
    l.font = mkFont(w, 24)
    l.color = col
    l.horizAlign = align
    l.text = t
end sub

sub hideCharts(s as Object)
    for each n in s.hb
        n.visible = false
    end for
    for each n in s.hl
        n.visible = false
    end for
    s.ut.visible = false
    s.uf.visible = false
end sub

' 8 bars (21 h ago … now) for one metric of a host row, label first; the latest bar brighter, over-threshold amber
sub spark(s as Object, k as Integer, x as Integer, label as String, series as Dynamic, warn as Integer)
    if Type(series) <> "roArray" or series.Count() = 0 then return
    l = s.hl[k]
    l.translation = [x, 0]
    l.text = label
    l.width = 0
    l.visible = true
    bx = x + l.boundingRect().width + 10
    cy = Int((s.h - 8) / 2)
    for i = 0 to 7
        b = s.hb[k * 8 + i]
        v = invalid
        if i < series.Count() then v = series[i]
        if v <> invalid
            bh = Int(3 + v / 100 * 25)
            b.height = bh
            b.translation = [bx + i * 9, cy + 13 - bh]
            b.color = pal("text3")
            if i = 7 then b.color = pal("text2")
            if v >= warn then b.color = pal("amber")
            b.visible = true
        end if
    end for
end sub

function hostStats(h as Dynamic) as String
    if h = invalid then return ""
    if sv(h.status) = "off" then return "<bad>응답 없음</bad>"
    parts = []
    parts.Push(metric("CPU", h.cpu, 85, "%"))
    parts.Push(metric("메모리", h.mem, 90, "%"))
    parts.Push(metric("디스크", h.disk, 85, "%"))
    if h.temp <> invalid then parts.Push(metric("온도", h.temp, 80, "°"))
    if sv(h.uptime) <> "" then parts.Push("켜진 지 " + uptimeKo(sv(h.uptime)))
    out = ""
    for each p in parts
        if p <> ""
            if out <> "" then out = out + "  ·  "
            out = out + p
        end if
    end for
    return out
end function

function metric(name as String, v as Dynamic, warn as Integer, unit as String) as String
    if v = invalid then return ""
    t = Int(v).ToStr() + unit
    if v >= warn then t = "<amb>" + t + "</amb>"
    return name + " " + t
end function

' "142d" / "23h" / "1d" -> 142일 / 23시간 / 1일
function uptimeKo(u as String) as String
    n = Left(u, Len(u) - 1)
    k = Right(u, 1)
    if k = "d" then return n + "일"
    if k = "h" then return n + "시간"
    if k = "m" then return n + "분"
    return u
end function

function fmtPct(v as Float) as String
    whole = Int(v * 100 + 0.5)
    frac = whole mod 100
    return Int(whole / 100).ToStr() + "." + Right("0" + frac.ToStr(), 2) + "%"
end function

' "2026-10-31 05:00" -> "10월 31일 05:00"
function koStamp(s as String) as String
    if Len(s) < 16 then return s
    return Int(Val(Mid(s, 6, 2))).ToStr() + "월 " + Int(Val(Mid(s, 9, 2))).ToStr() + "일 " + Mid(s, 12, 5)
end function

function stateWord(st as String) as String
    w = {up: "정상", down: "멈춤", degraded: "느려짐", maintenance: "점검 중", unknown: "알 수 없음"}
    if w[st] <> invalid then return w[st]
    return st
end function

function stateColor(st as String) as String
    if st = "up" then return pal("celadon")
    if st = "degraded" then return pal("amber")
    if st = "maintenance" then return pal("text3")
    return pal("danger")
end function

function stateText(st as String) as String
    if st = "up" then return pal("text2")
    if st = "maintenance" then return pal("text3")
    return stateColor(st)
end function

function weightOf(st as String) as String
    if st = "up" or st = "maintenance" then return "r"
    return "sb"
end function

' ---------- tasks ----------

sub buildTasks(d as Object)
    makeSlotsFor("tasks")
    active = arr(d.active)
    recent = arr(d.recent)
    nRun = 0
    nWait = 0
    nQ = 0
    for each t in active
        st = sv(t.status)
        if st = "running" then nRun = nRun + 1
        if st = "waiting_approval" then nWait = nWait + 1
        if st = "queued" or st = "scheduled" then nQ = nQ + 1
    end for
    ap = 0
    if d.approvals <> invalid then ap = Int(d.approvals)
    m.summary.text = "진행 " + nRun.ToStr() + "  ·  대기 " + nQ.ToStr() + "  ·  승인 기다림 " + ap.ToStr()
    m.lines.Push({t: "sec", f: false, text: "지금 하는 일"})
    if active.Count() = 0 then m.lines.Push({t: "empty", f: false, text: "진행 중인 작업이 없어요"})
    for each t in active
        m.lines.Push({t: "task", f: true, k: t, done: false})
    end for
    m.lines.Push({t: "sec", f: false, text: "최근 끝난 작업"})
    if recent.Count() = 0 then m.lines.Push({t: "empty", f: false, text: "끝난 작업이 없어요"})
    for each t in recent
        m.lines.Push({t: "task", f: true, k: t, done: true})
    end for
end sub

sub drawTaskLine(s as Object, ln as Object)
    hideCharts(s)
    for each l in s.c
        l.text = ""
    end for
    s.right.text = ""
    s.b.text = ""
    s.meta.text = ""
    s.dot.visible = false
    s.a.width = 1700
    s.a.translation = [96, 0]
    s.meta.translation = [96, 62]
    if ln.t = "sec"
        s.a.font = mkFont("sb", 30)
        s.a.color = pal("text2")
        s.a.vertAlign = "bottom"
        s.a.height = 84
        s.a.text = ln.text
        return
    end if
    s.a.height = 96
    if ln.t = "empty"
        s.a.font = mkFont("r", 30)
        s.a.color = pal("text3")
        s.a.vertAlign = "center"
        s.a.text = ln.text
        return
    end if
    t = ln.k
    s.a.font = mkFont("sb", 30)
    s.a.color = pal("text")
    s.a.vertAlign = "top"
    s.a.translation = [96, 16]
    s.a.height = 40
    s.a.text = clean(sv(t.goal))
    s.meta.text = taskMeta(t, ln.done)
end sub

function taskMeta(t as Object, done as Boolean) as String
    now = nowEpoch()
    st = sv(t.status)
    parts = []
    if done
        w = {done: "<ok>끝남</ok>", failed: "<bad>실패</bad>", cancelled: "<dim>취소</dim>"}
        if w[st] <> invalid then parts.Push(w[st]) else parts.Push(st)
        f = isoEpoch(t.finished)
        if f > 0 then parts.Push(stamp(f, now))
        if sv(t.device) <> "" then parts.Push(sv(t.device))
        if sv(t.result) <> "" then parts.Push(esc(sv(t.result)))
    else
        w = {running: "<run>진행 중</run>", waiting_approval: "<amb>승인 기다림</amb>", queued: "대기 중", scheduled: "예약됨"}
        if w[st] <> invalid then parts.Push(w[st]) else parts.Push(st)
        if sv(t.device) <> "" then parts.Push(sv(t.device))
        if sv(t.engine) <> "" then parts.Push(sv(t.engine))
        s = isoEpoch(t.started)
        if s > 0 then parts.Push(stamp(s, now) + " 시작")
        if sv(t.step) <> "" then parts.Push(esc(sv(t.step)))
    end if
    out = ""
    for each p in parts
        if out <> "" then out = out + "  ·  "
        out = out + p
    end for
    return out
end function

' MultiStyleLabel reads <tags>: the server's own text must not open one
function esc(s as String) as String
    return clean(s)
end function

function stamp(e as Integer, now as Integer) as String
    if dayKey(e) = dayKey(now) then return fmtHM(e)
    dt = localDt(e)
    return dt.GetMonth().ToStr() + "/" + dt.GetDayOfMonth().ToStr() + " " + fmtHM(e)
end function

' ---------- draw and keys ----------

sub draw()
    n = m.slots.Count()
    if n = 0 then return
    if m.focus < m.offset then m.offset = m.focus
    if m.focus > m.offset + n - 1 then m.offset = m.focus - n + 1
    ' scrolling up onto the first line of a group: also show the gap or section title above it
    if not m.pinned and m.offset > 0 and m.focus = m.offset and not m.lines[m.offset - 1].f then m.offset = m.offset - 1
    if m.msg.visible then return
    for i = 0 to n - 1
        s = m.slots[i]
        idx = m.offset + i
        if idx < m.lines.Count()
            s.g.visible = true
            ln = m.lines[idx]
            if m.top.kind = "tasks" then drawTaskLine(s, ln) else drawInfraLine(s, ln)
            on = idx = m.focus and ln.f
            s.fill.visible = on
            s.ring.visible = on
        else
            s.g.visible = false
        end if
    end for
    total = 0
    at = 0
    for i = 0 to m.lines.Count() - 1
        if m.lines[i].f
            total = total + 1
            if i <= m.focus then at = total
        end if
    end for
    if total > n then m.pos.text = at.ToStr() + " / " + total.ToStr() else m.pos.text = ""
end sub

sub move(d as Integer)
    m.pinned = false
    i = m.focus + d
    while i >= 0 and i < m.lines.Count()
        if m.lines[i].f
            m.focus = i
            draw()
            return
        end if
        i = i + d
    end while
    ' nothing focusable further: still let the list scroll to show a trailing header or gap
    if d < 0 then m.offset = 0
    draw()
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    if key = "back"
        m.top.closed = true
        return true
    else if key = "up"
        move(-1)
        return true
    else if key = "down"
        move(1)
        return true
    else if key = "replay"
        m.top.refresh = true
        return true
    else if key = "left" or key = "right" or key = "OK"
        return true
    end if
    return false
end function
