' Azel TV dashboard (SPEC.md): header with the living logo, a four-figure strip, one RowList of sections whose card
' width follows the content, a compact detail panel (OK), ambient mode (Play/Pause or 5 idle minutes), error/stale/
' partial/empty states and settings. View-only: nothing here sends anything to the gateway except the poll itself.

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
    m.inCall = false
    m.header = m.top.findNode("header")
    m.strip = m.top.findNode("strip")
    m.rows = m.top.findNode("rows")
    m.rowsClip = m.top.findNode("rowsClip")
    m.detail = m.top.findNode("detail")
    m.ambientG = m.top.findNode("ambientG")
    m.ambBase = m.top.findNode("ambBase")
    m.errorG = m.top.findNode("errorG")
    m.settings = m.top.findNode("settings")
    m.poll = m.top.findNode("poll")
    m.idle = m.top.findNode("idle")
    m.tick = m.top.findNode("tick")
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
    m.hLogo = CreateObject("roSGNode", "Logo")
    m.header.appendChild(m.hLogo)
    m.hName = mkLabel(m.header, 84, 9, 0, 46, 34, "sb", pal("text"))
    m.hName.width = 0
    m.hName.text = "Azel"
    m.hState = mkLabel(m.header, 0, 17, 0, 30, 24, "r", pal("text"))
    m.hState.width = 0
    m.hClock = mkLabel(m.header, 1432, 8, 300, 48, 44, "sb", pal("text"))
    m.hClock.width = 0
    m.hDate = mkLabel(m.header, 0, 20, 760, 30, 24, "r", pal("text2"))
    m.hDate.horizAlign = "right"
end sub

sub buildStrip()
    mkPoster(m.strip, "pkg:/images/rule.png", 0, 0, 1728, 2, pal("rule"))
    for each x in [426, 864, 1302]
        mkPoster(m.strip, "pkg:/images/rule.png", x, 2, 2, 120, pal("rule"))
    end for
    m.tiles = []
    xs = [0, 438, 876, 1314]
    ws = [426, 438, 438, 426]
    for i = 0 to 3
        t = CreateObject("roSGNode", "StatTile")
        t.translation = [xs[i], 0]
        t.colW = ws[i]
        m.strip.appendChild(t)
        m.tiles.Push(t)
    end for
    setFigs([{k: "경고", v: "—", t: "", acc: ""}, {k: "진행 중 작업", v: "—", t: "", acc: ""}, {k: "다음 마감", v: "—", t: "", acc: ""}, {k: "다음 통화", v: "—", t: "", acc: ""}])
end sub

sub setFigs(list as Object)
    for i = 0 to 3
        m.tiles[i].data = list[i]
    end for
end sub

sub buildRows()
    m.rows.rowLabelFont = mkFont("sb", 34)
    m.rows.rowLabelColor = pal("text")
    m.counter = mkLabel(m.rowsClip, 1224, 6, 600, 30, 24, "r", pal("text3"))
    m.counter.horizAlign = "right"
    m.counterOn = []
end sub

sub updateCounter()
    f = m.rows.rowItemFocused
    m.counter.visible = false
    if f = invalid or f.Count() <> 2 or m.model = invalid then return
    if f[0] >= m.model.sections.Count() then return
    sec = m.model.sections[f[0]]
    txt = sec.note
    if sec.cards.Count() > sec.cap
        n = (f[1] + 1).ToStr() + " / " + sec.cards.Count().ToStr()
        if txt <> "" then txt = txt + "  ·  " + n else txt = n
    end if
    if txt = "" then return
    m.counter.text = txt
    m.counter.visible = true
end sub

sub buildDetail()
    m.dBg = mkPoster(m.detail, "pkg:/images/card.9.png", 0, 0, 852, 400, pal("raised"))
    m.dTitle = mkLabel(m.detail, 40, 36, 772, 0, 34, "sb", pal("text"))
    m.dTitle.wrap = true
    m.dTitle.maxLines = 3
    m.dTitle.height = 0
    m.dSub = mkLabel(m.detail, 40, 0, 772, 30, 24, "r", pal("text2"))
    m.dSubHot = mkLabel(m.detail, 40, 0, 772, 30, 24, "sb", pal("amber"))
    m.dLine = []
    m.dFl = []
    m.dFv = []
    for i = 0 to 5
        m.dLine.Push(mkPoster(m.detail, "pkg:/images/rule.png", 40, 0, 772, 2, pal("rule")))
        m.dFl.Push(mkLabel(m.detail, 40, 0, 120, 30, 24, "r", pal("text2")))
        m.dFv.Push(mkLabel(m.detail, 200, 0, 612, 40, 30, "r", pal("text")))
    end for
    m.dHint = CreateObject("roSGNode", "MultiStyleLabel")
    m.dHint.width = 772
    m.dHint.height = 30
    m.dHint.drawingStyles = {
        default: {fontUri: "pkg:/fonts/PretendardKR-Regular.otf", fontSize: 24, color: "#8A9692FF"}
        k: {fontUri: "pkg:/fonts/PretendardKR-SemiBold.otf", fontSize: 24, color: "#A9B4B0FF"}
    }
    m.dHint.text = "<k>뒤로</k> 닫기  ·  <k>좌우 키</k> 옆 카드"
    m.detail.appendChild(m.dHint)
end sub

sub buildAmbient()
    m.aLogo = CreateObject("roSGNode", "Logo")
    m.aLogo.scale = [2.34375, 2.34375]
    m.aLogo.opacity = 0.85
    m.ambBase.appendChild(m.aLogo)
    m.aClock = mkLabel(m.ambBase, 198, -25, 0, 200, 200, "sb", "0xECF0ECCC")
    m.aClock.width = 0
    m.aL1 = mkLabel(m.ambBase, 198, 215, 1300, 52, 34, "r", "0xA9B4B0CC")
    m.aL2 = mkLabel(m.ambBase, 198, 267, 1300, 52, 34, "r", "0xFF7A6EE6")
    m.aL3 = mkLabel(m.ambBase, 198, 319, 1300, 52, 34, "r", "0xE9BC6EE6")
end sub

sub buildError()
    m.eTitle = mkLabel(m.errorG, 96, 360, 1500, 68, 56, "sb", pal("text"))
    m.eBody = mkLabel(m.errorG, 96, 448, 1100, 88, 30, "r", pal("text2"))
    m.eBody.wrap = true
    m.eBody.maxLines = 2
    m.eBody.lineSpacing = 4
    m.eFill = []
    m.eRing = []
    m.eText = []
    m.eBtnW = []
    names = ["다시 시도", "설정"]
    x = 96
    for i = 0 to 1
        l = mkLabel(m.errorG, 0, 592, 0, 72, 30, "sb", pal("text"))
        l.width = 0
        l.vertAlign = "center"
        l.text = names[i]
        bw = l.boundingRect().width + 64
        fill = mkPoster(m.errorG, "pkg:/images/card.9.png", x, 592, bw, 72, pal("card"))
        ring = mkPoster(m.errorG, "pkg:/images/focus.9.png", x, 592, bw, 72, pal("text"))
        ring.visible = false
        m.errorG.removeChild(l)
        m.errorG.appendChild(l)
        l.translation = [x + 32, 592]
        m.eFill.Push(fill)
        m.eRing.Push(ring)
        m.eText.Push(l)
        x = x + bw + 24
    end for
    m.eHost = mkLabel(m.errorG, 96, 712, 1300, 30, 24, "r", pal("text3"))
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
    m.rowsClip.visible = false
    m.detail.visible = false
    m.ambientG.visible = false
    m.errorG.visible = false
    m.settings.visible = false
    m.hLogo.active = false
    m.aLogo.active = false
end sub

sub setDim(on as Boolean)
    o = 1.0
    if on then o = 0.35
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
    m.rowsClip.visible = true
    m.hLogo.opacity = 1
    m.hLogo.active = true
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
    m.aLogo.tint = m.glaze
    m.aLogo.inCall = m.inCall
    m.aLogo.active = true
    updateAmbient()
end sub

sub showError()
    hideAllModes()
    m.mode = "error"
    m.header.visible = true
    m.errorG.visible = true
    m.top.setFocus(true)
    m.idle.control = "stop"
    m.hLogo.tint = pal("text3")
    m.hLogo.opacity = 0.4
    m.hLogo.active = false
    if m.errCode = 401
        m.eTitle.text = "토큰이 맞지 않아요"
        m.eBody.text = "설정에서 TV 토큰을 다시 넣어 주세요."
        m.errIdx = 1
    else
        m.eTitle.text = "서버에 연결할 수 없어요"
        m.eBody.text = "인터넷 연결과 서버 주소를 확인해 주세요. 30초마다 저절로 다시 시도해요."
        m.errIdx = 0
    end if
    h = m.url
    p = Instr(1, h, "://")
    if p > 0 then h = Mid(h, p + 3)
    p = Instr(1, h, "/")
    if p > 0 then h = Left(h, p - 1)
    m.eHost.text = "서버 주소 " + h
    refreshError()
    updateHeader()
end sub

sub refreshError()
    for i = 0 to 1
        m.eRing[i].visible = i = m.errIdx
        if i = m.errIdx then m.eFill[i].blendColor = pal("cardFocus") else m.eFill[i].blendColor = pal("card")
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
    setFigs(model.figs)
    m.glaze = pal("celadon")
    m.inCall = false
    a = m.data.assistant
    if a <> invalid
        if sv(a.state) = "call" then m.inCall = true
        if a.persona <> invalid and m.inCall then m.glaze = glazeOf(sv(a.persona.tint))
    end if
    m.hLogo.tint = m.glaze
    m.hLogo.inCall = m.inCall
    setRows(model)
    if m.mode = "ambient"
        m.aLogo.tint = m.glaze
        m.aLogo.inCall = m.inCall
        updateAmbient()
    end if
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
    sizes = []
    gaps = []
    for each s in model.sections
        titles.Push(s.title)
        sizes.Push([s.w, 200])
        gaps.Push([24, 0])
    end for
    m.sectionTitles = titles
    m.rows.rowItemSize = sizes
    m.rows.rowItemSpacing = gaps
    m.rows.content = makeContent(model.sections)
    updateCounter()
    if prevTitle <> ""
        r = 0
        c = 0
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
    inErr = m.mode = "error"
    m.hClock.visible = not inErr
    m.hDate.visible = not inErr
    st = ""
    if m.inCall
        st = "통화 중"
        if m.data <> invalid and m.data.assistant <> invalid and m.data.assistant.persona <> invalid
            if sv(m.data.assistant.persona.name) <> "" then st = st + " · " + sv(m.data.assistant.persona.name)
        end if
    end if
    m.hState.text = st
    m.hState.visible = st <> "" and not inErr
    m.hState.translation = [84 + m.hName.boundingRect().width + 24, 17]
    m.hClock.text = fmtHM(now)
    cw = m.hClock.boundingRect().width
    m.hClock.translation = [1732 - cw, 8]
    m.hDate.translation = [1732 - cw - 20 - 760, 20]
    m.hDate.color = pal("text2")
    if not m.hasData
        if m.err <> "" then m.hDate.text = "" else m.hDate.text = "불러오는 중이에요"
    else if m.err <> ""
        m.hDate.text = "연결이 끊겼어요 · " + fmtHM(m.lastOk) + " 기준"
        m.hDate.color = pal("amber")
    else if m.fetching
        m.hDate.text = "갱신 중…"
    else
        m.hDate.text = dateKo(now)
        age = now - m.lastOk
        if age > 120
            m.hDate.text = m.hDate.text + " · " + Int(age / 60).ToStr() + "분 전 갱신"
            m.hDate.color = pal("text3")
        end if
    end if
end sub

sub updateAmbient()
    now = nowEpoch()
    m.aClock.text = fmtHM(now)
    l1 = dateKo(now)
    l2 = ""
    l3 = ""
    if m.model <> invalid
        if m.model.nextCall <> "" then l1 = l1 + " · 다음 통화 " + m.model.nextCall
        l2 = m.model.brokenLine
        l3 = m.model.soonLine
    end if
    m.aL1.text = l1
    m.aL2.text = l2
    m.aL3.text = l3
    m.aL2.visible = l2 <> ""
    m.aL3.visible = l3 <> ""
    offs = [[0, 0], [160, 60], [320, 0], [320, 140], [160, 200], [0, 140]]
    o = offs[m.offsetIdx mod 6]
    m.ambBase.translation = [240 + o[0], 320 + o[1]]
end sub

sub updateDetail(r as Integer, c as Integer)
    if m.model = invalid then return
    if r >= m.model.sections.Count() then return
    cards = m.model.sections[r].cards
    if c >= cards.Count() then return
    k = cards[c]
    m.dTitle.text = k.dtitle
    y = 36 + m.dTitle.boundingRect().height
    m.dSub.visible = false
    m.dSubHot.visible = false
    if k.dsub <> ""
        s = m.dSub
        if k.dsoon then s = m.dSubHot
        s.text = k.dsub
        s.translation = [40, y + 12]
        s.visible = true
        y = y + 12 + 30
    end if
    for i = 0 to 5
        show = i < k.facts.Count()
        m.dLine[i].visible = show
        m.dFl[i].visible = show
        m.dFv[i].visible = show
    end for
    if k.facts.Count() > 0
        y = y + 28
        for i = 0 to k.facts.Count() - 1
            if i > 5 then exit for
            m.dLine[i].translation = [40, y]
            m.dFl[i].translation = [40, y + 20]
            m.dFv[i].translation = [200, y + 14]
            m.dFl[i].text = k.facts[i].l
            m.dFv[i].text = k.facts[i].v
            y = y + 64
        end for
        m.dLine[k.facts.Count() - 1].visible = true
        y = y + 2
    end if
    y = y + 28
    m.dHint.translation = [40, y]
    y = y + 30 + 32
    m.dBg.height = y
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
    updateCounter()
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
    m.hLogo.tint = pal("celadon")
    showDash()
    startFetch()
end sub
