' Turns the /v1/tv JSON (v2, or v1 as a fallback) into sections of cards (SPEC.md 2.3, 5.4), the four strip figures
' and the ambient lines. Pure functions of (data, version, now): no UI here.

function newCard(t as String, w as Integer) as Object
    return {
        t: t, w: w, head: "", hl: "", sent: "", meta: "", when: "", tail: "", soon: false, now: false, name: ""
        v: [-1, -1, -1, -1], down: "", n1: "", l1: "", m1: "", n2: "", l2: "", m2: ""
        ts: 0, dtitle: "", dsub: "", dsoon: false, facts: []
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
    return s.Replace(">", "")
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

function nodeCard(n as Object, now as Integer) as Object
    c = newCard("node", 268)
    c.name = sv(n.name)
    c.dtitle = c.name
    seen = isoEpoch(n.seen)
    if n.up = false
        c.t = "nodedown"
        c.down = "응답 없음"
        if seen > 0
            c.down = Int((now - seen) / 60).ToStr() + "분째 응답 없음"
            c.meta = "마지막 응답 " + fmtHM(seen)
            c.facts.Push(fact("마지막 응답", fmtHM(seen)))
        end if
        c.facts.Push(fact("상태", "응답 없음"))
        c.dsub = c.down
        return c
    end if
    c.v = [nz(n.cpu), nz(n.ram), nz(n.disk), nz(n.temp)]
    if seen > 0 then c.facts.Push(fact("마지막 응답", relAgo(seen, now)))
    c.facts.Push(fact("상태", "정상"))
    return c
end function

function strHM(s as String) as String
    return s
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

    if infra <> invalid
        for each s in arr(infra.down)
            downSvc = downSvc + 1
            down.Push(urgentCard("crit", "서비스 꺼짐", sv(s.node) + "의 " + sv(s.service) + josaGa(sv(s.service)) + " 응답하지 않아요", "인프라"))
        end for
        for each n in arr(infra.nodes)
            if n.up = false
                downNodes = downNodes + 1
                seen = isoEpoch(n.seen)
                mins = ""
                meta = "인프라"
                if seen > 0
                    mins = " " + Int((now - seen) / 60).ToStr() + "분째"
                    meta = "인프라 · " + fmtHM(seen) + "부터"
                end if
                down.Push(urgentCard("crit", "노드 응답 없음", sv(n.name) + josaGa(sv(n.name)) + mins + " 응답하지 않아요", meta))
                downNodeCards.Push(nodeCard(n, now))
            else
                nodes.Push(nodeCard(n, now))
            end if
        end for
        for each a in arr(infra.alerts)
            head = sv(a.head)
            if head = "" then head = "경고"
            at = isoEpoch(a.at)
            meta = "인프라"
            if at > 0 then meta = "인프라 · " + relAgo(at, now)
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
            c.sent = clean(sv(t.text))
            c.dtitle = c.sent
            c.meta = "할 일"
            c.facts.Push(fact("출처", "GISTRA"))
            if e > 0
                c.meta = "할 일 · " + dueWord(e, now)
                c.dsub = dueText(e, now) + " · " + remainText(e, now)
                c.facts.Unshift(fact("마감", dateKo(e) + " " + fmtHM(e)))
                if e > now
                    if nextDue < 0 or e < nextDue
                        nextDue = e
                        nextDueTitle = sv(t.text)
                    end if
                    if e - now < 86400
                        u = urgentCard("soon", "마감 " + remainBig(e, now) + " 전", sv(t.text), dueText(e, now))
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
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", w: 414, cap: 4, cards: urgent})

    ' 오늘: classes, calendar events and scheduled calls, one timetable from now to midnight
    slots = []
    if gistra <> invalid
        for each k in arr(gistra.classes)
            addSlot(slots, sv(k.start), sv(k["end"]), sv(k.name), "수업 · " + sv(k.room), nowHM)
        end for
    end if
    if d.calendar <> invalid
        for each e in arr(d.calendar)
            if sv(e.start) <> "종일" then addSlot(slots, sv(e.start), sv(e["end"]), sv(e.what), "캘린더", nowHM)
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
    sections.Push({title: "오늘", w: 560, cap: 3, cards: cards})

    ' 인프라: down nodes first
    cards = []
    if infra = invalid
        cards.Push(failCard(414, "인프라", ""))
    else
        appendAll(cards, downNodeCards)
        appendAll(cards, nodes)
        if cards.Count() = 0 then cards.Push(emptyCard(414, "노드 정보가 없어요"))
    end if
    w = 268
    if infra = invalid or cards.Count() = 1 and cards[0].t <> "node" and cards[0].t <> "nodedown" then w = 414
    sections.Push({title: "인프라", w: w, cap: 6, cards: cards})

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
    sections.Push({title: "Azel 작업", w: 560, cap: 3, cards: cards})

    ' 과제와 할 일
    cards = []
    if gistra = invalid
        cards.Push(failCard(414, "GISTRA", ""))
    else
        appendAll(cards, assigns)
        appendAll(cards, todos)
        if cards.Count() = 0 then cards.Push(emptyCard(414, "마감이 가까운 과제가 없어요"))
    end if
    sections.Push({title: "과제와 할 일", w: 414, cap: 4, cards: cards})

    return finishModel(sections, [f1, f2, f3, f4], urgent, downSvc + downNodes + alertsCrit, running, nextCall)
end function

sub addSlot(slots as Object, start as String, endT as String, what as String, meta as String, nowHM as String)
    if start = "" then return
    if endT <> "" and endT <= nowHM then return
    if endT = "" and start < nowHM then return
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

function textCards(lines as Dynamic, w as Integer) as Object
    out = []
    for each l in arr(lines)
        c = newCard("todo", w)
        c.sent = clean(sv(l))
        c.dtitle = c.sent
        out.Push(c)
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
            if not noneLine(sv(l))
                broken = broken + 1
                urgent.Push(urgentCard("crit", "경고", sv(l), "인프라"))
            end if
        end for
        for each l in arr(infra.problems)
            if not noneLine(sv(l))
                broken = broken + 1
                urgent.Push(urgentCard("crit", "경고", sv(l), "인프라"))
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
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", w: 414, cap: 4, cards: urgent})
    cs = []
    if d.calendar = invalid then cs.Push(failCard(560, "캘린더", ""))
    for each e in arr(d.calls)
        addSlot(cs, sv(e.at), "", sv(e.what), "예약 통화 · Azel이 전화해요", nowHM)
    end for
    for each l in arr(d.calendar)
        t = sv(l)
        if Left(t, 2) = "- " then t = Mid(t, 3)
        if Left(t, 4) <> "캘린더 " and t <> "일정 없음" then
            c = newCard("todo", 560)
            c.sent = clean(t)
            c.dtitle = c.sent
            cs.Push(c)
        end if
    end for
    if cs.Count() = 0 then cs.Push(emptyCard(560, "오늘 남은 일정이 없어요"))
    sections.Push({title: "오늘", w: 560, cap: 3, cards: cs})
    if infra = invalid
        sections.Push({title: "인프라", w: 414, cap: 4, cards: [failCard(414, "인프라", "")]})
    else
        cs = textCards(infra.health, 414)
        if cs.Count() = 0 then cs.Push(emptyCard(414, "노드 정보가 없어요"))
        sections.Push({title: "인프라", w: 414, cap: 4, cards: cs})
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
    sections.Push({title: "Azel 작업", w: 560, cap: 3, cards: cs})
    if todo = invalid
        sections.Push({title: "과제와 할 일", w: 414, cap: 4, cards: [failCard(414, "GISTRA", "")]})
    else
        cs = textCards(todo.assignments, 414)
        appendAll(cs, textCards(todo.today, 414))
        if cs.Count() = 0 then cs.Push(emptyCard(414, "마감이 가까운 과제가 없어요"))
        sections.Push({title: "과제와 할 일", w: 414, cap: 4, cards: cs})
    end if
    return finishModel(sections, [f1, f2, f3, f4], urgent, broken, running, nextCall)
end function

function buildModel(d as Object, v as Integer, now as Integer) as Object
    if v = 2 then return buildV2(d, now)
    return buildV1(d, now)
end function

function makeContent(sections as Object) as Object
    root = CreateObject("roSGNode", "ContentNode")
    for each sec in sections
        row = root.CreateChild("ContentNode")
        row.title = sec.title
        for each c in sec.cards
            item = row.CreateChild("ContentNode")
            item.AddFields({
                d_t: c.t, d_w: c.w, d_head: c.head, d_hl: c.hl, d_sent: c.sent, d_meta: c.meta, d_when: c.when
                d_tail: c.tail, d_soon: c.soon, d_now: c.now, d_name: c.name, d_v1: c.v[0], d_v2: c.v[1]
                d_v3: c.v[2], d_v4: c.v[3], d_down: c.down, d_n1: c.n1, d_l1: c.l1, d_m1: c.m1, d_n2: c.n2
                d_l2: c.l2, d_m2: c.m2
            })
        end for
    end for
    return root
end function
