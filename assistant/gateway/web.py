"""Web search and fetch for the paths that have no native web tools (Qwen, NIM, the voice API turn).

search(): one Claude API call, Haiku with the server-side web_search tool; trimmed text plus source titles and URLs.
fetch(): http(s) only, public addresses only (checked on every redirect hop), capped in bytes and characters.
"""
import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

import claude_api

SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3,
               "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"}}
SEARCH_USD = 0.01  # per search request, on top of tokens
SEARCH_SYSTEM = ("웹 검색 결과를 한국어로 짧게 정리해. 질문에 바로 답하는 사실과 숫자만, 날짜가 있으면 날짜와 함께. "
                 "항목마다 출처 사이트 이름을 괄호로. 모르면 모른다고 해.")
MAX_BYTES = 400_000
MAX_CHARS = 4000
REDIRECTS = 3


async def search(api_key, query, n=5, client=None, today=None):
    """{"text", "sources": [{"title", "url"}], "cost_usd", "usage"}."""
    from datetime import datetime, timedelta, timezone
    today = today or datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d (%a)")
    messages = [{"role": "user", "content": f"오늘은 {today}, 한국이야. 질문: {str(query)[:300]}"}]
    text, sources, cost, usage = [], [], 0.0, {}
    c = client or httpx.AsyncClient(timeout=60)
    try:
        for _ in range(3):  # a long server-side search pauses the turn: send it back to continue
            body = claude_api.request_body("haiku", SEARCH_SYSTEM, [SEARCH_TOOL], messages, max_tokens=700)
            r = await c.post(claude_api.API_URL, json=body,
                             headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            m = r.json()
            usage = m.get("usage") or {}
            cost += claude_api.cost_usd("haiku", usage) + \
                (usage.get("server_tool_use") or {}).get("web_search_requests", 0) * SEARCH_USD
            for b in m.get("content", []):
                if b.get("type") == "text":
                    text.append(b.get("text", ""))
                elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                    sources += [{"title": str(x.get("title", ""))[:80], "url": str(x.get("url", ""))}
                                for x in b["content"] if x.get("type") == "web_search_result"]
            if m.get("stop_reason") != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": m["content"]}]
    finally:
        if client is None:
            await c.aclose()
    seen, uniq = set(), []
    for s in sources:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)
    return {"text": "".join(text).strip()[:1500], "sources": uniq[:max(1, min(n, 8))], "cost_usd": cost,
            "usage": usage}


def _public(host):
    """Every address the name resolves to is a public one (no loopback, private, link-local, CGNAT/tailnet).
    shortcut: checked before connecting, so a DNS answer that changes in between (rebinding) is not caught; pin the
    resolved address in the connection if this ever fetches for anyone but the owner."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10"):
            return False
    return bool(infos)


async def check_url(url):
    p = urlsplit(str(url))
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("http(s) 주소만 열 수 있어요.")
    if not await asyncio.to_thread(_public, p.hostname):
        raise ValueError("내부 주소는 열 수 없어요.")


def to_text(raw, ctype):
    if "html" not in ctype:
        return raw
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    body = re.sub(r"\s+", " ", html.unescape(raw)).strip()
    return (html.unescape(title.group(1)).strip() + "\n" if title else "") + body


async def fetch(url, client=None):
    """{"url", "text"}: the page as text, at most MAX_CHARS. Raises ValueError for a refused address."""
    c = client or httpx.AsyncClient(timeout=15, follow_redirects=False,
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; azel-fetch)"})
    try:
        for _ in range(REDIRECTS + 1):
            await check_url(url)
            async with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(str(url), r.headers["location"])
                    continue
                r.raise_for_status()
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) >= MAX_BYTES:
                        break
                text = to_text(buf.decode(r.encoding or "utf-8", "replace"), r.headers.get("content-type", ""))
                return {"url": str(url), "text": text[:MAX_CHARS]}
        raise ValueError("리다이렉트가 너무 많아요.")
    finally:
        if client is None:
            await c.aclose()


# ---------- SearXNG: the user's own metasearch (free, so local Qwen can search) ----------
SEARXNG_URL = "https://search.infra.scian.xyz/search"
SEARXNG_TIMEOUT_S = 6
THIN = 240  # fewer snippet characters than this across the top results: read the top pages too


def site_of(url):
    return re.sub(r"^www\.", "", urlsplit(url).hostname or "")


async def searxng(query, n=5, url=SEARXNG_URL, client=None, fetch_pages=True):
    """{"text", "sources", "cost_usd": 0, "usage": {}, "engine": "searxng", "unresponsive": [...]}, or None when
    it returned nothing. Engines that fail inside SearXNG (rate limits, CAPTCHAs) only show in "unresponsive"."""
    c = client or httpx.AsyncClient(timeout=SEARXNG_TIMEOUT_S, follow_redirects=True)
    try:
        r = await c.get(url, params={"q": str(query)[:300], "format": "json", "language": "ko"})
        r.raise_for_status()
        d = r.json()
    finally:
        if client is None:
            await c.aclose()
    hits = [x for x in d.get("results") or [] if x.get("url", "").startswith(("http://", "https://"))][:max(1, min(n, 8))]
    answers = [str(a.get("answer", a) if isinstance(a, dict) else a)[:300] for a in d.get("answers") or []]
    boxes = [f"{b.get('infobox', '')}: {str(b.get('content', ''))[:300]}" for b in d.get("infoboxes") or []]
    if not hits and not answers and not boxes:
        return None
    lines = [*answers, *boxes] + [f"- {x.get('title', '')[:80]} ({site_of(x['url'])}): {str(x.get('content', ''))[:300]}"
                                  for x in hits]
    if fetch_pages and sum(len(str(x.get("content", ""))) for x in hits[:3]) < THIN:
        for x in hits[:2]:  # the snippets say little ("날씨 지도. 광주."): what the top pages themselves say
            try:
                page = await fetch(x["url"])
                lines.append(f"[{site_of(x['url'])} 본문] {page['text'][:1200]}")
            except Exception:  # noqa: BLE001  a page that won't load: the snippets still stand
                continue
    return {"text": "\n".join(lines)[:4000], "sources": [{"title": x.get("title", "")[:80], "url": x["url"]} for x in hits],
            "cost_usd": 0.0, "usage": {}, "engine": "searxng",
            "unresponsive": [e[0] if isinstance(e, list) else str(e) for e in d.get("unresponsive_engines") or []]}
