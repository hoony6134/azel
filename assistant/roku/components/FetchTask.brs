' One poll: GET <baseUrl>/v1/tv?v=2, falling back to /v1/tv when the gateway doesn't know v=2 (400, 404 or 422).
' result = {ok: true, v: 1|2, data: <json>} or {ok: false, code: <http code or 0>, error: "<why>"}.
sub init()
    m.top.functionName = "run"
end sub

sub run()
    base = m.top.baseUrl
    while Right(base, 1) = "/"
        base = Left(base, Len(base) - 1)
    end while
    if base = "demo" or base = "demo-call"
        m.top.result = {ok: true, v: 2, data: demoData(base = "demo-call")}
        return
    end if
    r = doGet(base + "/v1/tv?v=2")
    v = 2
    if r.code = 400 or r.code = 404 or r.code = 422
        r = doGet(base + "/v1/tv")
        v = 1
    end if
    if r.code = 401
        m.top.result = {ok: false, code: 401, error: "토큰이 맞지 않아요"}
    else if r.code = 0
        m.top.result = {ok: false, code: 0, error: "응답이 없어요"}
    else if r.code <> 200
        m.top.result = {ok: false, code: r.code, error: "서버 오류 " + r.code.ToStr()}
    else
        data = ParseJson(r.body)
        if data = invalid
            m.top.result = {ok: false, code: 200, error: "응답을 읽을 수 없어요"}
        else
            ' a gateway that predates v=2 ignores the parameter and answers with the v1 shape: go by what came back
            if data.DoesExist("gistra") then v = 2 else v = 1
            m.top.result = {ok: true, v: v, data: data}
        end if
    end if
end sub

function doGet(url as String) as Object
    req = CreateObject("roUrlTransfer")
    port = CreateObject("roMessagePort")
    req.SetMessagePort(port)
    req.SetUrl(url)
    req.AddHeader("Authorization", "Bearer " + m.top.token)
    req.AddHeader("Accept", "application/json")
    if Left(url, 5) = "https"
        req.SetCertificatesFile("common:/certs/ca-bundle.crt")
        req.InitClientCertificates()
    end if
    if not req.AsyncGetToString() then return {code: 0, body: ""}
    msg = wait(15000, port)
    if type(msg) <> "roUrlEvent"
        req.AsyncCancel()
        return {code: 0, body: ""}
    end if
    return {code: msg.GetResponseCode(), body: msg.GetString()}
end function

' Preview mode: set the server address to "demo" in the settings. Made-up data, times relative to now.
function demoData(inCall as Boolean) as Object
    now = CreateObject("roDateTime").AsSeconds()
    hm = function(t as Integer) as String
        dt = CreateObject("roDateTime")
        dt.FromSeconds(t)
        dt.ToLocalTime()
        return Right("0" + dt.GetHours().ToStr(), 2) + ":" + Right("0" + dt.GetMinutes().ToStr(), 2)
    end function
    iso = function(t as Integer) as String
        dt = CreateObject("roDateTime")
        dt.FromSeconds(t)
        return Left(dt.ToISOString(), 19) + "+00:00"
    end function
    return {
        at: iso(now)
        assistant: {state: demoState(inCall), persona: {name: "세레나", tint: "plum"}}
        infra: {
            asof: "17:04"
            hosts: [
                {name: "albireo", group: "cygnus", status: "ok", up: true, cpu: 1, mem: 69, disk: 15, temp: invalid, load: "0.08/2", uptime: "142d", seen: iso(now - 30)}
                {name: "deneb", group: "cygnus", status: "warn", up: true, cpu: 14, mem: 72, disk: 91, temp: invalid, load: "0.27/2", uptime: "159d", seen: iso(now - 30)}
                {name: "fawaris", group: "cygnus", status: "ok", up: true, cpu: 3, mem: 49, disk: 29, temp: 67, load: "0.07/4", uptime: "24d", seen: iso(now - 30)}
                {name: "gienah", group: "cygnus", status: "ok", up: true, cpu: 2, mem: 37, disk: 55, temp: 47, load: "0.03/4", uptime: "220d", seen: iso(now - 30)}
                {name: "sadr", group: "cygnus", status: "off", up: false, cpu: invalid, mem: invalid, disk: invalid, temp: invalid, load: invalid, uptime: invalid, seen: iso(now - 1320)}
                {name: "vd-core", group: "vujade", status: "ok", up: true, cpu: 1, mem: 24, disk: 25, temp: 45, load: "0.18/12", uptime: "54d", seen: iso(now - 30)}
                {name: "vd-spark", group: "vujade", status: "ok", up: true, cpu: 2, mem: 78, disk: 19, temp: 42, load: "0.30/20", uptime: "23h", seen: iso(now - 30)}
            ]
            alerts: [{text: "deneb 디스크가 91% 찼어요", head: "디스크 부족", level: "warn", at: iso(now - 720), group: "cygnus"}]
            down: [{service: "Vuja De claw (NemoClaw)", node: "vd-spark", group: "Vuja De GPU", type: "http", state: "down", note: "24h 0.00%", since: invalid}]
            notes: []
        }
        tasks: {
            running: [{goal: "블로그 초안 정리", since: iso(now - 840)}, {goal: "인프라 점검 보고서", since: iso(now - 300)}]
            queued: {count: 2, next: "블로그 초안 검토"}
            approvals: {count: 1, oldest: iso(now - 180)}
            overnight: {count: 3, starts: "01:00"}
            recent: [{goal: "서버 로그 요약", status: "done", at: iso(now - 3900)}]
        }
        gistra: {
            asof: "15:41"
            classes: [{start: hm(now - 1800), end: hm(now + 2700), name: "자료구조", room: "대학 A동 212호"}]
            assignments: [{title: "과제 4: 해시 테이블 구현", course: "자료구조", due: iso(now + 10800)}]
            todos: [{text: "하우스 문서 — 문서 부착 공지", title: "문서 부착 공지", kind: "하우스 문서", due: iso(now + 90000)}, {text: "컴파일러 프로젝트", title: "컴파일러 프로젝트", kind: "", due: iso(now + 90000)}, {text: "대외부 — 인수인계 기록지 작성", title: "인수인계 기록지 작성", kind: "대외부", due: invalid}]
            sections: [
                {title: "7일 안 마감", items: [{when: "11/23 23:59", due: iso(now + 90000), date: "2026-11-23", kind: "과기사관", title: "공결 확인서 전송 메일", note: "", tag: invalid}]}
                {title: "복습할 단원", items: [{when: invalid, due: invalid, date: invalid, kind: "현대 예술의 이해", title: "팝아트", note: "새로 배움 · 예정 10월 28일 (수)", tag: invalid}, {when: invalid, due: invalid, date: invalid, kind: "프로그래밍 언어 및 컴파일러", title: "Lexical Analysis (Token, Regex)", note: "익히는 중 · 예정 11월 1일 (일)", tag: invalid}]}
                {title: "LMS 알림 (최근 7일)", items: [{when: "11/22", due: invalid, date: "2026-11-22", kind: "현대 예술의 이해[01]", title: "강의 개요", note: "새 토론글이 등록되었습니다.", tag: invalid}]}
            ]
        }
        calendar: [{start: hm(now - 1800), end: hm(now + 2700), what: "자료구조", cal: "MyTT", where: "대학 A동 212호"}, {start: hm(now + 7200), end: hm(now + 10800), what: "연구실 회의", cal: "일반", where: ""}]
        calls: [{at: hm(now + 5400), what: "약 먹기"}]
        next_deadline: {title: "과제 4: 해시 테이블 구현", due: iso(now + 10800), course: "자료구조", source: "gistra"}
    }
end function

function demoState(inCall as Boolean) as String
    if inCall then return "call"
    return "idle"
end function
