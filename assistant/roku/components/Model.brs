' Turns the /v1/tv JSON (v2, or v1 as a fallback) into cards: sections of cards, the four summary tiles and the
' urgent line. Pure functions of (data, version, now): no UI here.

function newCard(t as String, kind as String, body as String, meta as String) as Object
    return {
        t: t, kind: kind, body: body, meta: meta, acc: "", hot: false, title: "", num: "", chip: "", chipc: ""
        m1: -1, m2: -1, m3: -1, m4: -1, dkind: kind, dtitle: body, dsub: "", dhot: false, facts: [], ts: 0
    }
end function

function nz(v as Dynamic) as Integer
    if v = invalid then return -1
    return Int(v)
end function

function sv(v as Dynamic) as String
    t = Type(v)
    if t = "String" or t = "roString" then return v
    return ""
end function

function arr(v as Dynamic) as Object
    if Type(v) = "roArray" then return v
    return []
end function

function fact(l as String, v as String) as Object
    return {l: l, v: v}
end function

function failCard(section as String) as Object
    c = newCard("fail", "불러오지 못했어요", section + "을 읽지 못했어요", "다음 갱신 때 다시 시도해요")
    c.dkind = "불러오지 못했어요"
    return c
end function

function emptyCard(word as String, copy as String) as Object
    c = newCard("empty", word, copy, "")
    c.dkind = word
    return c
end function

' due-ish card: "마감 3시간 전" style kind, hot amber when within 24 h
function dueCard(kind as String, title as String, course as String, due as Integer, now as Integer, src as String) as Object
    c = newCard("due", kind, title, "")
    c.title = title
    if course <> "" then c.kind = course + " · " + kind
    c.dkind = c.kind
    if due > 0
        remaining = due - now
        c.meta = dueText(due, now) + " · " + remainText(due, now)
        c.ts = due
        if remaining < 86400
            c.acc = "amber"
            c.hot = true
        end if
        c.dsub = c.meta
        c.dhot = c.hot
        c.facts.push(fact("마감", dateKo(due) + " " + fmtHM(due)))
    else
        c.meta = "마감 없음"
        c.dsub = "마감 없음"
    end if
    if course <> "" then c.facts.push(fact("과목", course))
    c.facts.push(fact("출처", src))
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

function nodeCard(n as Object, now as Integer) as Object
    name = sv(n.name)
    if n.up = false
        c = newCard("nodedown", "", "응답이 없어요", "")
        c.title = name
        seen = isoEpoch(n.seen)
        if seen > 0
            c.body = relAgo(seen, now) + " 이후 응답이 없어요"
            c.meta = "마지막 응답 " + fmtHM(seen)
        end if
        c.chip = "꺼짐"
        c.chipc = "danger"
        c.dkind = "노드"
        c.dtitle = name
        c.dsub = c.body
        c.facts.push(fact("마지막 응답", c.meta))
        return c
    end if
    c = newCard("node", "노드", name, "")
    c.title = name
    c.m1 = nz(n.cpu)
    c.m2 = nz(n.ram)
    c.m3 = nz(n.disk)
    c.m4 = nz(n.temp)
    c.dtitle = name
    c.facts.push(fact("CPU", pct(c.m1)))
    c.facts.push(fact("메모리", pct(c.m2)))
    c.facts.push(fact("디스크", pct(c.m3)))
    c.facts.push(fact("온도", degrees(c.m4)))
    return c
end function

function pct(v as Integer) as String
    if v < 0 then return "—"
    return v.ToStr() + "%"
end function

function degrees(v as Integer) as String
    if v < 0 then return "—"
    return v.ToStr() + "°"
end function

function countCard(kind as String, n as Integer, meta as String) as Object
    c = newCard("count", kind, "", meta)
    c.num = n.ToStr()
    c.dkind = kind
    c.dtitle = n.ToStr() + "개"
    c.dsub = meta
    return c
end function

' ---------- v2 ----------

function buildV2(d as Object, now as Integer) as Object
    infra = d.infra
    tasks = d.tasks
    gistra = d.gistra
    danger = []
    amber = []
    alerts = 0
    downSvc = 0
    approvals = 0
    running = 0
    queued = 0
    nextDue = -1
    nextDueTitle = ""

    if infra <> invalid
        for each a in arr(infra.alerts)
            alerts = alerts + 1
            c = newCard("alert", "경고", sv(a.text), "인프라")
            c.dtitle = sv(a.text)
            c.facts.push(fact("출처", "인프라"))
            at = isoEpoch(a.at)
            if at > 0 then c.meta = "인프라 · " + relAgo(at, now)
            if sv(a.level) = "crit"
                c.acc = "danger"
                c.hot = true
                danger.Push(c)
            else
                c.acc = "amber"
                c.hot = true
                amber.Push(c)
            end if
        end for
        for each s in arr(infra.down)
            downSvc = downSvc + 1
            c = newCard("alert", "서비스 꺼짐", sv(s.service) + " (" + sv(s.node) + ") 응답 없음", "인프라")
            c.acc = "danger"
            c.hot = true
            c.dtitle = c.body
            c.facts.push(fact("출처", "인프라"))
            danger.Push(c)
        end for
        for each n in arr(infra.nodes)
            if n.up = false
                c = newCard("alert", "노드 꺼짐", sv(n.name) + " 응답 없음", "인프라")
                c.acc = "danger"
                c.hot = true
                c.dtitle = c.body
                c.facts.push(fact("출처", "인프라"))
                danger.Push(c)
            end if
        end for
    end if

    if tasks <> invalid
        running = arr(tasks.running).Count()
        if tasks.queued <> invalid then queued = nz(tasks.queued["count"])
        if tasks.approvals <> invalid then approvals = nz(tasks.approvals["count"])
        if approvals > 0
            c = newCard("alert", "승인 필요", "승인을 기다리는 작업 " + approvals.ToStr() + "개. 휴대폰에서 확인해 주세요", "Azel 작업")
            c.acc = "amber"
            c.hot = true
            c.dtitle = c.body
            old = isoEpoch(tasks.approvals.oldest)
            if old > 0 then c.meta = "Azel 작업 · " + relAgo(old, now)
            c.facts.push(fact("출처", "Azel 작업"))
            amber.Push(c)
        end if
    end if

    due = []
    classes = []
    if gistra <> invalid
        for each a in arr(gistra.assignments)
            e = isoEpoch(a.due)
            c = dueCard("과제", sv(a.title), sv(a.course), e, now, "GISTRA")
            due.Push(c)
            if e > now and (nextDue < 0 or e < nextDue)
                nextDue = e
                nextDueTitle = sv(a.title)
            end if
            if c.hot and e > now then insertSorted(amber, makeUrgentDue(c, e, now))
        end for
        for each t in arr(gistra.todos)
            e = isoEpoch(t.due)
            c = newCard("todo", "할 일", sv(t.text), "GISTRA 할 일")
            c.dtitle = c.body
            c.facts.push(fact("출처", "GISTRA"))
            if e > 0
                c.meta = dueText(e, now) + " · " + remainText(e, now)
                c.dsub = c.meta
                c.facts.push(fact("마감", dateKo(e) + " " + fmtHM(e)))
                if e - now < 86400 and e > now
                    c.acc = "amber"
                    c.hot = true
                    c.dhot = true
                    u = newCard("due", "마감 " + remainShort(e, now), sv(t.text), dueText(e, now))
                    u.acc = "amber"
                    u.hot = true
                    u.ts = e
                    u.dtitle = u.body
                    u.dsub = c.meta
                    u.dhot = true
                    u.facts = c.facts
                    insertSorted(amber, u)
                end if
                if nextDue < 0 or (e > now and e < nextDue)
                    if e > now
                        nextDue = e
                        nextDueTitle = sv(t.text)
                    end if
                end if
            end if
            due.Push(c)
        end for
        for each k in arr(gistra.classes)
            classes.Push(k)
        end for
    end if

    ' tiles
    tiles = []
    t1 = {k: "경고", v: (alerts + downSvc).ToStr(), s2: "없음", hot: false, acc: ""}
    if downSvc > 0 then t1.s2 = "서비스 " + downSvc.ToStr() + " 꺼짐"
    if alerts + downSvc > 0
        t1.hot = true
        t1.acc = "danger"
    end if
    if infra = invalid
        t1.v = "—"
        t1.hot = false
        t1.acc = ""
    end if
    t2 = {k: "진행 중 작업", v: running.ToStr(), s2: "대기 " + queued.ToStr(), hot: false, acc: ""}
    if approvals > 0
        t2.s2 = "승인 " + approvals.ToStr() + " 대기"
        t2.hot = true
        t2.acc = "amber"
    end if
    if tasks = invalid
        t2.v = "—"
        t2.s2 = "없음"
        t2.hot = false
        t2.acc = ""
    end if
    t3 = {k: "다음 마감", v: "—", s2: "없음", hot: false, acc: ""}
    if nextDue > 0
        if dayKey(nextDue) = dayKey(now) then t3.v = fmtHM(nextDue) else t3.v = localDt(nextDue).GetMonth().ToStr() + "/" + localDt(nextDue).GetDayOfMonth().ToStr()
        t3.s2 = Left(nextDueTitle, 12)
        if nextDue - now < 86400
            t3.hot = true
            t3.acc = "amber"
        end if
    end if
    t4 = {k: "다음 통화", v: "—", s2: "없음", hot: false, acc: ""}
    nowHM = fmtHM(now)
    for each c in arr(d.calls)
        if sv(c.at) >= nowHM
            t4.v = sv(c.at)
            t4.s2 = Left(sv(c.what), 12)
            exit for
        end if
    end for
    tiles = [t1, t2, t3, t4]

    sections = []
    urgent = []
    for each c in danger
        urgent.Push(c)
    end for
    for each c in amber
        urgent.Push(c)
    end for
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", cards: urgent})

    ' 인프라
    cards = []
    if infra = invalid
        cards.Push(failCard("인프라"))
    else
        for each n in arr(infra.nodes)
            cards.Push(nodeCard(n, now))
        end for
        if cards.Count() = 0 then cards.Push(emptyCard("인프라", "노드 정보가 없어요"))
    end if
    sections.Push({title: "인프라", cards: cards})

    ' Azel 작업
    cards = []
    if tasks = invalid
        cards.Push(failCard("Azel 작업"))
    else
        for each r in arr(tasks.running)
            c = newCard("task", "", sv(r.goal), "")
            c.chip = "진행 중"
            c.chipc = "indigo"
            s = isoEpoch(r.since)
            if s > 0 then c.meta = Int((now - s) / 60).ToStr() + "분째"
            c.dkind = "진행 중"
            c.dsub = c.meta
            c.facts.push(fact("출처", "Azel 작업"))
            cards.Push(c)
        end for
        if tasks.queued <> invalid and queued > 0
            nxt = sv(tasks.queued["next"])
            mm = ""
            if nxt <> "" then mm = "다음: " + nxt
            cards.Push(countCard("대기 중", queued, mm))
        end if
        if tasks.overnight <> invalid and nz(tasks.overnight["count"]) > 0
            cards.Push(countCard("밤사이 할 일", nz(tasks.overnight["count"]), "새벽 " + Int(Val(Left(sv(tasks.overnight.starts), 2))).ToStr() + "시부터 차례로"))
        end if
        for each r in arr(tasks.recent)
            c = newCard("task", "", sv(r.goal), "")
            if sv(r.status) = "failed"
                c.chip = "실패"
                c.chipc = "danger"
            else
                c.chip = "끝남"
                c.chipc = "celadon"
            end if
            f = isoEpoch(r.at)
            if f > 0 then c.meta = relAgo(f, now)
            c.dkind = c.chip
            c.dsub = c.meta
            c.facts.push(fact("출처", "Azel 작업"))
            cards.Push(c)
        end for
        if cards.Count() = 0 then cards.Push(emptyCard("Azel 작업", "진행 중인 작업이 없어요"))
    end if
    sections.Push({title: "Azel 작업", cards: cards})

    ' GISTRA
    cards = []
    if gistra = invalid
        cards.Push(failCard("GISTRA"))
    else
        for each k in classes
            c = newCard("class", "", sv(k.name), sv(k.room))
            c.title = sv(k.start) + "–" + sv(k["end"])
            if sv(k.start) <= nowHM and nowHM < sv(k["end"])
                c.chip = "지금"
                c.chipc = "celadon"
            end if
            c.dkind = "수업"
            c.dsub = c.title
            c.facts.push(fact("시간", c.title))
            if sv(k.room) <> "" then c.facts.push(fact("강의실", sv(k.room)))
            c.facts.push(fact("출처", "GISTRA"))
            cards.Push(c)
        end for
        for each c in due
            cards.Push(c)
        end for
        if cards.Count() = 0 then cards.Push(emptyCard("GISTRA", "오늘 수업과 마감이 없어요"))
    end if
    sections.Push({title: "GISTRA 수업·과제", cards: cards})

    ' 일정·예약 통화
    cards = []
    if d.calendar = invalid
        cards.Push(failCard("캘린더"))
    else
        for each e in arr(d.calendar)
            c = newCard("event", "", sv(e.what), "캘린더")
            c.title = sv(e.start)
            if sv(e["end"]) <> "" then c.title = sv(e.start) + "–" + sv(e["end"])
            c.dkind = "일정"
            c.dsub = c.title
            c.facts.push(fact("시간", c.title))
            c.facts.push(fact("출처", "캘린더"))
            cards.Push(c)
        end for
    end if
    for each e in arr(d.calls)
        c = newCard("call", "", sv(e.what), "Azel이 전화해요")
        c.title = sv(e.at)
        c.chip = "예약 통화"
        c.chipc = "celadon"
        c.dkind = "예약 통화"
        c.dsub = sv(e.at)
        c.facts.push(fact("시간", sv(e.at)))
        c.facts.push(fact("출처", "Azel"))
        cards.Push(c)
    end for
    if cards.Count() = 0 then cards.Push(emptyCard("일정·통화", "오늘 남은 일정이 없어요"))
    sections.Push({title: "오늘 일정·예약 통화", cards: cards})

    urgentLine = ""
    if urgent.Count() > 0 then urgentLine = urgent[0].body
    return {sections: sections, tiles: tiles, urgentLine: urgentLine, alerts: alerts + downSvc, running: running, nextCall: t4.v}
end function

function remainShort(e as Integer, now as Integer) as String
    d = e - now
    if d < 3600 then return Int(d / 60 + 1).ToStr() + "분 전"
    return Int(d / 3600).ToStr() + "시간 전"
end function

' the urgent-row copy of a due card: "마감 3시간 전" kind and the plain title
function makeUrgentDue(c as Object, e as Integer, now as Integer) as Object
    u = newCard("due", "마감 " + remainShort(e, now), c.body, dueText(e, now))
    u.acc = "amber"
    u.hot = true
    u.ts = e
    u.dkind = c.dkind
    u.dtitle = c.body
    u.dsub = c.dsub
    u.dhot = true
    u.facts = c.facts
    return u
end function

' ---------- v1 (display strings only) ----------

function noneLine(s as String) as Boolean
    return Instr(1, s, "없음") > 0 or Instr(1, s, "No firing") > 0 or Instr(1, s, "정상") > 0
end function

function textCards(kind as String, lines as Dynamic, src as String) as Object
    out = []
    for each l in arr(lines)
        c = newCard("todo", kind, sv(l), src)
        c.dtitle = c.body
        c.facts.push(fact("출처", src))
        out.Push(c)
    end for
    return out
end function

function buildV1(d as Object, now as Integer) as Object
    infra = d.infra
    tasks = d.tasks
    todo = d.todo
    urgent = []
    alerts = 0
    approvals = 0
    running = 0
    queued = 0
    if infra <> invalid
        for each l in arr(infra.alerts)
            if not noneLine(sv(l))
                alerts = alerts + 1
                c = newCard("alert", "경고", sv(l), "인프라")
                c.acc = "danger"
                c.hot = true
                c.dtitle = c.body
                urgent.Push(c)
            end if
        end for
        for each l in arr(infra.problems)
            if not noneLine(sv(l))
                alerts = alerts + 1
                c = newCard("alert", "경고", sv(l), "인프라")
                c.acc = "danger"
                c.hot = true
                c.dtitle = c.body
                urgent.Push(c)
            end if
        end for
    end if
    if tasks <> invalid
        running = arr(tasks.running).Count()
        queued = nz(tasks.queued)
        approvals = nz(tasks.approvals)
        if approvals > 0
            c = newCard("alert", "승인 필요", "승인을 기다리는 작업 " + approvals.ToStr() + "개. 휴대폰에서 확인해 주세요", "Azel 작업")
            c.acc = "amber"
            c.hot = true
            c.dtitle = c.body
            urgent.Push(c)
        end if
    end if
    t1 = {k: "경고", v: alerts.ToStr(), s2: "없음", hot: alerts > 0, acc: ""}
    if alerts > 0 then t1.acc = "danger"
    if infra = invalid then t1 = {k: "경고", v: "—", s2: "없음", hot: false, acc: ""}
    t2 = {k: "진행 중 작업", v: running.ToStr(), s2: "대기 " + queued.ToStr(), hot: false, acc: ""}
    if approvals > 0
        t2.s2 = "승인 " + approvals.ToStr() + " 대기"
        t2.hot = true
        t2.acc = "amber"
    end if
    if tasks = invalid then t2 = {k: "진행 중 작업", v: "—", s2: "없음", hot: false, acc: ""}
    t3 = {k: "다음 마감", v: "—", s2: "없음", hot: false, acc: ""}
    t4 = {k: "다음 통화", v: "—", s2: "없음", hot: false, acc: ""}
    nowHM = fmtHM(now)
    for each c in arr(d.calls)
        if sv(c.at) >= nowHM
            t4.v = sv(c.at)
            t4.s2 = Left(sv(c.what), 12)
            exit for
        end if
    end for
    sections = []
    if urgent.Count() > 0 then sections.Push({title: "지금 확인할 것", cards: urgent})
    if infra = invalid
        sections.Push({title: "인프라", cards: [failCard("인프라")]})
    else
        cs = textCards("인프라", infra.health, "인프라")
        if cs.Count() = 0 then cs.Push(emptyCard("인프라", "노드 정보가 없어요"))
        sections.Push({title: "인프라", cards: cs})
    end if
    cs = []
    if tasks = invalid
        cs.Push(failCard("Azel 작업"))
    else
        for each r in arr(tasks.running)
            c = newCard("task", "", sv(r.goal), "")
            c.chip = "진행 중"
            c.chipc = "indigo"
            cs.Push(c)
        end for
        if queued > 0 then cs.Push(countCard("대기 중", queued, ""))
        for each r in arr(tasks.recent)
            c = newCard("task", "", sv(r.goal), "")
            c.chip = "끝남"
            c.chipc = "celadon"
            if sv(r.status) = "failed"
                c.chip = "실패"
                c.chipc = "danger"
            end if
            cs.Push(c)
        end for
        if cs.Count() = 0 then cs.Push(emptyCard("Azel 작업", "진행 중인 작업이 없어요"))
    end if
    sections.Push({title: "Azel 작업", cards: cs})
    if todo = invalid
        sections.Push({title: "GISTRA 수업·과제", cards: [failCard("GISTRA")]})
    else
        cs = textCards("오늘", todo.today, "GISTRA")
        for each c in textCards("과제·할 일", todo.assignments, "GISTRA")
            cs.Push(c)
        end for
        if cs.Count() = 0 then cs.Push(emptyCard("GISTRA", "오늘 수업과 마감이 없어요"))
        sections.Push({title: "GISTRA 수업·과제", cards: cs})
    end if
    cs = []
    if d.calendar = invalid
        cs.Push(failCard("캘린더"))
    else
        for each c in textCards("일정", d.calendar, "캘린더")
            cs.Push(c)
        end for
    end if
    for each e in arr(d.calls)
        c = newCard("call", "", sv(e.what), "Azel이 전화해요")
        c.title = sv(e.at)
        c.chip = "예약 통화"
        c.chipc = "celadon"
        cs.Push(c)
    end for
    if cs.Count() = 0 then cs.Push(emptyCard("일정·통화", "오늘 남은 일정이 없어요"))
    sections.Push({title: "오늘 일정·예약 통화", cards: cs})
    urgentLine = ""
    if urgent.Count() > 0 then urgentLine = urgent[0].body
    return {sections: sections, tiles: [t1, t2, t3, t4], urgentLine: urgentLine, alerts: alerts, running: running, nextCall: t4.v}
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
                d_t: c.t, d_kind: c.kind, d_body: c.body, d_meta: c.meta, d_acc: c.acc, d_hot: c.hot, d_title: c.title
                d_num: c.num, d_chip: c.chip, d_chipc: c.chipc, d_m1: c.m1, d_m2: c.m2, d_m3: c.m3, d_m4: c.m4
            })
        end for
    end for
    return root
end function
