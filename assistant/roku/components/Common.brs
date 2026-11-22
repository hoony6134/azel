' Shared by MainScene, DashCard and StatTile: colour tokens (SPEC.md 4.1), fonts, and time helpers.
' Design size is 1920x1080; Pretendard Regular + SemiBold are bundled (KS X 1001 subset).

function pal(name as String) as String
    c = {
        bg: "0x0D1113FF", ambient: "0x080B0CFF", raised: "0x131A1DFF", card: "0x182025FF", cardFocus: "0x222C32FF"
        rule: "0x26313AFF", text: "0xECF0ECFF", text2: "0xA9B4B0FF", text3: "0x8A9692FF"
        danger: "0xFF7A6EFF", amber: "0xE9BC6EFF", celadon: "0x82CBB4FF"
    }
    if c[name] <> invalid then return c[name]
    return c.text
end function

' accent token ("danger", "amber", "celadon", "indigo", "") -> colour; "" is the primary text colour
function accentColor(acc as String, fallback as String) as String
    if acc = "" then return pal(fallback)
    return pal(acc)
end function

function withAlpha(c as String, a as String) as String
    return Left(c, 8) + a
end function

function mkFont(weight as String, size as Integer) as Object
    f = CreateObject("roSGNode", "Font")
    if weight = "sb" then f.uri = "pkg:/fonts/PretendardKR-SemiBold.otf" else f.uri = "pkg:/fonts/PretendardKR-Regular.otf"
    f.size = size
    return f
end function

function mkLabel(parent as Object, x as Integer, y as Integer, w as Integer, h as Integer, size as Integer, weight as String, color as String) as Object
    l = CreateObject("roSGNode", "Label")
    l.translation = [x, y]
    l.width = w
    l.height = h
    l.color = color
    l.font = mkFont(weight, size)
    parent.appendChild(l)
    return l
end function

function mkPoster(parent as Object, uri as String, x as Integer, y as Integer, w as Integer, h as Integer, color as String) as Object
    p = CreateObject("roSGNode", "Poster")
    p.uri = uri
    p.translation = [x, y]
    p.width = w
    p.height = h
    p.blendColor = color
    parent.appendChild(p)
    return p
end function

function mkRect(parent as Object, x as Integer, y as Integer, w as Integer, h as Integer, color as String) as Object
    r = CreateObject("roSGNode", "Rectangle")
    r.translation = [x, y]
    r.width = w
    r.height = h
    r.color = color
    parent.appendChild(r)
    return r
end function

' ---------- time ----------

function nowEpoch() as Integer
    dt = CreateObject("roDateTime")
    return dt.AsSeconds()
end function

' "2026-11-22T23:59:00+09:00" -> epoch seconds (UTC); -1 when it can't be read
function isoEpoch(s as Dynamic) as Integer
    t = Type(s)
    if t <> "String" and t <> "roString" then return -1
    if Len(s) < 19 then return -1
    dt = CreateObject("roDateTime")
    dt.FromISO8601String(Left(s, 19) + "Z")
    sec = dt.AsSeconds()
    if sec <= 0 then return -1
    off = 0
    if Len(s) >= 25
        off = Int(Val(Mid(s, 21, 2))) * 3600 + Int(Val(Mid(s, 24, 2))) * 60
        if Mid(s, 20, 1) = "-" then off = -off
    end if
    return sec - off
end function

function localDt(epoch as Integer) as Object
    dt = CreateObject("roDateTime")
    dt.FromSeconds(epoch)
    dt.ToLocalTime()
    return dt
end function

function two(n as Integer) as String
    return Right("0" + n.ToStr(), 2)
end function

function fmtHM(epoch as Integer) as String
    dt = localDt(epoch)
    return two(dt.GetHours()) + ":" + two(dt.GetMinutes())
end function

function dayKey(epoch as Integer) as Integer
    dt = localDt(epoch)
    return dt.GetYear() * 10000 + dt.GetMonth() * 100 + dt.GetDayOfMonth()
end function

function weekdayKo(dt as Object) as String
    w = {Sunday: "일요일", Monday: "월요일", Tuesday: "화요일", Wednesday: "수요일", Thursday: "목요일", Friday: "금요일", Saturday: "토요일"}
    k = dt.GetWeekday()
    if w[k] <> invalid then return w[k]
    return ""
end function

' "11월 22일 일요일"
function dateKo(epoch as Integer) as String
    dt = localDt(epoch)
    return dt.GetMonth().ToStr() + "월 " + dt.GetDayOfMonth().ToStr() + "일 " + weekdayKo(dt)
end function

' 방금 / N분 전 / N시간 전 / 어제 HH:MM / M월 D일
function relAgo(epoch as Integer, now as Integer) as String
    d = now - epoch
    if d < 60 then return "방금"
    if d < 3600 then return Int(d / 60).ToStr() + "분 전"
    if d < 86400 and dayKey(epoch) = dayKey(now) then return Int(d / 3600).ToStr() + "시간 전"
    if dayKey(epoch) = dayKey(now - 86400) then return "어제 " + fmtHM(epoch)
    if d < 86400 then return Int(d / 3600).ToStr() + "시간 전"
    dt = localDt(epoch)
    return dt.GetMonth().ToStr() + "월 " + dt.GetDayOfMonth().ToStr() + "일"
end function

' "3시간" / "12분" / "2일" (the big figure on a deadline card; "남음" is its tail)
function remainBig(epoch as Integer, now as Integer) as String
    d = epoch - now
    if d < 60 then return "곧"
    if d < 3600 then return Int(d / 60).ToStr() + "분"
    if d < 86400 then return Int(d / 3600).ToStr() + "시간"
    return Int(d / 86400).ToStr() + "일"
end function

' N분 남음 / N시간 남음 / 내일 HH:MM / M월 D일
function remainText(epoch as Integer, now as Integer) as String
    d = epoch - now
    if d <= 0 then return "마감 지남"
    if d < 3600 then return Int(d / 60 + 1).ToStr() + "분 남음"
    if d < 86400 then return Int(d / 3600).ToStr() + "시간 남음"
    if dayKey(epoch) = dayKey(now + 86400) then return "내일 " + fmtHM(epoch)
    dt = localDt(epoch)
    return dt.GetMonth().ToStr() + "월 " + dt.GetDayOfMonth().ToStr() + "일"
end function

' 오늘 23:59 / 내일 09:00 / 11월 26일 수요일
function dueText(epoch as Integer, now as Integer) as String
    if dayKey(epoch) = dayKey(now) then return "오늘 " + fmtHM(epoch)
    if dayKey(epoch) = dayKey(now + 86400) then return "내일 " + fmtHM(epoch)
    return dateKo(epoch)
end function
