' One GET <baseUrl>/v1/tv with the TV token. result = {ok: true, data: <json>} or {ok: false, error: "<why>"}.
sub init()
    m.top.functionName = "run"
end sub

sub run()
    base = m.top.baseUrl
    while Right(base, 1) = "/"
        base = Left(base, Len(base) - 1)
    end while
    req = CreateObject("roUrlTransfer")
    port = CreateObject("roMessagePort")
    req.SetMessagePort(port)
    req.SetUrl(base + "/v1/tv")
    req.AddHeader("Authorization", "Bearer " + m.top.token)
    req.AddHeader("Accept", "application/json")
    if Left(base, 5) = "https"
        req.SetCertificatesFile("common:/certs/ca-bundle.crt")
        req.InitClientCertificates()
    end if
    if not req.AsyncGetToString()
        m.top.result = {ok: false, error: "요청을 보낼 수 없어요"}
        return
    end if
    msg = wait(15000, port)
    if type(msg) <> "roUrlEvent"
        req.AsyncCancel()
        m.top.result = {ok: false, error: "응답이 없어요"}
        return
    end if
    code = msg.GetResponseCode()
    if code = 401
        m.top.result = {ok: false, error: "토큰이 맞지 않아요"}
    else if code <> 200
        m.top.result = {ok: false, error: "서버 오류 " + code.ToStr()}
    else
        data = ParseJson(msg.GetString())
        if data = invalid
            m.top.result = {ok: false, error: "응답을 읽을 수 없어요"}
        else
            m.top.result = {ok: true, data: data}
        end if
    end if
end sub
