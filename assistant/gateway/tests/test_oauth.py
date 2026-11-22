"""oauth.py (PKCE, discovery, DCR, locked refresh) and spotify.py against fake providers; the gateway routes."""
import asyncio
import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connectors  # noqa: E402
import oauth  # noqa: E402
import spotify  # noqa: E402

GW_CB = "http://100.64.0.8:8892/v1/oauth/callback"
SECRETS = ("SECRET-AT", "SECRET-RT", "SECRET-AT2", "SECRET-RT2")


class FakeSpotify:
    """accounts.spotify.com token endpoint plus the few Web API routes the tools use."""

    def __init__(self, devices=None, premium=True, artist="IU"):
        self.calls, self.token_posts, self.challenge = [], 0, None
        self.devices = [{"id": "mac1", "name": "JH MacBook", "is_active": False}] if devices is None else devices
        self.premium, self.artist, self.grant_ok = premium, artist, True

    def __call__(self, req):
        u = urlsplit(str(req.url))
        self.calls.append((req.method, u.path, parse_qs(u.query), req.content.decode() if req.content else ""))
        if u.path == "/api/token":
            self.token_posts += 1
            f = parse_qs(req.content.decode())
            if f["grant_type"] == ["authorization_code"]:
                ok = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
                if f["code"] != ["the-code"] or ok != self.challenge:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "SECRET-AT", "refresh_token": "SECRET-RT",
                                                 "expires_in": 3600, "scope": "user-read-playback-state"})
            if not self.grant_ok or f["refresh_token"] != ["SECRET-RT"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "SECRET-AT2", "refresh_token": "SECRET-RT2", "expires_in": 3600})
        assert req.headers["authorization"].startswith("Bearer SECRET-AT")
        if u.path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if u.path == "/v1/search":
            q = parse_qs(u.query)["q"][0]
            return httpx.Response(200, json={
                "artists": {"items": [{"name": self.artist, "uri": "spotify:artist:3HqSLMAZ3g3d5poNaI7GOU"}]},
                "tracks": {"items": [{"name": f"{q} 노래", "uri": "spotify:track:4iV5W9uYEdYUVa79Axb7Rh",
                                      "artists": [{"name": self.artist}]}]},
                "playlists": {"items": [None, {"name": "잔잔한 밤", "uri": "spotify:playlist:37i9dQZF1DX4sWSpwq3LiO"}]}})
        if u.path.startswith("/v1/me/player") and req.method in ("PUT", "POST"):
            if not self.premium:
                return httpx.Response(403, json={"error": {"status": 403, "message": "Player command failed: Premium required",
                                                           "reason": "PREMIUM_REQUIRED"}})
            if not self.devices:
                return httpx.Response(404, json={"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}})
            return httpx.Response(204)
        if u.path == "/v1/me/player":
            return httpx.Response(200, json={"is_playing": True, "device": {"name": "JH MacBook"},
                                             "item": {"name": "밤편지", "artists": [{"name": "IU"}]}})
        return httpx.Response(404)


def client(fake):
    return httpx.AsyncClient(transport=httpx.MockTransport(fake))


def run(coro):
    return asyncio.run(coro)


def connect(tmp, fake):
    """Consent end to end: authorize, the provider's code, finish. Returns the authorize result."""
    async def go():
        async with client(fake) as c:
            a = await oauth.authorize(tmp, "spotify", c, GW_CB, client_id="my-client-id")
            q = parse_qs(urlsplit(a["url"]).query)
            fake.challenge = q["code_challenge"][0]
            assert await oauth.finish(tmp, "spotify", "the-code", a["state"], c) == "connected"
            return a
    return run(go())


def test_authorize_url_is_pkce_s256_with_state_and_scopes(tmp_path):
    async def go():
        async with client(FakeSpotify()) as c:
            with pytest.raises(ValueError, match="client_id"):
                await oauth.authorize(tmp_path, "spotify", c, GW_CB)  # Spotify has no DCR: a client id is needed
            with pytest.raises(ValueError, match="redirect_uri"):
                await oauth.authorize(tmp_path, "spotify", c, GW_CB, client_id="cid", redirect_uri="https://evil.example/cb")
            return await oauth.authorize(tmp_path, "spotify", c, GW_CB, client_id="cid",
                                         redirect_uri="http://127.0.0.1:43123/cb")
    a = run(go())
    u = urlsplit(a["url"])
    q = parse_qs(u.query)
    assert u.netloc == "accounts.spotify.com" and q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43
    assert q["state"] == [a["state"]] and q["client_id"] == ["cid"] and q["redirect_uri"] == ["http://127.0.0.1:43123/cb"]
    assert "user-modify-playback-state" in q["scope"][0] and a["expires_in"] == 600
    assert oauth.status(tmp_path, "spotify") == "needs_consent"


def test_finish_stores_tokens_private_and_state_is_single_use(tmp_path):
    fake = FakeSpotify()
    a = connect(tmp_path, fake)
    p = tmp_path / "oauth" / "spotify.json"
    assert p.stat().st_mode & 0o777 == 0o600 and p.parent.stat().st_mode & 0o777 == 0o700
    d = json.loads(p.read_text())
    assert d["client_id"] == "my-client-id" and d["refresh_token"] == "SECRET-RT" and d["redirect_uri"] == oauth.APP_REDIRECT
    assert oauth.status(tmp_path, "spotify") == "connected"
    assert oauth.bearer(tmp_path, "spotify") == {"Authorization": "Bearer SECRET-AT"}

    async def again():
        async with client(fake) as c:
            await oauth.finish(tmp_path, "spotify", "the-code", a["state"], c)
    with pytest.raises(ValueError, match="state"):
        run(again())  # replay


def test_wrong_verifier_or_other_name_is_refused(tmp_path):
    fake = FakeSpotify()

    async def go():
        async with client(fake) as c:
            a = await oauth.authorize(tmp_path, "spotify", c, GW_CB, client_id="cid")
            fake.challenge = "not-the-challenge"
            with pytest.raises(ValueError, match="refused"):
                await oauth.finish(tmp_path, "spotify", "the-code", a["state"], c)
            b = await oauth.authorize(tmp_path, "spotify", c, GW_CB, client_id="cid")
            with pytest.raises(ValueError, match="state"):
                await oauth.finish(tmp_path, "other", "the-code", b["state"], c)
    run(go())
    assert oauth.status(tmp_path, "spotify") == "needs_consent"


def test_refresh_runs_once_under_the_lock_and_keeps_the_rotated_token(tmp_path):
    fake = FakeSpotify()
    connect(tmp_path, fake)
    d = oauth.load(tmp_path, "spotify")
    oauth.save(tmp_path, "spotify", {**d, "expires_at": time.time() - 5})
    before = fake.token_posts

    async def go():
        async with client(fake) as c:
            return await asyncio.gather(*(oauth.access_token(tmp_path, "spotify", c) for _ in range(5)))
    assert run(go()) == ["SECRET-AT2"] * 5 and fake.token_posts == before + 1
    assert oauth.load(tmp_path, "spotify")["refresh_token"] == "SECRET-RT2"


def test_refused_refresh_marks_expired(tmp_path):
    fake = FakeSpotify()
    connect(tmp_path, fake)
    oauth.save(tmp_path, "spotify", {**oauth.load(tmp_path, "spotify"), "expires_at": 0})
    fake.grant_ok = False

    async def go():
        async with client(fake) as c:
            await oauth.refresh_due(tmp_path, c)
    run(go())
    assert oauth.status(tmp_path, "spotify") == "expired" and oauth.bearer(tmp_path, "spotify") is None
    assert spotify.bridge(tmp_path) == ([], None)


def test_refresh_due_refreshes_ahead_of_expiry(tmp_path):
    fake = FakeSpotify()
    connect(tmp_path, fake)
    oauth.save(tmp_path, "spotify", {**oauth.load(tmp_path, "spotify"), "expires_at": time.time() + 120})

    async def go():
        async with client(fake) as c:
            await oauth.refresh_due(tmp_path, c)
    run(go())
    assert oauth.load(tmp_path, "spotify")["access_token"] == "SECRET-AT2"


class FakeMCP:
    """An MCP server behind OAuth: 401 -> protected-resource metadata -> AS metadata with DCR."""

    def __init__(self, issuer="https://auth.example.com"):
        self.issuer, self.registered = issuer, []

    def __call__(self, req):
        u = urlsplit(str(req.url))
        if u.netloc == "mcp.example.com" and u.path == "/mcp":
            return httpx.Response(401, headers={"WWW-Authenticate":
                                                'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'})
        if u.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "https://mcp.example.com/mcp", "authorization_servers": [self.issuer],
                                             "scopes_supported": ["read"]})
        if u.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                                             "token_endpoint": f"{self.issuer}/token", "registration_endpoint": f"{self.issuer}/register",
                                             "code_challenge_methods_supported": ["S256"]})
        if u.path == "/register":
            body = json.loads(req.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "dcr-client-1"})
        return httpx.Response(404)


def test_mcp_discovery_and_dynamic_registration(tmp_path):
    fake = FakeMCP()

    async def go():
        async with client(fake) as c:
            return await oauth.authorize(tmp_path, "notes", c, GW_CB, server_url="https://mcp.example.com/mcp",
                                         redirect_uri=GW_CB)
    a = run(go())
    q = parse_qs(urlsplit(a["url"]).query)
    assert a["url"].startswith("https://auth.example.com/authorize?") and q["client_id"] == ["dcr-client-1"]
    assert q["resource"] == ["https://mcp.example.com/mcp"] and q["scope"] == ["read"]
    assert fake.registered[0]["token_endpoint_auth_method"] == "none" and fake.registered[0]["redirect_uris"] == [GW_CB]


def test_discovery_refuses_private_and_loopback_endpoints(tmp_path):
    for issuer in ("https://10.0.0.5", "https://127.0.0.1", "http://auth.example.com", "https://localhost"):
        async def go():
            async with client(FakeMCP(issuer)) as c:
                await oauth.authorize(tmp_path, "notes", c, GW_CB, server_url="https://mcp.example.com/mcp")
        with pytest.raises(ValueError, match="not allowed"):
            run(go())
    assert oauth.safe_endpoint("http://100.64.0.5:9000/token") and not oauth.safe_endpoint("https://169.254.169.254/x")


def test_an_oauth_custom_connector_gets_its_live_token_or_is_skipped(tmp_path):
    class Disc:
        connectors, plugins = [], []
    conn = connectors.check({"custom": [{"name": "notes", "url": "https://mcp.example.com/mcp", "oauth": True}]})
    servers, *_ = connectors.run_args(conn, Disc, [], "t", tmp_path)
    assert servers is None  # no grant yet: skipped, never called without auth
    oauth.save(tmp_path, "notes", {"access_token": "SECRET-AT", "expires_at": time.time() + 600, "client_id": "c",
                                   "token_endpoint": "https://auth.example.com/token"})
    servers, *_ = connectors.run_args(conn, Disc, [], "t", tmp_path)
    assert servers["notes"]["headers"] == {"Authorization": "Bearer SECRET-AT"}
    with pytest.raises(ValueError):
        connectors.check({"custom": [{"name": "notes", "url": "https://mcp.example.com/mcp", "oauth": "yes"}]})


# ---------- Spotify tools ----------
def tool(tmp, fake, name, args):
    async def go():
        async with client(fake) as c:
            return await spotify.call(tmp, name, args, client=c)
    out = run(go())
    assert not any(s in out for s in SECRETS)
    return out


def test_spotify_tools_listed_only_when_connected(tmp_path):
    assert spotify.bridge(tmp_path) == ([], None)
    connect(tmp_path, FakeSpotify())
    defs, fn = spotify.bridge(tmp_path)
    assert [d["name"] for d in defs] == ["spotify_play", "spotify_control", "spotify_now_playing", "spotify_devices"] and fn


def test_play_a_song_on_the_first_device_when_none_is_active(tmp_path):
    fake = FakeSpotify(artist="아이유")
    connect(tmp_path, fake)
    out = tool(tmp_path, fake, "spotify_play", {"query": "밤편지"})
    assert out == "JH MacBook에서 아이유의 '밤편지 노래'을 틀었어요."
    put = [c for c in fake.calls if c[1] == "/v1/me/player/play"][-1]
    assert put[2]["device_id"] == ["mac1"] and json.loads(put[3]) == {"uris": ["spotify:track:4iV5W9uYEdYUVa79Axb7Rh"]}


def test_play_an_artist_and_a_playlist_as_contexts(tmp_path):
    fake = FakeSpotify(devices=[{"id": "ph", "name": "iPhone", "is_active": True}], artist="IU")
    connect(tmp_path, fake)
    assert tool(tmp_path, fake, "spotify_play", {"query": "iu"}) == "IU의 노래을 틀었어요."
    put = [c for c in fake.calls if c[1] == "/v1/me/player/play"][-1]
    assert json.loads(put[3]) == {"context_uri": "spotify:artist:3HqSLMAZ3g3d5poNaI7GOU"} and "device_id" not in put[2]
    assert tool(tmp_path, fake, "spotify_play", {"query": "잔잔한 플레이리스트"}) == "'잔잔한 밤' 플레이리스트을 틀었어요."
    out = tool(tmp_path, fake, "spotify_play", {"uri": "spotify:album:1ATL5GLyefJaxhQzSPVrLX", "device": "iphone"})
    put = [c for c in fake.calls if c[1] == "/v1/me/player/play"][-1]
    assert out == "iPhone에서 요청한 곡을 틀었어요." and put[2]["device_id"] == ["ph"]


def test_no_device_no_premium_and_bad_args_are_spoken(tmp_path):
    fake = FakeSpotify(devices=[])
    connect(tmp_path, fake)
    assert "스포티파이를 한 번 열어" in tool(tmp_path, fake, "spotify_play", {"query": "밤편지"})
    assert "기기를 찾지 못했어요" in tool(tmp_path, FakeSpotify(), "spotify_play", {"query": "x", "device": "TV"})
    assert "프리미엄" in tool(tmp_path, FakeSpotify(premium=False), "spotify_control", {"action": "pause"})
    assert "인자가 틀렸어요" in tool(tmp_path, fake, "spotify_control", {"action": "volume", "volume": 300})
    assert "인자가 틀렸어요" in tool(tmp_path, fake, "spotify_play", {"uri": "https://open.spotify.com/x"})
    assert "인자가 틀렸어요" in tool(tmp_path, fake, "spotify_play", {})


def test_control_now_playing_and_devices(tmp_path):
    fake = FakeSpotify(devices=[{"id": "m", "name": "JH MacBook", "is_active": True}])
    connect(tmp_path, fake)
    assert tool(tmp_path, fake, "spotify_control", {"action": "volume", "volume": 30}) == "볼륨을 30으로 맞췄어요."
    assert [c for c in fake.calls if c[1] == "/v1/me/player/volume"][-1][2] == {"volume_percent": ["30"]}
    assert tool(tmp_path, fake, "spotify_control", {"action": "next"}) == "다음 곡으로 넘겼어요."
    assert tool(tmp_path, fake, "spotify_control", {"action": "shuffle", "on": False}) == "셔플을 껐어요."
    assert tool(tmp_path, fake, "spotify_now_playing", {}) == "IU의 '밤편지', JH MacBook에서 재생 중이에요."
    assert tool(tmp_path, fake, "spotify_devices", {}) == "스포티파이 기기: JH MacBook(재생 중)."


def test_expired_grant_is_spoken(tmp_path):
    fake = FakeSpotify()
    connect(tmp_path, fake)
    oauth.save(tmp_path, "spotify", {**oauth.load(tmp_path, "spotify"), "expired": True})
    assert "다시 연결" in tool(tmp_path, fake, "spotify_devices", {})


def test_public_https_callback_is_allowed_only_when_configured(tmp_path):
    pub = "https://azel.scian.xyz/v1/oauth/callback"
    assert oauth.check_redirect(pub, (GW_CB, pub)) == pub
    with pytest.raises(ValueError):
        oauth.check_redirect(pub, (GW_CB,))
    with pytest.raises(ValueError):
        oauth.check_redirect("http://127.0.0.1:8080/cb?next=evil", GW_CB)


def test_bridge_runs_spotify_and_mcp_serves_it_to_claude_code_only(tmp_path):
    import tools_bridge as TB
    ran = []

    async def run_tool(name, args):
        ran.append((name, args))
        return "멈췄어요."
    b = TB.Bridge(spotify=lambda: (spotify.DEFS, run_tool), mode=lambda: asyncio.sleep(0, "auto"))
    r = asyncio.run(b.run("spotify_control", {"action": "pause"}))
    assert r["ok"] and r["spoken"] == "멈췄어요." and ran == [("spotify_control", {"action": "pause"})]

    def names(scope):
        out = asyncio.run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, scope=scope))
        return {t["name"] for t in out["result"]["tools"]}
    assert "spotify_play" in names("full") and "spotify_play" not in names("external")
    out = asyncio.run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "spotify_play", "arguments": {"query": "x"}}}, scope="external"))
    assert out["error"]["message"] == "unknown tool"
    init = asyncio.run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}))
    assert init["result"]["serverInfo"]["name"] == "azel"


def test_custom_connector_with_oauth_api_key_and_allowlist_together(tmp_path):
    class Disc:
        connectors, plugins = [], []
    conn = connectors.check({"custom": [{"name": "notes", "url": "https://mcp.example.com/mcp", "oauth": True,
                                         "auth_ref": "notes", "allowed_tools": ["search", "read_*"]}]})
    servers, allowed, *_ = connectors.run_args(conn, Disc, [], "t", tmp_path)
    assert servers is None  # OAuth not connected yet: skipped even though it has an allowlist
    connectors.put_secret(tmp_path, "notes", '{"X-Api-Key": "SECRET-KEY"}')
    oauth.save(tmp_path, "notes", {"access_token": "SECRET-AT", "expires_at": time.time() + 600, "client_id": "c",
                                   "token_endpoint": "https://auth.example.com/token"})
    servers, allowed, *_ = connectors.run_args(conn, Disc, [], "t", tmp_path)
    assert servers["notes"]["headers"] == {"X-Api-Key": "SECRET-KEY", "Authorization": "Bearer SECRET-AT"}
    assert "mcp__notes__search" in allowed and "mcp__notes__read_*" in allowed and "mcp__notes" not in allowed
    (tmp_path / "mcp" / "notes").chmod(0o644)  # a secret file others can read: never sent at all
    assert connectors.run_args(conn, Disc, [], "t", tmp_path)[0] is None
