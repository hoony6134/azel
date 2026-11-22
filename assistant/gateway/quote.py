"""Stock quotes from free JSON endpoints, no key: snippets rarely carry today's price, and Qwen misread them.

find(): a Korean name through Naver's autocomplete ("삼성전자" -> 005930, KOR), else Yahoo's search (English names,
tickers and codes: "Anthropic" -> ANT, "005930" -> 005930.KS). price(): Naver's polling quote for a domestic code,
Yahoo's chart for everything else. Nothing found: LookupError, said as "상장된 종목을 못 찾았어요 (비상장일 수 있어요)".
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx

NAVER_AC = "https://ac.stock.naver.com/ac"
NAVER_QUOTE = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TIMEOUT_S = 6
UA = {"User-Agent": "Mozilla/5.0 (compatible; azel-quote)"}
KST = timezone(timedelta(hours=9))
CURRENCY = {"KRW": "원", "USD": "달러", "JPY": "엔", "EUR": "유로", "CNY": "위안", "HKD": "홍콩달러", "GBP": "파운드"}
KINDS = ("EQUITY", "ETF")
NOT_FOUND = "상장된 종목을 찾지 못했어요. 비상장 회사이거나 이름이 다를 수 있어요."
RETRY = " 한글 이름으로 못 찾았으면 영어 이름이나 티커로 한 번 더 찾아봐(예: Anthropic, NVDA)."
# exchange rates against the won: (Yahoo symbol, how many units are quoted). 엔 goes per 100, as Korean banks say it.
FX = {"달러": ("KRW=X", 1), "엔": ("JPYKRW=X", 100), "유로": ("EURKRW=X", 1), "위안": ("CNYKRW=X", 1),
      "파운드": ("GBPKRW=X", 1)}
FX_Q = re.compile(r"환율|원\s*달러|달러\s*(환율|얼마)|엔화|(\d+\s*)?(달러|엔|유로|위안|파운드)\s*(는|은|에|당)?\s*(얼마|몇\s*원)")


def clean(query):
    """The company out of a spoken question ("엔트로픽 주가 지금 알려줘" -> "엔트로픽")."""
    q = re.sub(r"(의\s*)?(주가|주식|시세|종가|얼마|지금|현재|오늘|알려\s*\S*|좀|요|\?|야|이야)", " ", str(query))
    return re.sub(r"\s+", " ", q).strip()[:60]


async def naver_find(q, c):
    try:
        r = await c.get(NAVER_AC, params={"q": q, "target": "stock"})
        items = r.json().get("items") or []
    except (httpx.HTTPError, ValueError):
        return None
    for it in items:
        code = str(it.get("code") or "")
        if it.get("nationCode") == "KOR" and re.fullmatch(r"\d{6}", code):
            return {"name": it.get("name") or q, "code": code}
        if it.get("reutersCode") or it.get("code"):  # a foreign stock: Yahoo knows it by its ticker
            return {"name": it.get("name") or q, "ticker": str(it.get("code"))}
    return None


async def yahoo_find(q, c, kinds=KINDS):
    try:
        r = await c.get(YAHOO_SEARCH, params={"q": q, "quotesCount": 6, "newsCount": 0})
        quotes = r.json().get("quotes") or []
    except (httpx.HTTPError, ValueError):
        return None
    stocks = [x for x in quotes if x.get("quoteType") in kinds]
    exact = [x for x in stocks if q.lower() in (str(x.get("shortname", "")).lower(), str(x.get("symbol", "")).lower())]
    best = (exact or stocks or [None])[0]  # "Kakao" is Kakao, not KakaoPay listed first
    if not best:
        return None
    sym = best["symbol"]
    if m := re.fullmatch(r"(\d{6})\.K[SQ]", sym):
        return {"name": best.get("shortname") or q, "code": m[1], "symbol": sym}
    return {"name": best.get("longname") or best.get("shortname") or sym, "symbol": sym}


EXCHANGE_TICKER = re.compile(r"\b(NASDAQ|NYSE|NYSEARCA|AMEX|KOSPI|KOSDAQ|KRX)\s*[:：]\s*([A-Z][A-Z.]{0,5}|\d{6})\b")
COMMON = {"home", "the", "news", "google", "wise", "investing", "yahoo", "finance", "naver", "wikipedia", "youtube", "namu",
          "tistory", "blog", "daum", "kr", "m", "en", "ko", "tradingview", "reuters", "bloomberg", "cnbc", "brunch",
          # sites that answer every query (SearXNG returns their pages for anything): never a company
          "zhihu", "reddit", "quora", "medium", "twitter", "facebook", "instagram", "linkedin", "baidu", "bing", "msn",
          "stock", "stocks", "price", "share", "shares", "inc", "corp", "com", "www", "and", "for", "today", "live", "chart"}


INITIAL = "g kk n d tt r m b pp s ss _ j jj ch k t p h".split()
MEDIAL = "a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i".split()
FINAL = ["", *"k k k n n n t l k m l l l p l m p p t t ng t t k t p t".split()]
SOUND_MIN = 0.6


def romanize(ko):
    out = []
    for ch in ko:
        o = ord(ch) - 0xAC00
        if 0 <= o < 11172:
            i, m, f = o // 588, o % 588 // 28, o % 28
            out.append(INITIAL[i].replace("_", "") + MEDIAL[m] + FINAL[f])
        elif ch.isascii() and ch.isalnum():
            out.append(ch.lower())
    return "".join(out)


def skeleton(latin):
    """Consonants only, with the letters Korean hears alike made one: anthropic -> ntrpk, 엔트로픽 -> ntrpk."""
    t = latin.lower()
    for a, b in (("ph", "p"), ("th", "t"), ("ch", "c"), ("ck", "k"), ("c", "k"), ("q", "k"), ("x", "ks"), ("f", "p"),
                 ("v", "b"), ("l", "r"), ("z", "j"), ("w", ""), ("y", ""), ("h", ""), ("ng", "n"), ("g", "k"), ("d", "t"),
                 ("b", "p"), ("j", "c"), ("s", "s")):
        t = t.replace(a, b)
    t = re.sub(r"[aeiou]", "", t)
    return re.sub(r"(.)\1+", r"\1", t)  # tt -> t


def sounds_like(korean, english):
    from difflib import SequenceMatcher
    a, b = skeleton(romanize(korean)), skeleton(english)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


async def search_find(q, c, search):
    """A Korean name neither source knows ("엔트로픽"): ask the web, then take an explicit "NASDAQ:ANT", or an English
    name from the top results that sounds like what was said (엔트로픽 ~ Anthropic), checked on Yahoo."""
    core = q.split()[-1] if q.split() else q  # "그러면 엔트로픽" -> 엔트로픽
    for words in (f"{core} stock ticker", f"{core} 주식"):
        try:
            r = await search(words)
        except Exception:  # noqa: BLE001  no search: the name just isn't found
            return None
        if not r:
            continue
        text = r.get("text", "") + " " + " ".join(x.get("title", "") for x in r.get("sources", []))
        seen = set()
        for x in r.get("sources", [])[:6]:
            host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", x.get("url", "")).split("/")[0]).split(".")[0]
            seen |= {host.lower(), *(w.lower() for w in re.findall(r"\b[A-Za-z][A-Za-z&]{2,}\b", x.get("title", "")))}
        names = [n for score, n in sorted(((sounds_like(core, n), n) for n in seen - COMMON), reverse=True)[:3]
                 if score >= SOUND_MIN]
        tickers = [m[2] for m in EXCHANGE_TICKER.finditer(text)][:3]  # "NASDAQ:ANT": checked like any other guess
        for cand in [*names, *tickers]:
            hit = await yahoo_find(cand, c, kinds=("EQUITY",))  # a company, not "an OpenAI ecosystem ETF"
            # the company must sound like what was said: 테슬라 is never Intel, 오픈에이아이 never an energy stock
            if hit and sounds_like(core, re.split(r"[\s,.]", hit["name"])[0]) >= SOUND_MIN:
                return hit
    return None


async def naver_price(code, c):
    r = await c.get(NAVER_QUOTE.format(code=code))
    r.raise_for_status()
    d = r.json()["datas"][0]
    num = lambda s: float(str(s).replace(",", ""))  # noqa: E731
    at = datetime.fromisoformat(d["localTradedAt"]).astimezone(KST) if d.get("localTradedAt") else None
    return {"name": d.get("stockName"), "price": num(d["closePrice"]), "change": num(d["compareToPreviousClosePrice"]),
            "pct": num(d["fluctuationsRatio"]), "currency": "KRW", "open": d.get("marketStatus") == "OPEN", "at": at,
            "source": "네이버 증권"}


async def yahoo_price(symbol, c):
    r = await c.get(YAHOO_CHART.format(symbol=symbol), params={"range": "1d", "interval": "1d"})
    r.raise_for_status()
    m = r.json()["chart"]["result"][0]["meta"]
    price, prev = m["regularMarketPrice"], m.get("chartPreviousClose") or m.get("previousClose")
    at = datetime.fromtimestamp(m["regularMarketTime"], KST) if m.get("regularMarketTime") else None
    return {"name": m.get("longName") or m.get("shortName") or symbol, "price": price, "currency": m.get("currency", "USD"),
            "change": price - prev if prev else None, "pct": (price - prev) / prev * 100 if prev else None,
            "open": None, "at": at, "source": "야후 파이낸스"}


def figure(x, currency):
    return f"{x:,.0f}" if currency in ("KRW", "JPY") else f"{x:,.2f}".rstrip("0").rstrip(".")


def spoken(q):
    """'네이버 증권 기준 11월 20일 삼성전자 주가는 180,900원이에요. 전날보다 8.13% 내렸어요.': the source and the date come
    first, where a model keeps them (it dropped a trailing "11월 21일 기준" and said "지금" of a Friday close)."""
    unit = CURRENCY.get(q["currency"], q["currency"])
    last = unit[-1]
    cop = "이에요" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "예요"  # 원이에요, 달러예요
    when = f" {q['at'].month}월 {q['at'].day}일" if q.get("at") else ""
    out = f"{q.get('source', '시세')} 기준{when} {q['name']} {q.get('what', '주가')}는 {figure(q['price'], q['currency'])}{unit}{cop}."
    if q.get("pct") is not None and abs(q["pct"]) >= 0.01:
        out += f" 전날보다 {abs(q['pct']):.2f}% {'올랐어요' if q['pct'] > 0 else '내렸어요'}."
    return out


async def fx(query, c):
    """'1달러는 1,337.8원이에요. …' for an exchange-rate question, or None when it is not one."""
    if not FX_Q.search(query):
        return None
    word = next((w for w in ("엔", "유로", "위안", "파운드") if w in query), "달러")
    symbol, per = FX[word]
    p = await yahoo_price(symbol, c)
    rate = p["price"] * per
    top = "은" if (ord(word[-1]) - 0xAC00) % 28 else "는"  # 100엔은, 1달러는
    when = f" {p['at'].month}월 {p['at'].day}일" if p.get("at") else ""
    out = f"야후 파이낸스 기준{when} {per}{word}{top} {rate:,.2f}".rstrip("0").rstrip(".") + "원이에요."
    if p.get("pct"):
        out += f" 전날보다 {abs(p['pct']):.2f}% {'올랐어요' if p['pct'] > 0 else '내렸어요'}."
    return {"spoken": out, "data": {**p, "rate": rate, "per": per, "at": None}}


async def quote(query, client=None, search=None):
    """{"spoken", "data"} for a company name, ticker or code, or an exchange rate ("달러 환율"). LookupError when no
    listed stock matches."""
    q = clean(query) or str(query)
    c = client or httpx.AsyncClient(timeout=TIMEOUT_S, headers=UA, follow_redirects=True)
    try:
        if rate := await fx(str(query), c):
            return rate
        hit = None
        if re.search(r"[가-힣]", q):
            hit = await naver_find(q, c)
        if not hit:
            hit = await yahoo_find(q, c)
        if not hit and search and re.search(r"[가-힣]", q):
            hit = await search_find(q, c, search)
        if not hit:
            raise LookupError(NOT_FOUND + (RETRY if re.search(r"[가-힣]", q) else ""))
        if hit.get("code"):
            try:
                p = await naver_price(hit["code"], c)
            except (httpx.HTTPError, KeyError, ValueError, IndexError):
                p = await yahoo_price(hit.get("symbol") or f"{hit['code']}.KS", c)
        else:
            p = await yahoo_price(hit.get("symbol") or hit["ticker"], c)
        p["name"] = hit["name"] if re.search(r"[가-힣]", hit["name"] or "") else (p.get("name") or hit["name"])
    finally:
        if client is None:
            await c.aclose()
    return {"spoken": spoken(p), "data": {**p, "at": p["at"].isoformat() if p.get("at") else None}}


HOLDING = re.compile(r"^(?P<name>.*?)\s*\((?P<tick>\^?[A-Z][A-Z.]{0,6}|\d{6})\)\s*$")  # ^GSPC: an index


async def closes(symbol, c, rng="5d"):
    """Daily closes (oldest first) from Yahoo's chart, for a sparkline; [] when there are none."""
    r = await c.get(YAHOO_CHART.format(symbol=symbol), params={"range": rng, "interval": "1d"})
    r.raise_for_status()
    res = (r.json()["chart"]["result"] or [{}])[0]
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    return [round(x, 2) for x in q.get("close") or [] if x is not None]


async def holding(entry, client=None):
    """One of the TV's 내 종목: `삼성전자 (005930)` / `애플 (AAPL)` (the ticker wins, the name is what is shown) or a bare
    name, looked up like a voice question but without web search, so an unlisted company (토스) is simply
    {"listed": False} instead of a guess. Price and change from Naver (Korean codes) or Yahoo, plus `spark`: 5 closes."""
    m = HOLDING.match(entry.strip())
    name, tick = (m["name"] or m["tick"], m["tick"]) if m else (entry.strip(), None)
    c = client or httpx.AsyncClient(timeout=TIMEOUT_S, headers=UA, follow_redirects=True)
    try:
        if tick is None:
            hit = (await naver_find(name, c) if re.search(r"[가-힣]", name) else None) or await yahoo_find(name, c)
            if not hit:
                return {"name": name, "listed": False}
            tick = hit.get("code") or hit.get("symbol") or hit.get("ticker")
        try:
            if re.fullmatch(r"\d{6}", tick):
                try:
                    p = await naver_price(tick, c)
                except (httpx.HTTPError, KeyError, ValueError, IndexError):
                    p = await yahoo_price(f"{tick}.KS", c)
                symbols = (f"{tick}.KS", f"{tick}.KQ")
            else:
                p, symbols = await yahoo_price(tick, c), (tick,)
        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
            return {"name": name, "ticker": tick, "listed": True, "price": None}
        spark = []
        for sym in symbols:  # a KOSDAQ code is .KQ on Yahoo
            try:
                spark = await closes(sym, c)
            except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
                spark = []
            if spark:
                break
    finally:
        if client is None:
            await c.aclose()
    return {"name": name, "ticker": tick, "listed": True, "price": p["price"],
            "currency": "index" if tick.startswith("^") else p["currency"],  # points, not dollars
            "change": p.get("change"), "pct": round(p["pct"], 2) if p.get("pct") is not None else None, "spark": spark,
            "source": p.get("source"), "at": p["at"].isoformat() if p.get("at") else None}


if __name__ == "__main__":  # a live look: python quote.py 삼성전자
    import sys
    print(asyncio.run(quote(" ".join(sys.argv[1:]) or "Samsung Electronics"))["spoken"])
