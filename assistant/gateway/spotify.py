"""Spotify Web API tools on oauth.py's grant (name "spotify"), for every model path and voice.

Playback control needs Spotify Premium and an awake Spotify app (desktop, phone or a Connect speaker): with none,
Spotify answers "no active device" and the tool says to open Spotify. The client id comes from the user's own
Spotify Developer app (SPOTIFY.md). Results are one short Korean sentence; tokens never leave oauth.py and the
Spotify API.

Tools:
  spotify_play {query | uri, device?}   a song, artist, album or playlist by words, or an exact spotify: URI
  spotify_control {action, volume?}     pause | resume | next | previous | volume (0-100) | shuffle (on/off)
  spotify_now_playing {}                what is playing, on which device
  spotify_devices {}                    the devices Spotify can play on
"""
import re

import httpx

import oauth

API = "https://api.spotify.com/v1"
NAME = "spotify"
URI_RE = re.compile(r"spotify:(track|album|artist|playlist):[A-Za-z0-9]{10,40}")
DEFS = [
    {"name": "spotify_play", "description": "스포티파이에서 노래·가수·앨범·플레이리스트를 튼다. query는 사용자가 말한 그대로"
                                            "(예: '아이유 밤편지', '아이유', '잔잔한 플레이리스트'), 정확한 spotify: URI가 있으면 uri. "
                                            "device는 기기 이름(없으면 지금 쓰는 기기).",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "query": {"type": "string", "maxLength": 200}, "uri": {"type": "string", "maxLength": 80},
         "device": {"type": "string", "maxLength": 80}}}},
    {"name": "spotify_control", "description": "스포티파이 재생 제어: pause(멈춤), resume(다시 재생), next(다음 곡), "
                                               "previous(이전 곡), volume(0~100, volume에 숫자), shuffle(셔플, on에 true/false).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "volume", "shuffle"]},
         "volume": {"type": "integer", "minimum": 0, "maximum": 100}, "on": {"type": "boolean"}}}},
    {"name": "spotify_now_playing", "description": "스포티파이에서 지금 재생 중인 곡과 기기.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "spotify_devices", "description": "스포티파이로 틀 수 있는 기기 목록(지금 켜져 있는 것만).",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
]
NAMES = {t["name"] for t in DEFS}


class Problem(Exception):
    """A spoken answer instead of a result (no device, not Premium, nothing found)."""


def bridge(home):
    """(definitions, run) once the user has connected Spotify, else ([], None)."""
    if oauth.status(home, NAME) != "connected":
        return [], None

    async def run(name, args):
        return await call(home, name, args)
    return DEFS, run


def _check(name, a):
    a = a if isinstance(a, dict) else {}
    clean = lambda v, n: v.strip() if isinstance(v, str) and v.strip() and len(v) <= n and \
        not any(ord(c) < 32 for c in v) else None  # noqa: E731
    if name == "spotify_play":
        q, uri, dev = clean(a.get("query"), 200), a.get("uri"), clean(a.get("device"), 80)
        if uri is not None and not (isinstance(uri, str) and URI_RE.fullmatch(uri)):
            raise ValueError("uri는 spotify:track:… 같은 형식")
        if not q and not uri:
            raise ValueError("query나 uri 중 하나")
        return {"query": q, "uri": uri, "device": dev}
    if name == "spotify_control":
        act = a.get("action")
        if act not in ("pause", "resume", "next", "previous", "volume", "shuffle"):
            raise ValueError("action")
        vol = a.get("volume")
        if act == "volume" and not (isinstance(vol, int) and not isinstance(vol, bool) and 0 <= vol <= 100):
            raise ValueError("volume은 0~100")
        return {"action": act, "volume": vol, "on": a.get("on") is not False}
    if name in ("spotify_now_playing", "spotify_devices"):
        return {}
    raise KeyError(name)


async def _api(http, token, method, path, **kw):
    r = await http.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code == 204 or (r.status_code < 300 and not r.content):
        return None
    if r.status_code == 404 and "NO_ACTIVE_DEVICE" in r.text:
        raise Problem("켜진 스포티파이 기기가 없어요. 휴대폰이나 맥에서 스포티파이를 한 번 열어 주세요.")
    if r.status_code == 403 and "PREMIUM" in r.text.upper():
        raise Problem("재생 제어는 스포티파이 프리미엄에서만 돼요.")
    if r.status_code == 401:
        raise oauth.Expired()
    if r.status_code == 429:
        raise Problem("스포티파이가 잠시 요청을 막았어요. 조금 뒤에 다시 해 주세요.")
    r.raise_for_status()
    return r.json()


def _norm(s):
    return re.sub(r"\W", "", (s or "").lower())


async def _device_id(http, token, wanted):
    """The device to use: the one named (substring), else the active one (None lets Spotify pick it), else the
    first available (a cold start would otherwise fail with no active device)."""
    devs = (await _api(http, token, "GET", "/me/player/devices") or {}).get("devices") or []
    if wanted:
        hit = next((d for d in devs if _norm(wanted) in _norm(d.get("name"))), None)
        if not hit:
            raise Problem(f"'{wanted[:30]}' 기기를 찾지 못했어요. 스포티파이가 켜져 있는지 봐 주세요.")
        return hit["id"], hit.get("name", "")
    if any(d.get("is_active") for d in devs):
        return None, ""
    if devs:
        return devs[0]["id"], devs[0].get("name", "")
    raise Problem("켜진 스포티파이 기기가 없어요. 휴대폰이나 맥에서 스포티파이를 한 번 열어 주세요.")


async def _find(http, token, query):
    """(body for /me/player/play, spoken name): the artist when the words name one exactly, a playlist when asked
    for one, else the top track."""
    playlist = re.search(r"플레이\s*리스트|playlist", query, re.I)
    types = "playlist" if playlist else "track,artist"
    q = re.sub(r"(플레이\s*리스트|playlist|노래|틀어\s*줘|들려\s*줘)", " ", query, flags=re.I).strip() or query
    res = await _api(http, token, "GET", "/search", params={"q": q, "type": types, "limit": 5}) or {}
    if playlist:
        items = [p for p in (res.get("playlists") or {}).get("items") or [] if p]
        if items:
            return {"context_uri": items[0]["uri"]}, f"'{items[0].get('name', '')}' 플레이리스트"
    artists = (res.get("artists") or {}).get("items") or []
    if artists and _norm(artists[0].get("name")) == _norm(q):
        return {"context_uri": artists[0]["uri"]}, f"{artists[0]['name']}의 노래"
    tracks = (res.get("tracks") or {}).get("items") or []
    if tracks:
        t = tracks[0]
        who = ", ".join(a.get("name", "") for a in t.get("artists") or [])
        return {"uris": [t["uri"]]}, f"{who}의 '{t.get('name', '')}'" if who else f"'{t.get('name', '')}'"
    raise Problem(f"스포티파이에서 '{query[:30]}'을 찾지 못했어요.")


async def _play(http, token, a):
    if a["uri"]:
        body = {"uris": [a["uri"]]} if a["uri"].startswith("spotify:track:") else {"context_uri": a["uri"]}
        what = "요청한 곡"
    else:
        body, what = await _find(http, token, a["query"])
    dev, dev_name = await _device_id(http, token, a["device"])
    await _api(http, token, "PUT", "/me/player/play", params={"device_id": dev} if dev else None, json=body)
    return f"{dev_name}에서 {what}을 틀었어요." if dev_name else f"{what}을 틀었어요."


async def _control(http, token, a):
    act = a["action"]
    if act == "pause":
        await _api(http, token, "PUT", "/me/player/pause")
        return "멈췄어요."
    if act == "resume":
        await _api(http, token, "PUT", "/me/player/play")
        return "다시 틀었어요."
    if act in ("next", "previous"):
        await _api(http, token, "POST", f"/me/player/{act}")
        return "다음 곡으로 넘겼어요." if act == "next" else "이전 곡으로 돌렸어요."
    if act == "volume":
        await _api(http, token, "PUT", "/me/player/volume", params={"volume_percent": a["volume"]})
        return f"볼륨을 {a['volume']}으로 맞췄어요."
    await _api(http, token, "PUT", "/me/player/shuffle", params={"state": "true" if a["on"] else "false"})
    return "셔플을 켰어요." if a["on"] else "셔플을 껐어요."


async def _now(http, token, a):
    p = await _api(http, token, "GET", "/me/player")
    item = (p or {}).get("item")
    if not item:
        return "지금 스포티파이에서 재생 중인 게 없어요."
    who = ", ".join(x.get("name", "") for x in item.get("artists") or [])
    dev = ((p.get("device") or {}).get("name")) or ""
    state = "재생 중" if p.get("is_playing") else "일시정지"
    return f"{who}의 '{item.get('name', '')}', {dev}에서 {state}이에요." if dev else f"{who}의 '{item.get('name', '')}', {state}이에요."


async def _devices(http, token, a):
    devs = (await _api(http, token, "GET", "/me/player/devices") or {}).get("devices") or []
    if not devs:
        return "켜진 스포티파이 기기가 없어요."
    return "스포티파이 기기: " + ", ".join(f"{d.get('name', '')}{'(재생 중)' if d.get('is_active') else ''}" for d in devs[:8]) + "."


async def call(home, name, args, client=None):
    """One tool; always a short Korean string, never an exception, never a token in a log."""
    try:
        a = _check(name, args)
    except KeyError:
        return f"모르는 도구예요: {str(name)[:40]}"
    except ValueError as e:
        return f"도구 인자가 틀렸어요: {e}"
    http = client or httpx.AsyncClient(timeout=15)
    try:
        token = await oauth.access_token(home, NAME, http)
        fn = {"spotify_play": _play, "spotify_control": _control, "spotify_now_playing": _now,
              "spotify_devices": _devices}[name]
        return (await fn(http, token, a))[:300]
    except Problem as e:
        return str(e)
    except oauth.Expired:
        return "스포티파이 연결이 끊겼어요. 앱의 연결 화면에서 스포티파이를 다시 연결해 주세요."
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return f"지금은 스포티파이를 쓸 수 없어요. ({type(e).__name__})"
    finally:
        if client is None:
            await http.aclose()
