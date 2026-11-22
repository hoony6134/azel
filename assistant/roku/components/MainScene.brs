' Azel TV dashboard (SPEC.md): header, four summary tiles, one RowList of sections with 402x220 cards, a side detail
' panel (OK), ambient mode (Play/Pause or 5 idle minutes), error/stale/partial/empty states, settings.
' View-only: no key sends anything to the gateway except the poll itself.

sub init()
    m.reg = CreateObject("roRegistrySection", "azel")
    m.url = m.reg.Read("url")
    m.token = m.reg.Read("token")
    if m.url = "" then m.url = "https://azel.scian.xyz"
    m.mode = "dash"
    m.data = invalid
    m.v = 2
    m.model = invalid
    m.sig = ""
    m.sectionTitles = []
    m.err = ""
    m.errCode = 0
    m.hasData = false
    m.lastOk = 0
    m.fetching = false
    m.offsetIdx = 0
    m.detailRow = 0
    m.detailCol = 0
    m.errIdx = 0
    m.setIdx = 0
    m.glaze = pal("celadon")
    m.header = m.top.findNode("header")
    m.strip = m.top.findNode("strip")
    m.rows = m.top.findNode("rows")
    m.detail = m.top.findNode("detail")
    m.ambientG = m.top.findNode("ambientG")
    m.orbG = m.top.findNode("orbG")
    m.errorG = m.top.findNode("errorG")
    m.settings = m.top.findNode("settings")
    m.poll = m.top.findNode("poll")
    m.idle = m.top.findNode("idle")
    m.tick = m.top.findNode("tick")
    m.breath = m.top.findNode("breath")
    buildHeader()
    buildStrip()
    buildRows()
    buildDetail()
    buildAmbient()
    buildError()
    buildSettings()
    m.poll.observeField("fire", "onPoll")
    m.idle.observeField("fire", "onIdle")
    m.tick.observeField("fire", "onTick")
    m.rows.observeField("rowItemSelected", "onSelected")
    m.rows.observeField("rowItemFocused", "onFocused")
    m.poll.control = "start"
    m.tick.control = "start"
    updateHeader()
    if m.token = ""
        showSettings()
    else
        showDash()
        startFetch()
    end if
end sub

' ---------- build ----------

sub buildHeader()
    m.hGlow = mkPoster(m.header, "pkg:/images/orb_glow_72.png", -14, -14, 100, 100, m.glaze)
    m.hBody = mkPoster(m.header, "pkg:/images/orb_body_72.png", 0, 0, 72, 72, "0xFFFFFFFF")
    n = mkLabel(m.header, 92, 0, 600, 44, 36, "sb", pal("text"))
    n.text = "Azel"
    m.hState = mkLabel(m.header, 92, 46, 700, 34, 26, "r", pal("text2"))
    m.hClock = mkLabel(m.header, 1128, 0, 600, 52, 48, "sb", pal("text"))
    m.hClock.horizAlign = "right"
    m.hDate = mkLabel(m.header, 828, 54, 900, 32, 26, "r", pal("text2"))
    m.hDate.horizAlign = "right"
end sub

sub buildStrip()
    m.tiles = []
    for i = 0 to 3
        t = CreateObject("roSGNode", "StatTile")
        t.translation = [i * 438, 0]
        m.strip.appendChild(t)
        m.tiles.Push(t)
    end for
    setTiles([{k: "경고", v: "—", s2: "", hot: false, acc: ""}, {k: "진행 중 작업", v: "—", s2: "", hot: false, acc: ""}, {k: "다음 마감", v: "—", s2: "", hot: false, acc: ""}, {k: "다음 통화", v: "—", s2: "", hot: false, acc: ""}])
end sub

sub setTiles(list as Object)
    for i = 0 to 3
        m.tiles[i].data = list[i]
    end for
end sub

sub buildRows()
    m.rows.rowLabelFont = mkFont("sb", 34)
    m.rows.rowLabelColor = pal("text")
end sub

sub buildDetail()
    mkRect(m.detail, 0, 0, 760, 1080, pal("raised"))
    mkRect(m.detail, 0, 0, 2, 1080, pal("stroke"))
    m.dKind = mkLabel(m.detail, 56, 54, 650, 32, 24, "sb", pal("text2"))
    m.dTitle = mkLabel(m.detail, 56, 100, 650, 156, 40, "sb", pal("text"))
    m.dTitle.wrap = true
    m.dTitle.maxLines = 3
    m.dTitle.height = 0 ' auto-height: the sub line follows its bottom
    m.dSub = mkLabel(m.detail, 56, 200, 650, 40, 28, "r", pal("text2"))
    m.dSubHot = mkLabel(m.detail, 56, 200, 650, 40, 28, "sb", pal("amber"))
    m.dLine = []
    m.dFl = []
    m.dFv = []
    for i = 0 to 4
        m.dLine.Push(mkRect(m.detail, 56, 0, 648, 2, pal("stroke")))
        m.dFl.Push(mkLabel(m.detail, 56, 0, 648, 30, 24, "r", pal("text3")))
        v = mkLabel(m.detail, 56, 0, 648, 80, 30, "r", pal("text"))
        v.wrap = true
        v.maxLines = 2
        m.dFv.Push(v)
    end for
    h = mkLabel(m.detail, 56, 1000, 650, 36, 26, "r", pal("text2"))
    h.text = "뒤로 닫기      ← → 옆 카드"
end sub

sub buildAmbient()
    m.aClock = mkLabel(m.orbG, 432, 10, 700, 190, 180, "sb", "0xECF0ECD1")
    m.aDate = mkLabel(m.orbG, 432, 222, 900, 50, 40, "r", "0xA9B4B0D1")
    m.aSum = mkLabel(m.orbG, 432, 282, 1100, 44, 34, "r", "0xA9B4B0D1")
    m.aUrgent = mkLabel(m.orbG, 432, 336, 1100, 44, 34, "sb", "0xE9BC6EE6")
    m.orbG.translation = [240, 250]
end sub

sub buildError()
    m.eOrb = mkPoster(m.errorG, "pkg:/images/orb_body_360.png", 860, 300, 200, 200, pal("grey"))
    m.eOrb.opacity = 0.55
    m.eTitle = mkLabel(m.errorG, 0, 516, 1920, 64, 52, "sb", pal("text"))
    m.eTitle.horizAlign = "center"
    m.eBody = mkLabel(m.errorG, 360, 600, 1200, 90, 30, "r", pal("text2"))
    m.eBody.horizAlign = "center"
    m.eBody.wrap = true
    m.eBody.maxLines = 2
    m.eBorder = []
    m.eFill = []
    m.eText = []
    names = ["다시 시도", "설정"]
    for i = 0 to 1
        x = 700 + i * 280
        m.eBorder.Push(mkPoster(m.errorG, "pkg:/images/pill.9.png", x - 2, 732, 244, 76, pal("stroke")))
        m.eFill.Push(mkPoster(m.errorG, "pkg:/images/pill.9.png", x, 734, 240, 72, pal("bg")))
        l = mkLabel(m.errorG, x, 734, 240, 72, 30, "sb", pal("text"))
        l.horizAlign = "center"
        l.vertAlign = "center"
        l.text = names[i]
        m.eText.Push(l)
    end for
end sub

sub buildSettings()
    head = mkLabel(m.settings, 96, 80, 1500, 80, 56, "sb", pal("text"))
    head.text = "Azel TV 설정"
    m.setItems = []
    for i = 0 to 2
        m.setItems.Push(mkLabel(m.settings, 96, 260 + i * 100, 1600, 70, 38, "r", pal("text2")))
    end for
    hint = mkLabel(m.settings, 96, 640, 1700, 200, 30, "r", pal("text3"))
    hint.wrap = true
    hint.maxLines = 4
    hint.lineSpacing = 8
    hint.text = "위아래로 고르고 확인(OK)을 누르세요." + Chr(10) + "토큰은 게이트웨이 ~/.config/biseo/tv_token 의 내용이에요(보기 전용)." + Chr(10) + "뒤로 가기로 돌아가요."
    refreshSettings()
end sub

sub refreshSettings()
    tok = "(없음)"
    if m.token <> "" then tok = Left(m.token, 4) + "… (" + m.token.Len().ToStr() + "자)"
    labels = ["서버 주소  " + m.url, "토큰  " + tok, "저장하고 시작"]
    for i = 0 to 2
        m.setItems[i].text = labels[i]
        if i = m.setIdx then m.setItems[i].color = pal("celadon") else m.setItems[i].color = pal("text2")
    end for
end sub

' ---------- modes ----------

sub hideAllModes()
    m.header.visible = false
    m.strip.visible = false
    m.rows.visible = false
    m.detail.visible = false
    m.ambientG.visible = false
    m.errorG.visible = false
    m.settings.visible = false
    m.breath.control = "stop"
end sub

sub setDim(on as Boolean)
    o = 1.0
    if on then o = 0.45
    m.header.opacity = o
    m.strip.opacity = o
    if on
        m.rows.opacity = o
    else
        applyStale()
    end if
end sub

sub showDash()
    hideAllModes()
    m.mode = "dash"
    m.header.visible = true
    m.strip.visible = true
    m.rows.visible = true
    setDim(false)
    m.rows.setFocus(true)
    restartIdle()
    updateHeader()
end sub

sub showDetail(r as Integer, c as Integer)
    m.mode = "detail"
    m.detailRow = r
    m.detailCol = c
    m.detail.visible = true
    setDim(true)
    updateDetail(r, c)
end sub

sub closeDetail()
    m.mode = "dash"
    m.detail.visible = false
    setDim(false)
    m.rows.setFocus(true)
end sub

sub showAmbient()
    hideAllModes()
    m.mode = "ambient"
    m.ambientG.visible = true
    m.top.setFocus(true)
    m.breath.control = "start"
    updateAmbient()
end sub

sub showError()
    hideAllModes()
    m.mode = "error"
    m.errorG.visible = true
    m.top.setFocus(true)
    m.idle.control = "stop"
    if m.errCode = 401
        m.eTitle.text = "토큰이 맞지 않아요"
        m.eBody.text = "설정에서 TV 토큰을 다시 넣어 주세요."
        m.errIdx = 1
    else
        m.eTitle.text = "서버에 연결할 수 없어요"
        m.eBody.text = "인터넷 연결과 서버 주소를 확인해 주세요." + Chr(10) + "30초마다 저절로 다시 시도해요."
        m.errIdx = 0
    end if
    refreshError()
end sub

sub refreshError()
    for i = 0 to 1
        if i = m.errIdx
            m.eFill[i].blendColor = pal("celadon")
            m.eText[i].color = pal("bg")
        else
            m.eFill[i].blendColor = pal("bg")
            m.eText[i].color = pal("text")
        end if
    end for
end sub

sub showSettings()
    hideAllModes()
    m.mode = "settings"
    m.settings.visible = true
    m.top.setFocus(true)
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
    if m.fetching then return
    m.fetching = true
    t = CreateObject("roSGNode", "FetchTask")
    t.baseUrl = m.url
    t.token = m.token
    t.observeField("result", "onFetch")
    m.task = t
    t.control = "RUN"
    updateHeader()
end sub

sub fetchNow()
    startFetch()
end sub

sub setPoll(sec as Integer)
    if m.poll.duration = sec then return
    m.poll.control = "stop"
    m.poll.duration = sec
    m.poll.control = "start"
end sub

sub onFetch(ev as Object)
    m.fetching = false
    r = ev.getData()
    if r.ok
        m.err = ""
        m.errCode = 0
        m.hasData = true
        m.lastOk = nowEpoch()
        m.data = r.data
        m.v = r.v
        setPoll(60)
        applyData()
        if m.mode = "error" then showDash()
    else
        m.err = r.error
        m.errCode = r.code
        setPoll(30)
        if not m.hasData
            showError()
        else
            applyStale()
        end if
    end if
    updateHeader()
end sub

sub onPoll()
    startFetch()
end sub

sub onIdle()
    if m.mode = "dash" or m.mode = "detail" then showAmbient()
end sub

sub onTick()
    updateHeader()
    if m.hasData then applyData()
    applyStale()
    if m.mode = "ambient"
        m.offsetIdx = m.offsetIdx + 1
        updateAmbient()
    end if
end sub

sub applyStale()
    if m.mode = "detail" then return
    if m.err <> "" and m.hasData and nowEpoch() - m.lastOk > 600
        m.rows.opacity = 0.6
    else
        m.rows.opacity = 1.0
    end if
end sub

sub applyData()
    now = nowEpoch()
    model = buildModel(m.data, m.v, now)
    m.model = model
    setTiles(model.tiles)
    a = m.data.assistant
    if a <> invalid
        if a.persona <> invalid then m.glaze = glazeOf(sv(a.persona.tint)) else m.glaze = pal("celadon")
    end if
    m.hGlow.blendColor = m.glaze
    setRows(model)
    if m.mode = "ambient" then updateAmbient()
    if m.mode = "detail" then updateDetail(m.detailRow, m.detailCol)
end sub

sub setRows(model as Object)
    sig = FormatJson(model.sections)
    if sig = m.sig then return
    m.sig = sig
    prevTitle = ""
    prevC = 0
    f = m.rows.rowItemFocused
    if f <> invalid and f.Count() = 2 and f[0] < m.sectionTitles.Count()
        prevTitle = m.sectionTitles[f[0]]
        prevC = f[1]
    end if
    titles = []
    for each s in model.sections
        titles.Push(s.title)
    end for
    m.sectionTitles = titles
    m.rows.content = makeContent(model.sections)
    r = 0
    c = 0
    if prevTitle <> ""
        for i = 0 to titles.Count() - 1
            if titles[i] = prevTitle
                r = i
                c = prevC
            end if
        end for
        n = model.sections[r].cards.Count()
        if c > n - 1 then c = n - 1
        if c < 0 then c = 0
        m.rows.jumpToRowItem = [r, c]
    end if
end sub

function glazeOf(tint as String) as String
    g = {celadon: "0x82CBB4FF", indigo: "0x9AAAF0FF", amber: "0xE9BC6EFF", azalea: "0xF2A7B8FF", sprout: "0xB9D77AFF", sky: "0x8FCBEAFF", plum: "0xC4A2E6FF", ash: "0xB8C0BDFF"}
    if g[tint] <> invalid then return g[tint]
    if Left(tint, 1) = "#" and Len(tint) = 7 then return "0x" + UCase(Mid(tint, 2, 6)) + "FF"
    return g.celadon
end function

sub updateHeader()
    now = nowEpoch()
    m.hClock.text = fmtHM(now)
    st = "대기 중"
    st2 = pal("text2")
    if m.data <> invalid and m.data.assistant <> invalid and sv(m.data.assistant.state) = "call"
        st = "통화 중"
        st2 = pal("celadon")
        if m.data.assistant.persona <> invalid
            if sv(m.data.assistant.persona.name) <> "" then st = st + " · " + sv(m.data.assistant.persona.name)
        end if
    end if
    m.hState.text = st
    m.hState.color = st2
    m.hDate.color = pal("text2")
    if not m.hasData
        if m.err <> "" then m.hDate.text = "" else m.hDate.text = "불러오는 중이에요"
    else if m.err <> ""
        m.hDate.text = "연결이 끊겼어요 · " + fmtHM(m.lastOk) + " 기준"
        m.hDate.color = pal("amber")
    else if m.fetching
        m.hDate.text = "갱신 중…"
    else
        m.hDate.text = dateKo(now) + " · " + fmtHM(m.lastOk) + " 갱신"
    end if
end sub

sub updateAmbient()
    now = nowEpoch()
    m.aClock.text = fmtHM(now)
    m.aDate.text = dateKo(now)
    s = ""
    u = ""
    if m.model <> invalid
        s = "경고 " + m.model.alerts.ToStr() + " · 진행 중 작업 " + m.model.running.ToStr()
        if m.model.nextCall <> "—" then s = s + " · 다음 통화 " + m.model.nextCall
        u = m.model.urgentLine
    end if
    m.aSum.text = s
    m.aUrgent.text = u
    m.aUrgent.visible = u <> ""
    m.aGlow = m.top.findNode("aGlow")
    m.aGlow.blendColor = m.glaze
    offs = [[0, 0], [120, 60], [240, 0], [240, 120], [120, 180], [0, 120]]
    o = offs[m.offsetIdx mod 6]
    m.orbG.translation = [240 + o[0], 250 + o[1]]
end sub

sub updateDetail(r as Integer, c as Integer)
    if m.model = invalid then return
    if r >= m.model.sections.Count() then return
    cards = m.model.sections[r].cards
    if c >= cards.Count() then return
    k = cards[c]
    m.dKind.text = k.dkind
    m.dKind.color = accentColor(k.acc, "text2")
    m.dTitle.text = k.dtitle
    y = 100 + m.dTitle.boundingRect().height + 14
    m.dSub.visible = false
    m.dSubHot.visible = false
    if k.dsub <> ""
        s = m.dSub
        if k.dhot then s = m.dSubHot
        s.text = k.dsub
        s.translation = [56, y]
        s.visible = true
        y = y + 54
    end if
    y = y + 34
    for i = 0 to 4
        show = i < k.facts.Count()
        m.dLine[i].visible = show
        m.dFl[i].visible = show
        m.dFv[i].visible = show
        if show
            m.dLine[i].translation = [56, y]
            m.dFl[i].translation = [56, y + 14]
            m.dFv[i].translation = [56, y + 46]
            m.dFl[i].text = k.facts[i].l
            m.dFv[i].text = k.facts[i].v
            y = y + 106
        end if
    end for
end sub

' ---------- input ----------

sub onSelected()
    f = m.rows.rowItemSelected
    if f = invalid or f.Count() <> 2 then return
    if m.mode = "detail"
        closeDetail()
    else if m.mode = "dash"
        showDetail(f[0], f[1])
    end if
end sub

sub onFocused()
    restartIdle()
    if m.mode <> "detail" then return
    f = m.rows.rowItemFocused
    if f = invalid or f.Count() <> 2 then return
    if f[0] <> m.detailRow
        m.rows.jumpToRowItem = [m.detailRow, m.detailCol]
    else
        m.detailCol = f[1]
        updateDetail(f[0], f[1])
    end if
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    restartIdle()
    if m.mode = "ambient"
        showDash()
        return true
    end if
    if m.mode = "settings" then return settingsKey(key)
    if m.mode = "error" then return errorKey(key)
    if key = "back"
        if m.mode = "detail"
            closeDetail()
            return true
        end if
        f = m.rows.rowItemFocused
        if f <> invalid and f.Count() = 2
            if f[0] <> 0 or f[1] <> 0
                m.rows.jumpToRowItem = [0, 0]
                return true
            end if
        end if
        return false
    else if key = "play"
        showAmbient()
        return true
    else if key = "replay"
        fetchNow()
        return true
    else if key = "options"
        showSettings()
        return true
    end if
    return false
end function

function errorKey(key as String) as Boolean
    if key = "left" or key = "right"
        m.errIdx = 1 - m.errIdx
        refreshError()
    else if key = "OK"
        if m.errIdx = 0 then startFetch() else showSettings()
    else if key = "replay"
        startFetch()
    else if key = "options"
        showSettings()
    else
        return false
    end if
    return true
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
        if not m.hasData and m.err <> ""
            showError()
        else
            showDash()
        end if
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
    m.hasData = false
    m.err = ""
    m.sig = ""
    m.model = invalid
    showDash()
    startFetch()
end sub
