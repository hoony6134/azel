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
            nodes: [
                {name: "albireo", up: true, cpu: 34, ram: 61, disk: 48, temp: 58, seen: iso(now - 30)}
                {name: "deneb", up: true, cpu: 12, ram: 40, disk: 91, temp: 52, seen: iso(now - 30)}
                {name: "gienah", up: true, cpu: 71, ram: 77, disk: 55, temp: 74, seen: iso(now - 30)}
                {name: "ruchba", up: true, cpu: 6, ram: 56, disk: 22, temp: invalid, seen: iso(now - 30)}
                {name: "sadr", up: false, cpu: invalid, ram: invalid, disk: invalid, temp: invalid, seen: iso(now - 1320)}
            ]
            alerts: [{text: "deneb 디스크가 91% 찼어요", head: "디스크 부족", level: "warn", at: iso(now - 720)}]
            down: [{service: "searxng", node: "gienah", since: invalid}]
        }
        tasks: {
            running: [{goal: "블로그 초안 정리", since: iso(now - 840)}, {goal: "인프라 점검 보고서", since: iso(now - 300)}]
            queued: {count: 2, next: "블로그 초안 검토"}
            approvals: {count: 1, oldest: iso(now - 180)}
            overnight: {count: 3, starts: "01:00"}
            recent: [{goal: "서버 로그 요약", status: "done", at: iso(now - 3900)}]
        }
        gistra: {
            classes: [{start: hm(now - 1800), end: hm(now + 2700), name: "자료구조", room: "대학 A동 212호"}]
            assignments: [{title: "과제 4: 해시 테이블 구현", course: "자료구조", due: iso(now + 10800)}]
            todos: [{text: "컴파일러 프로젝트", due: iso(now + 90000)}]
        }
        calendar: [{start: hm(now + 7200), end: hm(now + 10800), what: "연구실 회의"}]
        calls: [{at: hm(now + 5400), what: "약 먹기"}]
    }
end function

function demoState(inCall as Boolean) as String
    if inCall then return "call"
    return "idle"
end function
