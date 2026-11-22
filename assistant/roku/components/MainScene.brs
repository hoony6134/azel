' Azel TV dashboard: three pages (left/right), ambient mode after 5 idle minutes, a settings screen (* key).
' View-only: the only input is paging. Everything is drawn from code so one font (bundled Pretendard, which has
' Hangul) is used for every label.

sub init()
    m.reg = CreateObject("roRegistrySection", "azel")
    m.url = m.reg.Read("url")
    m.token = m.reg.Read("token")
    if m.url = "" then m.url = "https://azel.scian.xyz"
    m.page = 0
    m.data = invalid
    m.err = ""
    m.lastOk = ""
    m.mode = "dash"
    m.minutes = 0
    m.pageTitles = ["인프라", "작업", "할 일 · 일정"]
    m.dash = m.top.findNode("dash")
    m.ambient = m.top.findNode("ambient")
    m.settings = m.top.findNode("settings")
    buildDash()
    buildAmbient()
    buildSettings()
    m.poll = m.top.findNode("poll")
    m.idle = m.top.findNode("idle")
    m.tick = m.top.findNode("tick")
    m.poll.observeField("fire", "onPoll")
    m.idle.observeField("fire", "onIdle")
    m.tick.observeField("fire", "onTick")
    m.poll.control = "start"
    m.tick.control = "start"
    m.top.setFocus(true)
    if m.token = ""
        showSettings()
    else
        showDash()
        startFetch()
    end if
end sub

function mkLabel(parent as Object, x as Integer, y as Integer, w as Integer, h as Integer, size as Integer, color as String) as Object
    l = CreateObject("roSGNode", "Label")
    l.translation = [x, y]
    l.width = w
    l.height = h
    l.color = color
    f = CreateObject("roSGNode", "Font")
    f.uri = "pkg:/fonts/PretendardKR-Regular.otf"
    f.size = size
    l.font = f
    parent.appendChild(l)
    return l
end function

sub buildDash()
    m.title = mkLabel(m.dash, 96, 48, 1000, 80, 60, "0xE8EEF2FF")
    m.dots = mkLabel(m.dash, 96, 132, 400, 40, 30, "0x6B7A88FF")
    m.clock = mkLabel(m.dash, 1500, 48, 324, 70, 56, "0xE8EEF2FF")
    m.clock.horizAlign = "right"
    m.status = mkLabel(m.dash, 900, 128, 924, 40, 28, "0x8FA3B3FF")
    m.status.horizAlign = "right"
    m.colTitle = []
    m.colBody = []
    for i = 0 to 2
        x = 96 + i * 600
        t = mkLabel(m.dash, x, 220, 540, 50, 36, "0x7FD1AEFF")
        b = mkLabel(m.dash, x, 290, 540, 700, 32, "0xD5DDE3FF")
        b.wrap = true
        b.lineSpacing = 12
        b.vertAlign = "top"
        b.maxLines = 14
        m.colTitle.push(t)
        m.colBody.push(b)
    end for
end sub

sub buildAmbient()
    m.aClock = mkLabel(m.ambient, 0, 0, 900, 260, 220, "0x9FB4C4FF")
    m.aText = mkLabel(m.ambient, 0, 0, 1100, 260, 40, "0x6B7A88FF")
    m.aText.wrap = true
    m.aText.lineSpacing = 14
    m.aText.vertAlign = "top"
end sub

sub buildSettings()
    head = mkLabel(m.settings, 96, 80, 1500, 80, 56, "0xE8EEF2FF")
    head.text = "Azel TV 설정"
    m.setItems = []
    for i = 0 to 2
        m.setItems.push(mkLabel(m.settings, 96, 260 + i * 100, 1600, 70, 38, "0x8FA3B3FF"))
    end for
    hint = mkLabel(m.settings, 96, 640, 1700, 200, 30, "0x6B7A88FF")
    hint.text = "위아래로 고르고 확인(OK)을 누르세요." + Chr(10) + "토큰은 게이트웨이 ~/.config/biseo/tv_token 의 내용이에요(보기 전용)." + Chr(10) + "뒤로 가기로 돌아가요."
    m.setIdx = 0
    refreshSettings()
end sub

sub refreshSettings()
    tok = "(없음)"
    if m.token <> "" then tok = Left(m.token, 4) + "… (" + m.token.Len().ToStr() + "자)"
    labels = ["서버 주소  " + m.url, "토큰  " + tok, "저장하고 시작"]
    for i = 0 to 2
        m.setItems[i].text = labels[i]
        if i = m.setIdx
            m.setItems[i].color = "0x7FD1AEFF"
        else
            m.setItems[i].color = "0x8FA3B3FF"
        end if
    end for
end sub

' ---------- modes ----------

sub showDash()
    m.mode = "dash"
    m.dash.visible = true
    m.ambient.visible = false
    m.settings.visible = false
    restartIdle()
    updateDash()
end sub

sub showAmbient()
    m.mode = "ambient"
    m.dash.visible = false
    m.ambient.visible = true
    m.settings.visible = false
    updateAmbient()
end sub

sub showSettings()
    m.mode = "settings"
    m.dash.visible = false
    m.ambient.visible = false
    m.settings.visible = true
    m.idle.control = "stop"
    refreshSettings()
end sub

sub restartIdle()
    m.idle.control = "stop"
    m.idle.control = "start"
end sub

' ---------- data ----------

sub startFetch()
    if m.token = "" then return
    t = CreateObject("roSGNode", "FetchTask")
    t.baseUrl = m.url
    t.token = m.token
    t.observeField("result", "onFetch")
    m.task = t
    t.control = "RUN"
end sub

sub onFetch(ev as Object)
    r = ev.getData()
    if r.ok
        m.data = r.data
        m.err = ""
        m.lastOk = nowHM()
    else
        m.err = r.error
    end if
    updateDash()
    if m.mode = "ambient" then updateAmbient()
end sub

sub onPoll()
    startFetch()
end sub

sub onIdle()
    if m.mode = "dash" then showAmbient()
end sub

sub onTick()
    m.minutes = m.minutes + 1
    m.clock.text = nowHM()
    if m.mode = "ambient" then updateAmbient()
end sub

function nowHM() as String
    dt = CreateObject("roDateTime")
    dt.ToLocalTime()
    return Right("0" + dt.GetHours().ToStr(), 2) + ":" + Right("0" + dt.GetMinutes().ToStr(), 2)
end function

function joinLines(arr as Dynamic, empty as String) as String
    if arr = invalid then return "(불러올 수 없어요)"
    if Type(arr) <> "roArray" then return empty
    if arr.Count() = 0 then return empty
    s = ""
    for each line in arr
        if s <> "" then s = s + Chr(10)
        s = s + line
    end for
    return s
end function

function part(key as String) as Dynamic
    if m.data = invalid then return invalid
    return m.data[key]
end function

function pageColumns(p as Integer) as Object
    cols = []
    if p = 0
        i = part("infra")
        if i = invalid
            cols.push({t: "서버 상태", b: "(불러올 수 없어요)"})
            cols.push({t: "문제", b: "-"})
            cols.push({t: "경보", b: "-"})
        else
            cols.push({t: "서버 상태", b: joinLines(i.health, "-")})
            cols.push({t: "문제", b: joinLines(i.problems, "문제 없음")})
            cols.push({t: "경보", b: joinLines(i.alerts, "경보 없음")})
        end if
    else if p = 1
        t = part("tasks")
        if t = invalid
            cols.push({t: "실행 중", b: "(불러올 수 없어요)"})
            cols.push({t: "대기", b: "-"})
            cols.push({t: "최근", b: "-"})
        else
            run = []
            for each r in t.running
                run.push("• " + r.goal)
            end for
            rec = []
            for each r in t.recent
                rec.push("• " + r.goal + "  (" + r.status + ")")
            end for
            cols.push({t: "실행 중", b: joinLines(run, "없음")})
            cols.push({t: "대기", b: "대기 " + t.queued.ToStr() + "건" + Chr(10) + "승인 대기 " + t.approvals.ToStr() + "건 (앱에서 처리)"})
            cols.push({t: "최근 끝난 일", b: joinLines(rec, "없음")})
        end if
    else
        td = part("todo")
        if td = invalid
            cols.push({t: "오늘", b: "(불러올 수 없어요)"})
            cols.push({t: "과제 · 할 일", b: "-"})
        else
            cols.push({t: "오늘", b: joinLines(td.today, "없음")})
            cols.push({t: "과제 · 할 일", b: joinLines(td.assignments, "없음")})
        end if
        ev = []
        calls = part("calls")
        if calls <> invalid
            for each c in calls
                ev.push(c.at + "  전화 · " + c.what)
            end for
        end if
        cal = part("calendar")
        if cal <> invalid
            for each c in cal
                ev.push(c)
            end for
        end if
        cols.push({t: "오늘 일정 · 전화", b: joinLines(ev, "없음")})
    end if
    return cols
end function

sub updateDash()
    m.title.text = m.pageTitles[m.page]
    m.dots.text = dotsText()
    m.clock.text = nowHM()
    cols = pageColumns(m.page)
    for i = 0 to 2
        m.colTitle[i].visible = i < cols.Count()
        m.colBody[i].visible = i < cols.Count()
        if i < cols.Count()
            m.colTitle[i].text = cols[i].t
            m.colBody[i].text = cols[i].b
        end if
    end for
    m.status.color = "0x8FA3B3FF"
    if m.err <> ""
        m.status.color = "0xF0B35AFF"
        m.status.text = "연결 끊김 · " + m.err + " · 마지막 " + m.lastOk
    else
        a = part("assistant")
        s = ""
        if a <> invalid
            s = a.name + " · "
            if a.state = "call" then s = s + "통화 중" else s = s + "대기"
            s = s + "   "
        end if
        if m.lastOk <> "" then s = s + "갱신 " + m.lastOk else s = s + "불러오는 중…"
        m.status.text = s
    end if
end sub

function dotsText() as String
    s = ""
    for i = 0 to 2
        if i = m.page then s = s + "● " else s = s + "○ "
    end for
    return s
end function

sub updateAmbient()
    m.aClock.text = nowHM()
    lines = []
    i = part("infra")
    if i <> invalid
        lines.push(joinLines(i.problems, "문제 없음"))
    end if
    t = part("tasks")
    if t <> invalid
        lines.push("실행 중 " + t.running.Count().ToStr() + " · 대기 " + t.queued.ToStr() + " · 승인 대기 " + t.approvals.ToStr())
    end if
    td = part("todo")
    if td <> invalid and td.today <> invalid and td.today.Count() > 0 then lines.push(td.today[0])
    if m.err <> "" then lines.push("연결 끊김")
    m.aText.text = joinLines(lines, "")
    ' move a little every minute so nothing burns in
    spots = [[120, 140], [700, 90], [1000, 300], [200, 500], [800, 560], [420, 280]]
    p = spots[m.minutes mod 6]
    m.aClock.translation = [p[0], p[1]]
    m.aText.translation = [p[0] + 8, p[1] + 250]
end sub

' ---------- input ----------

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    restartIdle()
    if m.mode = "ambient"
        showDash()
        return true
    end if
    if m.mode = "settings"
        return settingsKey(key)
    end if
    if key = "left"
        m.page = (m.page + 2) mod 3
        updateDash()
        return true
    else if key = "right"
        m.page = (m.page + 1) mod 3
        updateDash()
        return true
    else if key = "options"
        showSettings()
        return true
    else if key = "OK"
        showAmbient()
        return true
    end if
    return false
end function

function settingsKey(key as String) as Boolean
    if key = "up"
        m.setIdx = (m.setIdx + 2) mod 3
        refreshSettings()
    else if key = "down"
        m.setIdx = (m.setIdx + 1) mod 3
        refreshSettings()
    else if key = "OK"
        if m.setIdx = 0
            openKeyboard("Server URL", m.url, "url")
        else if m.setIdx = 1
            openKeyboard("Token", "", "token")
        else
            saveSettings()
        end if
    else if key = "back" and m.token <> ""
        showDash()
    else
        return false
    end if
    return true
end function

sub openKeyboard(title as String, text as String, which as String)
    m.kbWhich = which
    d = CreateObject("roSGNode", "StandardKeyboardDialog")
    d.title = title
    d.text = text
    d.buttons = ["OK", "Cancel"]
    d.observeField("buttonSelected", "onKb")
    m.top.dialog = d
end sub

sub onKb()
    d = m.top.dialog
    if d = invalid then return
    if d.buttonSelected = 0
        if m.kbWhich = "url" then m.url = d.text.Trim() else m.token = d.text.Trim()
        refreshSettings()
    end if
    d.close = true
end sub

sub saveSettings()
    if m.token = "" then return
    m.reg.Write("url", m.url)
    m.reg.Write("token", m.token)
    m.reg.Flush()
    m.data = invalid
    m.err = ""
    showDash()
    startFetch()
end sub
