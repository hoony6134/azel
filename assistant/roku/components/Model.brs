' Turns the /v1/tv JSON (v2, or v1 as a fallback) into sections of cards (SPEC.md 2.3, 5.4), the four strip figures
' and the ambient lines. Pure functions of (data, version, now): no UI here.

function newCard(t as String, w as Integer) as Object
    return {
        t: t, w: w, head: "", hl: "", sent: "", meta: "", when: "", tail: "", soon: false, now: false, name: ""
        title: "", v: [-1, -1, -1, -1], down: "", n1: "", l1: "", m1: "", n2: "", l2: "", m2: ""
        status: "", load: "", upt: "", note: "", ts: 0, dtitle: "", dsub: "", dsoon: false, facts: []
        series: [], labels: [], unit: "", icons: [], icon: "", h: 200
    }
end function

function sv(v as Dynamic) as String
    t = Type(v)
    if t = "String" or t = "roString" then return v
    return ""
end function

function nz(v as Dynamic) as Integer
    if v = invalid then return -1
    return Int(v)
end function

function arr(v as Dynamic) as Object
    if Type(v) = "roArray" then return v
    return []
end function

function fact(l as String, v as String) as Object
    return {l: l, v: v}
end function

' 이/가 by the last character: a Hangul syllable with a final consonant takes 이, anything else 가
function josaGa(word as String) as String
    n = Len(word)
    if n = 0 then return "가"
    code = Asc(Mid(word, n, 1))
    if code >= 44032 and code <= 55203 and (code - 44032) mod 28 <> 0 then return "이"
    return "가"
end function

function clean(s as String) as String
    s = s.Replace("<", "")
    return s.Replace(">", "").Trim()
end function

function failCard(w as Integer, section as String, still as String) as Object
    c = newCard("fail", w)
    c.sent = section + "을 읽지 못했어요" + still
    c.meta = "다음 갱신 때 다시 시도해요"
    c.dtitle = c.sent
    return c
end function

function emptyCard(w as Integer, copy as String) as Object
    c = newCard("empty", w)
    c.sent = copy
    c.dtitle = copy
    return c
end function

sub insertSorted(list as Object, c as Object)
    i = list.Count()
    while i > 0 and list[i - 1].ts > c.ts
        i = i - 1
    end while
    list.Push(invalid)
    j = list.Count() - 1
    while j > i
        list[j] = list[j - 1]
        j = j - 1
    end while
    list[i] = c
end sub

sub appendAll(dst as Object, src as Object)
    for each c in src
        dst.Push(c)
    end for
end sub

function urgentCard(level as String, head as String, sent as String, meta as String) as Object
    c = newCard("urgent", 414)
    c.hl = level
    c.head = head
    c.sent = clean(sent)
    c.meta = meta
    c.dtitle = head + " · " + clean(sent)
    c.dsub = meta
    c.dsoon = level = "soon"
    return c
end function

function dueWord(e as Integer, now as Integer) as String
    if dayKey(e) = dayKey(now) then return "오늘까지"
    if dayKey(e) = dayKey(now + 86400) then return "내일까지"
    return weekdayKo(localDt(e)) + "까지"
end function

' deadline card: big "3시간" + 남음, the title, "자료구조 · 오늘 23:59"
function dueCard(title as String, course as String, e as Integer, now as Integer) as Object
    c = newCard("due", 414)
    c.sent = clean(title)
    c.dtitle = c.sent
    if e > 0
        c.when = remainBig(e, now)
        c.tail = "남음"
        c.ts = e
        c.soon = e - now < 86400
        c.meta = dueText(e, now)
        if course <> "" then c.meta = course + " · " + c.meta
        c.dsub = remainText(e, now)
        if course <> "" then c.dsub = c.dsub + " · " + course
        c.dsoon = c.soon
        c.facts.Push(fact("마감", dateKo(e) + " " + fmtHM(e)))
    else
        c.meta = course
        c.when = "—"
    end if
    c.facts.Push(fact("출처", "GISTRA"))
    return c
end function

function hostCard(n as Object, now as Integer) as Object
    c = newCard("host", 414)
    c.name = sv(n.name)
    c.dtitle = c.name
    st = sv(n.status)
    if st = "" then
        st = "ok"
        if n.up = false then st = "off"
    end if
    c.status = st
    seen = isoEpoch(n.seen)
    mem = n["mem"]
    if mem = invalid then mem = n.ram
    c.v = [nz(n.cpu), nz(mem), nz(n.disk), nz(n.temp)]
    c.load = sv(n.load)
    c.upt = sv(n.uptime)
    words = {ok: "정상", warn: "주의", problem: "문제", off: "꺼짐"}
    c.dsub = sv(words[st])
    if st = "off"
        c.meta = ""
        if seen > 0 then c.meta = "마지막 응답 " + fmtHM(seen)
        if c.meta <> "" then c.facts.Push(fact("마지막 응답", fmtHM(seen)))
    else
        if seen > 0 then c.facts.Push(fact("마지막 응답", relAgo(seen, now)))
        if c.load <> "" then c.facts.Push(fact("부하", c.load))
        if c.upt <> "" then c.facts.Push(fact("가동 시간", c.upt))
    end if
    c.facts.Push(fact("상태", c.dsub))
    if sv(n.group) <> "" then c.facts.Push(fact("그룹", sv(n.group)))
    return c
end function

function hostGroup(c as Object, infra as Object) as String
    hl = infra.hosts
    if hl = invalid then return ""
    for each h in hl
        if sv(h.name) = c.name then return sv(h.group)
    end for
    return ""
end function

function knownTitle(cards as Object, title as String) as Boolean
    for each c in cards
        if c.dtitle = title or c.sent = title then return true
    end for
    return false
end function

' a generic GISTRA list item: its date on top, the title, then the course or category and the note
function itemCard(it as Object, now as Integer) as Object
    c = newCard("item", 414)
    ' dated items (a deadline, an LMS notice): the date on top, the title (with its note) and the course below;
    ' undated ones (a unit to review): the course on top, the title, and the note below
    ttl = clean(sv(it.title))
    if sv(it.when) <> ""
        c.title = sv(it.when)
        c.sent = ttl
        if sv(it.note) <> "" then c.sent = ttl + " — " + clean(sv(it.note))
        c.meta = sv(it.kind)
    else
        c.title = sv(it.kind)
        c.sent = ttl
        c.meta = sv(it.note)
    end if
    c.dtitle = ttl
    e = isoEpoch(it.due)
    if e > 0
        c.dsub = dueText(e, now) + " · " + remainText(e, now)
        c.soon = e - now < 86400 and e > now
        c.dsoon = c.soon
        c.facts.Push(fact("마감", dateKo(e) + " " + fmtHM(e)))
    else
        c.dsub = sv(it.when)
    end if
    if sv(it.kind) <> "" then c.facts.Push(fact("분류", sv(it.kind)))
    if sv(it.note) <> "" then c.facts.Push(fact("메모", sv(it.note)))
    c.facts.Push(fact("출처", "GISTRA"))
    return c
end function

' ---------- v2 ----------

function buildV2(d as Object, now as Integer) as Object
    infra = d.infra
    tasks = d.tasks
    gistra = d.gistra
    nowHM = fmtHM(now)
    down = []
    soonDue = []
    soonRest = []
    alertsCrit = 0
    downSvc = 0
    downNodes = 0
    approvals = 0
    running = 0
    queued = 0
    nextDue = -1
    nextDueTitle = ""
    nodes = []
    downNodeCards = []

    infraAsOf = ""
    if infra <> invalid
        infraAsOf = sv(infra.asof)
        for each s in arr(infra.down)
            where = sv(s.node)
            sent = sv(s.service) + josaGa(sv(s.service)) + " 응답하지 않아요"
            if where <> "" then sent = where + "의 " + sent
            meta = "인프라"
            if sv(s.note) <> "" then meta = "인프라 · " + sv(s.note)
            if sv(s.state) = "degraded"
                soonRest.Push(urgentCard("soon", "서비스 느림", sv(s.service) + " 응답이 느려요", meta))
            else
                downSvc = downSvc + 1
                down.Push(urgentCard("crit", "서비스 꺼짐", sent, meta))
            end if
        end for
        hostList = infra.hosts
        if hostList = invalid then hostList = infra.nodes
        for each n in arr(hostList)
            c = hostCard(n, now)
            st = c.status
            if st = "off" or st = "problem"
                downNodes = downNodes + 1
                seen = isoEpoch(n.seen)
                mins = ""
                meta = "인프라"
                if seen > 0
                    mins = " " + Int((now - seen) / 60).ToStr() + "분째"
                    meta = "인프라 · " + fmtHM(seen) + "부터"
                end if
                head = "노드 응답 없음"
                if st = "problem" then head = "노드 문제"
                down.Push(urgentCard("crit", head, sv(n.name) + josaGa(sv(n.name)) + mins + " 응답하지 않아요", meta))
                downNodeCards.Push(c)
            else
                if st = "warn" then soonRest.Push(urgentCard("soon", "노드 주의", sv(n.name) + "에 주의가 필요해요", "인프라"))
                nodes.Push(c)
            end if
        end for
        for each a in arr(infra.alerts)
            head = sv(a.head)
            if head = "" then head = "경고"
            at = isoEpoch(a.at)
            meta = "인프라"
            if sv(a.group) <> "" then meta = "인프라 · " + sv(a.group)
            if at > 0 then meta = meta + " · " + relAgo(at, now)
            if sv(a.level) = "crit"
                alertsCrit = alertsCrit + 1
                down.Push(urgentCard("crit", head, sv(a.text), meta))
            else
                soonRest.Push(urgentCard("soon", head, sv(a.text), meta))
            end if
        end for
    end if

    if tasks <> invalid
        running = arr(tasks.running).Count()
        if tasks.queued <> invalid then queued = nz(tasks.queued["count"])
        if tasks.approvals <> invalid then approvals = nz(tasks.approvals["count"])
        if approvals > 0
            old = isoEpoch(tasks.approvals.oldest)
            meta = "휴대폰에서 확인"
            if old > 0 then meta = meta + " · " + relAgo(old, now)
            soonRest.Unshift(urgentCard("soon", "승인 대기", "작업 " + approvals.ToStr() + "개가 승인을 기다려요", meta))
        end if
    end if

    assigns = []
    todos = []
    if gistra <> invalid
        for each a in arr(gistra.assignments)
            e = isoEpoch(a.due)
            c = dueCard(sv(a.title), sv(a.course), e, now)
            insertSorted(assigns, c)
            if e > now
                if nextDue < 0 or e < nextDue
                    nextDue = e
                    nextDueTitle = sv(a.title)
                end if
                if e - now < 86400
                    u = urgentCard("soon", "마감 " + remainBig(e, now) + " 전", sv(a.title), dueText(e, now))
                    u.ts = e
                    u.dtitle = u.head + " · " + u.sent
                    u.dsub = remainText(e, now) + " · " + sv(a.course)
                    u.facts = c.facts
                    insertSorted(soonDue, u)
                end if
            end if
        end for
        for each t in arr(gistra.todos)
            e = isoEpoch(t.due)
            c = newCard("todo", 414)
            title = sv(t["title"])
            if title = "" then title = sv(t.text)
            kind = sv(t.kind)
            if kind = "" then kind = "할 일"
            c.sent = clean(title)
            c.dtitle = c.sent
            c.meta = kind
            c.facts.Push(fact("출처", "GISTRA"))
            if kind <> "할 일" then c.facts.Push(fact("분류", kind))
            if e > 0
                c.meta = kind + " · " + dueWord(e, now)
                c.dsub = dueText(e, now) + " · " + remainText(e, now)
                c.facts.Unshift(fact("마감", dateKo(e) + " " + fmtHM(e)))
                if e > now
                    if nextDue < 0 or e < nextDue
                        nextDue = e
                        nextDueTitle = title
                    end if
                    if e - now < 86400
                        u = urgentCard("soon", "마감 " + remainBig(e, now) + " 전", title, dueText(e, now))
                        u.ts = e
                        u.dtitle = u.head + " · " + u.sent
                        u.dsub = remainText(e, now)
                        u.facts = c.facts
                        insertSorted(soonDue, u)
                    end if
                end if
            end if
            todos.Push(c)
        end for
    end if

    nd = d.next_deadline
    if nd <> invalid
        e = isoEpoch(nd.due)
        if e > now
            nextDue = e
            nextDueTitle = sv(nd.title)
        end if
    end if

    ' strip: 경고 = services down + nodes down + crit alerts (warn alerts only show in the urgent row)
    f1 = {k: "경고", v: (downSvc + downNodes + alertsCrit).ToStr(), t: "없음", acc: ""}
    if downSvc + downNodes > 0
        f1.t = "서비스 " + downSvc.ToStr() + " · 노드 " + downNodes.ToStr() + " 꺼짐"
    end if
    if downSvc + downNodes + alertsCrit > 0 then f1.acc = "danger"
    if infra = invalid then f1 = {k: "경고", v: "—", t: "없음", acc: ""}
    f2 = {k: "진행 중 작업", v: running.ToStr(), t: "승인 " + approvals.ToStr() + " · 대기 " + queued.ToStr(), acc: ""}
    if tasks = invalid then f2 = {k: "진행 중 작업", v: "—", t: "없음", acc: ""}
    f3 = {k: "다음 마감", v: "—", t: "없음", acc: ""}
    if nextDue > 0
        if dayKey(nextDue) = dayKey(now)
            f3.v = fmtHM(nextDue)
        else
            f3.v = localDt(nextDue).GetMonth().ToStr() + "/" + localDt(nextDue).GetDayOfMonth().ToStr()
        end if
        f3.t = nextDueTitle
        if nextDue - now < 86400 then f3.acc = "amber"
    end if
    f4 = {k: "다음 통화", v: "—", t: "없음", acc: ""}
    nextCall = ""
    for each c in arr(d.calls)
        if sv(c.at) >= nowHM
            f4.v = sv(c.at)
            f4.t = sv(c.what)
            nextCall = sv(c.at)
            exit for
        end if
    end for

    sections = []
    urgent = []
    appendAll(urgent, down)
    appendAll(urgent, soonDue)
    appendAll(urgent, soonRest)
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", w: 414, cap: 4, note: "", cards: urgent})

    ' 오늘: classes, calendar events and scheduled calls, one timetable from now to midnight
    slots = []
    ' the calendars first (the gateway puts MyTT, the timetable, ahead and merges the same event across calendars),
    ' then GISTRA's classes; a class already on the timetable at the same minute is not shown twice
    if d.calendar <> invalid
        for each e in arr(d.calendar)
            if sv(e.start) <> "종일"
                meta = sv(e.cal)
                if meta = "" then meta = "캘린더"
                if sv(e.where) <> "" then meta = meta + " · " + sv(e.where)
                addSlot(slots, sv(e.start), sv(e["end"]), sv(e.what), meta, nowHM)
            end if
        end for
    end if
    if gistra <> invalid
        for each k in arr(gistra.classes)
            addSlot(slots, sv(k.start), sv(k["end"]), sv(k.name), "수업 · " + sv(k.room), nowHM)
        end for
    end if
    for each e in arr(d.calls)
        addSlot(slots, sv(e.at), "", sv(e.what), "예약 통화 · Azel이 전화해요", nowHM)
    end for
    cards = []
    if d.calendar = invalid then cards.Push(failCard(560, "캘린더", ""))
    sorted = sortSlots(slots)
    appendAll(cards, sorted)
    if cards.Count() = 0 then cards.Push(emptyCard(560, "오늘 남은 일정이 없어요"))
    sections.Push({title: "오늘", w: 560, cap: 3, note: "", cards: cards})

    ' 날씨: now / high / low / rain, then the next 12 hours as two small bar charts (KMA only)
    if d.weather <> invalid then sections.Push(weatherSection(d.weather))

    ' 인프라: one row per host group (the [cygnus] / [vujade] of the health text), the ones that need a look first
    if infra = invalid
        sections.Push({title: "인프라", w: 414, cap: 4, note: "", cards: [failCard(414, "인프라", "")]})
    else
        all = []
        appendAll(all, downNodeCards)
        appendAll(all, nodes)
        groups = []
        for each c in all
            g = hostGroup(c, infra)
            found = false
            for each x in groups
                if x.g = g then found = true
            end for
            if not found then groups.Push({g: g})
        end for
        note = ""
        if infraAsOf <> "" then note = infraAsOf + " 기준"
        if all.Count() = 0
            sections.Push({title: "인프라", w: 414, cap: 4, note: note, cards: [emptyCard(414, "노드 정보가 없어요")]})
        else
            for each x in groups
                cs = []
                for each c in all
                    if hostGroup(c, infra) = x.g then cs.Push(c)
                end for
                title = "인프라"
                if groups.Count() > 1 and x.g <> "" then title = "인프라 · " + x.g
                sections.Push({title: title, w: 414, cap: 4, note: note, cards: cs})
            end for
        end if
    end if

    ' Azel 작업
    cards = []
    if tasks = invalid
        cards.Push(failCard(560, "Azel 작업", ""))
    else
        for each r in arr(tasks.running)
            c = newCard("task", 560)
            c.sent = clean(sv(r.goal))
            c.dtitle = c.sent
            c.meta = "진행 중"
            s = isoEpoch(r.since)
            if s > 0 then c.meta = "진행 중 · " + Int((now - s) / 60).ToStr() + "분째"
            c.facts.Push(fact("출처", "Azel 작업"))
            cards.Push(c)
        end for
        nOver = 0
        if tasks.overnight <> invalid then nOver = nz(tasks.overnight["count"])
        if queued > 0 or nOver > 0
            c = newCard("count", 560)
            blocks = []
            if queued > 0
                nxt = sv(tasks.queued["next"])
                m1 = ""
                if nxt <> "" then m1 = "다음: " + nxt
                blocks.Push({n: queued.ToStr(), l: "대기 중", mt: m1})
            end if
            if nOver > 0
                blocks.Push({n: nOver.ToStr(), l: "밤사이 할 일", mt: "새벽 " + Int(Val(Left(sv(tasks.overnight.starts), 2))).ToStr() + "시부터"})
            end if
            c.n1 = blocks[0].n
            c.l1 = blocks[0].l
            c.m1 = blocks[0].mt
            if blocks.Count() > 1
                c.n2 = blocks[1].n
                c.l2 = blocks[1].l
                c.m2 = blocks[1].mt
            end if
            c.dtitle = c.l1 + " " + c.n1 + "개"
            if c.n2 <> "" then c.dtitle = c.dtitle + " · " + c.l2 + " " + c.n2 + "개"
            cards.Push(c)
        end if
        shown = 0
        for each r in arr(tasks.recent)
            if shown < 2
                c = newCard("task", 560)
                c.sent = clean(sv(r.goal))
                c.dtitle = c.sent
                f = isoEpoch(r.at)
                word = "끝남"
                if sv(r.status) = "failed" then word = "실패"
                c.meta = word
                if f > 0 then c.meta = word + " · " + relAgo(f, now)
                c.facts.Push(fact("출처", "Azel 작업"))
                cards.Push(c)
                shown = shown + 1
            end if
        end for
        if cards.Count() = 0 then cards.Push(emptyCard(560, "진행 중인 작업이 없어요"))
    end if
    sections.Push({title: "Azel 작업", w: 560, cap: 3, note: "", cards: cards})

    ' 내 종목: price, change and five days of closes per holding; an unlisted one is just its name and 비상장
    if Type(d.holdings) = "roArray" and d.holdings.Count() > 0
        cs = []
        for each h in d.holdings
            cs.Push(stockCard(h))
        end for
        sections.Push({title: "내 종목", w: 414, cap: 4, note: "", cards: cs})
    end if

    ' 과제와 할 일 (남은 항목), then the other GISTRA lists as rows of their own: 7일 안 마감 (what is not already above),
    ' 복습할 단원, LMS 알림, and whatever else the server sends
    cards = []
    gnote = ""
    if gistra = invalid
        cards.Push(failCard(414, "GISTRA", ""))
    else
        if sv(gistra.asof) <> "" then gnote = sv(gistra.asof) + " 기준"
        appendAll(cards, assigns)
        appendAll(cards, todos)
        if cards.Count() = 0 then cards.Push(emptyCard(414, "마감이 가까운 과제가 없어요"))
    end if
    sections.Push({title: "과제와 할 일", w: 414, cap: 4, note: gnote, cards: cards})
    if gistra <> invalid
        for each sec in arr(gistra.sections)
            t = sv(sec.title)
            if t <> "남은 항목" and t <> "수업" and arr(sec.items).Count() > 0
                rows = []
                for each it in arr(sec.items)
                    if not (t = "7일 안 마감" and knownTitle(cards, sv(it.title)))
                        rows.Push(itemCard(it, now))
                    end if
                end for
                if rows.Count() > 0
                    t = Left(t, Instr(1, t + " (", " (") - 1)
                    sections.Push({title: "GISTRA · " + t, w: 414, cap: 4, note: gnote, cards: rows})
                end if
            end if
        end for
    end if

    return finishModel(sections, [f1, f2, f3, f4], urgent, downSvc + downNodes + alertsCrit, running, nextCall)
end function

sub addSlot(slots as Object, start as String, endT as String, what as String, meta as String, nowHM as String)
    if start = "" then return
    if endT <> "" and endT <= nowHM then return
    if endT = "" and start < nowHM then return
    for each o in slots
        if o.name = start and o.sent = clean(what) then return
    end for
    c = newCard("slot", 560)
    c.when = start
    if endT <> "" then c.tail = "–" + endT
    c.sent = clean(what)
    c.meta = meta
    c.now = endT <> "" and start <= nowHM and nowHM < endT
    c.name = start
    c.dtitle = c.sent
    c.dsub = start
    if endT <> "" then c.dsub = start + "–" + endT
    slots.Push(c)
end sub

' by start time (strings "HH:MM"); the one in progress stays first
function sortSlots(slots as Object) as Object
    out = []
    for each c in slots
        i = out.Count()
        while i > 0 and out[i - 1].name > c.name
            i = i - 1
        end while
        out.Push(invalid)
        j = out.Count() - 1
        while j > i
            out[j] = out[j - 1]
            j = j - 1
        end while
        out[i] = c
    end for
    return out
end function

function finishModel(sections as Object, figs as Object, urgent as Object, broken as Integer, running as Integer, nextCall as String) as Object
    brokenLine = ""
    soonLine = ""
    for each c in urgent
        if c.hl = "crit" and brokenLine = "" then brokenLine = c.head + " · " + c.sent
        if c.hl = "soon" and soonLine = "" then soonLine = c.head + " · " + c.sent
    end for
    return {sections: sections, figs: figs, broken: broken, running: running, nextCall: nextCall, brokenLine: brokenLine, soonLine: soonLine}
end function

' ---------- v1 (display strings only) ----------

function noneLine(s as String) as Boolean
    return Instr(1, s, "없음") > 0 or Instr(1, s, "No firing") > 0 or Instr(1, s, "정상") > 0
end function

' v1 lines of Cygnus text: markdown headers, the snapshot/"As of" stamps and "없음" are not items
function plainLine(l as String) as String
    t = l.Trim()
    if Left(t, 1) = "#" or Left(t, 4) = "스냅샷" or Left(t, 5) = "As of" or t = "없음" then return ""
    if Left(t, 2) = "- " then t = Mid(t, 3)
    return t
end function

function textCards(lines as Dynamic, w as Integer) as Object
    out = []
    for each l in arr(lines)
      if plainLine(sv(l)) <> "" then
        c = newCard("todo", w)
        c.sent = clean(plainLine(sv(l)))
        c.dtitle = c.sent
        out.Push(c)
      end if
    end for
    return out
end function

function buildV1(d as Object, now as Integer) as Object
    infra = d.infra
    tasks = d.tasks
    todo = d.todo
    nowHM = fmtHM(now)
    urgent = []
    broken = 0
    approvals = 0
    running = 0
    queued = 0
    if infra <> invalid
        for each l in arr(infra.alerts)
            if not noneLine(sv(l)) and plainLine(sv(l)) <> ""
                broken = broken + 1
                urgent.Push(urgentCard("crit", "경고", plainLine(sv(l)), "인프라"))
            end if
        end for
        for each l in arr(infra.problems)
            if not noneLine(sv(l)) and plainLine(sv(l)) <> ""
                broken = broken + 1
                urgent.Push(urgentCard("crit", "경고", plainLine(sv(l)), "인프라"))
            end if
        end for
    end if
    if tasks <> invalid
        running = arr(tasks.running).Count()
        queued = nz(tasks.queued)
        approvals = nz(tasks.approvals)
        if approvals > 0 then urgent.Push(urgentCard("soon", "승인 대기", "작업 " + approvals.ToStr() + "개가 승인을 기다려요", "휴대폰에서 확인"))
    end if
    f1 = {k: "경고", v: broken.ToStr(), t: "없음", acc: ""}
    if broken > 0 then f1.acc = "danger"
    if infra = invalid then f1 = {k: "경고", v: "—", t: "없음", acc: ""}
    f2 = {k: "진행 중 작업", v: running.ToStr(), t: "승인 " + approvals.ToStr() + " · 대기 " + queued.ToStr(), acc: ""}
    if tasks = invalid then f2 = {k: "진행 중 작업", v: "—", t: "없음", acc: ""}
    f3 = {k: "다음 마감", v: "—", t: "없음", acc: ""}
    f4 = {k: "다음 통화", v: "—", t: "없음", acc: ""}
    nextCall = ""
    for each c in arr(d.calls)
        if sv(c.at) >= nowHM
            f4.v = sv(c.at)
            f4.t = sv(c.what)
            nextCall = sv(c.at)
            exit for
        end if
    end for
    sections = []
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", w: 414, cap: 4, note: "", cards: urgent})
    cs = []
    if d.calendar = invalid then cs.Push(failCard(560, "캘린더", ""))
    for each e in arr(d.calls)
        addSlot(cs, sv(e.at), "", sv(e.what), "예약 통화 · Azel이 전화해요", nowHM)
    end for
    for each l in arr(d.calendar)
        t = sv(l)
        if Left(t, 2) = "- " then t = Mid(t, 3)
        ' all-day lines (LMS deadlines, holidays) are not a timetable; GISTRA and the strip carry those
        if Left(t, 4) <> "캘린더 " and t <> "일정 없음" and Instr(1, t, " 종일 ") = 0 then
            c = newCard("todo", 560)
            c.sent = clean(t)
            c.dtitle = c.sent
            cs.Push(c)
        end if
    end for
    if cs.Count() = 0 then cs.Push(emptyCard(560, "오늘 남은 일정이 없어요"))
    sections.Push({title: "오늘", w: 560, cap: 3, note: "", cards: cs})
    if infra = invalid
        sections.Push({title: "인프라", w: 414, cap: 4, note: "", cards: [failCard(414, "인프라", "")]})
    else
        cs = textCards(infra.health, 414)
        if cs.Count() = 0 then cs.Push(emptyCard(414, "노드 정보가 없어요"))
        sections.Push({title: "인프라", w: 414, cap: 4, note: "", cards: cs})
    end if
    cs = []
    if tasks = invalid
        cs.Push(failCard(560, "Azel 작업", ""))
    else
        for each r in arr(tasks.running)
            c = newCard("task", 560)
            c.sent = clean(sv(r.goal))
            c.dtitle = c.sent
            c.meta = "진행 중"
            cs.Push(c)
        end for
        for each r in arr(tasks.recent)
            c = newCard("task", 560)
            c.sent = clean(sv(r.goal))
            c.dtitle = c.sent
            c.meta = "끝남"
            if sv(r.status) = "failed" then c.meta = "실패"
            cs.Push(c)
        end for
        if cs.Count() = 0 then cs.Push(emptyCard(560, "진행 중인 작업이 없어요"))
    end if
    sections.Push({title: "Azel 작업", w: 560, cap: 3, note: "", cards: cs})
    if todo = invalid
        sections.Push({title: "과제와 할 일", w: 414, cap: 4, note: "", cards: [failCard(414, "GISTRA", "")]})
    else
        cs = textCards(todo.assignments, 414)
        appendAll(cs, textCards(todo.today, 414))
        if cs.Count() = 0 then cs.Push(emptyCard(414, "마감이 가까운 과제가 없어요"))
        sections.Push({title: "과제와 할 일", w: 414, cap: 4, note: "", cards: cs})
    end if
    return finishModel(sections, [f1, f2, f3, f4], urgent, broken, running, nextCall)
end function

function buildModel(d as Object, v as Integer, now as Integer) as Object
    if v = 2 then return buildV2(d, now)
    return buildV1(d, now)
end function

function deg(v as Dynamic) as String
    if v = invalid then return "—"
    n = Int(v + 0.5)
    if v < 0 then n = -Int(-v + 0.5)
    if n < 0 then return "-" + (-n).ToStr() + "°"
    return n.ToStr() + "°"
end function

function weatherSection(w as Object) as Object
    c = newCard("wx", 560)
    c.h = 232
    c.when = deg(w["now"])
    c.tail = sv(w.sky)
    c.icon = sv(w.icon)
    parts = []
    if w.high <> invalid then parts.Push("최고 " + deg(w.high))
    if w.low <> invalid then parts.Push("최저 " + deg(w.low))
    if w.pop <> invalid then parts.Push("비 올 확률 " + Int(w.pop).ToStr() + "%")
    c.sent = parts.Join("  ·  ")
    if w.pop <> invalid
        c.soon = w.pop >= 60
    end if
    c.meta = sv(w.source) + " · " + sv(w.place)
    c.dtitle = "지금 " + sv(w.place) + " " + c.when + " " + c.tail
    c.dsub = c.sent
    c.facts.Push(fact("출처", sv(w.source)))
    cards = [c]
    hrs = arr(w.hourly)
    if hrs.Count() >= 3
        t = newCard("chart", 560)
        p = newCard("chart", 560)
        t.h = 232
        p.h = 232
        t.title = "기온 °C"
        p.title = "비 올 확률 %"
        ' one bar every 2 hours across the same 12 hours: six bars with room for an icon and a value each.
        ' The weather row is taller than the others (232, not 200), so icon, value, bar and hour each get a line.
        idx = -1
        for each h in hrs
            idx = idx + 1
            if idx mod 2 = 0 and t.series.Count() < 6
                t.series.Push(h.t)
                pv = h.pop
                if pv = invalid then pv = 0
                p.series.Push(pv)
                t.labels.Push(sv(h.h))
                p.labels.Push(sv(h.h))
                t.icons.Push(sv(h.icon))
            end if
        end for
        lo = 1000.0
        hi = -1000.0
        for each v in t.series
            if v <> invalid
                if v < lo then lo = v
                if v > hi then hi = v
            end if
        end for
        t.tail = "최저 " + deg(lo) + " · 최고 " + deg(hi)
        p.tail = "0 ~ 100"
        t.meta = "앞으로 12시간"
        p.meta = "60% 넘으면 호박색"
        t.dtitle = "앞으로 12시간 기온 " + t.tail
        p.dtitle = "앞으로 12시간 비 올 확률"
        p.unit = "%"
        t.unit = "°"
        cards.Push(t)
        cards.Push(p)
    end if
    return {title: "날씨", w: 560, h: 232, cap: 3, note: "", cards: cards}
end function

function stockCard(h as Object) as Object
    c = newCard("stock", 414)
    c.name = sv(h.name)
    c.dtitle = c.name
    unlisted = false
    if Type(h.listed) = "Boolean" or Type(h.listed) = "roBoolean"
        unlisted = not h.listed
    end if
    if unlisted
        c.status = "unlisted"
        c.sent = "비상장"
        return c
    end if
    if h.price = invalid
        c.status = "noprice"
        c.sent = "시세를 못 받았어요"
        return c
    end if
    c.when = money(h.price, sv(h.currency))
    if h.pct <> invalid
        p = h.pct
        if p > 0
            c.hl = "up"
            c.tail = fmt2(p) + "%"
        else if p < 0
            c.hl = "down"
            c.tail = fmt2(-p) + "%"
        else
            c.tail = "0.00%"
        end if
    end if
    for each v in arr(h.spark)
        c.series.Push(v)
    end for
    c.meta = sv(h.source)
    if sv(h.ticker) <> "" then c.meta = sv(h.ticker) + " · " + c.meta
    sign = ""
    if c.hl = "up" then sign = "+"
    if c.hl = "down" then sign = "-"
    c.dsub = c.when + "  " + sign + c.tail
    c.facts.Push(fact("5일", sparkText(c.series, sv(h.currency))))
    return c
end function

function fmt2(v as Float) as String
    n = Int(v * 100 + 0.5)
    return Int(n / 100).ToStr() + "." + Right("0" + (n mod 100).ToStr(), 2)
end function

' 180,900원 / $342.56
function money(v as Float, cur as String) as String
    if cur = "KRW" or cur = "JPY"
        s = Int(v + 0.5).ToStr()
        out = ""
        while Len(s) > 3
            out = "," + Right(s, 3) + out
            s = Left(s, Len(s) - 3)
        end while
        unit = "원"
        if cur = "JPY" then unit = "엔"
        return s + out + unit
    end if
    if cur = "USD" then return "$" + fmt2(v)
    if cur = "index" then return fmt2(v)
    return fmt2(v) + " " + cur
end function

function sparkText(series as Object, cur as String) as String
    out = []
    for each v in series
        out.Push(money(v, cur))
    end for
    return out.Join(" → ")
end function

function makeContent(sections as Object) as Object
    root = CreateObject("roSGNode", "ContentNode")
    for each sec in sections
        row = root.CreateChild("ContentNode")
        row.title = sec.title
        row.AddFields({d_note: sec.note})
        for each c in sec.cards
            item = row.CreateChild("ContentNode")
            item.AddFields({
                d_t: c.t, d_w: c.w, d_head: c.head, d_hl: c.hl, d_sent: c.sent, d_meta: c.meta, d_when: c.when
                d_tail: c.tail, d_soon: c.soon, d_now: c.now, d_name: c.name, d_v1: c.v[0], d_v2: c.v[1]
                d_v3: c.v[2], d_v4: c.v[3], d_down: c.down, d_n1: c.n1, d_l1: c.l1, d_m1: c.m1, d_n2: c.n2
                d_l2: c.l2, d_m2: c.m2, d_status: c.status, d_load: c.load, d_upt: c.upt, d_title: c.title
                d_series: c.series, d_labels: c.labels, d_unit: c.unit, d_icons: c.icons, d_icon: c.icon, d_h: c.h
            })
        end for
    end for
    return root
end function
