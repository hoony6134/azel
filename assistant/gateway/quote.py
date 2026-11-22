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


async def yahoo_find(q, c):
    try:
        r = await c.get(YAHOO_SEARCH, params={"q": q, "quotesCount": 6, "newsCount": 0})
        quotes = r.json().get("quotes") or []
    except (httpx.HTTPError, ValueError):
        return None
    stocks = [x for x in quotes if x.get("quoteType") in KINDS]
    exact = [x for x in stocks if q.lower() in (str(x.get("shortname", "")).lower(), str(x.get("symbol", "")).lower())]
    best = (exact or stocks or [None])[0]  # "Kakao" is Kakao, not KakaoPay listed first
    if not best:
        return None
    sym = best["symbol"]
    if m := re.fullmatch(r"(\d{6})\.K[SQ]", sym):
        return {"name": best.get("shortname") or q, "code": m[1], "symbol": sym}
    return {"name": best.get("longname") or best.get("shortname") or sym, "symbol": sym}


async def naver_price(code, c):
    r = await c.get(NAVER_QUOTE.format(code=code))
    r.raise_for_status()
    d = r.json()["datas"][0]
    num = lambda s: float(str(s).replace(",", ""))  # noqa: E731
    at = datetime.fromisoformat(d["localTradedAt"]).astimezone(KST) if d.get("localTradedAt") else None
    return {"name": d.get("stockName"), "price": num(d["closePrice"]), "change": num(d["compareToPreviousClosePrice"]),
            "pct": num(d["fluctuationsRatio"]), "currency": "KRW", "open": d.get("marketStatus") == "OPEN", "at": at}


async def yahoo_price(symbol, c):
    r = await c.get(YAHOO_CHART.format(symbol=symbol), params={"range": "1d", "interval": "1d"})
    r.raise_for_status()
    m = r.json()["chart"]["result"][0]["meta"]
    price, prev = m["regularMarketPrice"], m.get("chartPreviousClose") or m.get("previousClose")
    at = datetime.fromtimestamp(m["regularMarketTime"], KST) if m.get("regularMarketTime") else None
    return {"name": m.get("longName") or m.get("shortName") or symbol, "price": price, "currency": m.get("currency", "USD"),
            "change": price - prev if prev else None, "pct": (price - prev) / prev * 100 if prev else None,
            "open": None, "at": at}


def figure(x, currency):
    return f"{x:,.0f}" if currency in ("KRW", "JPY") else f"{x:,.2f}".rstrip("0").rstrip(".")


def spoken(q):
    """'삼성전자 주가는 180,900원이에요. 11월 20일 기준이고, 전날보다 8.13% 내렸어요.' (digits: the voice reads them)."""
    unit = CURRENCY.get(q["currency"], q["currency"])
    last = unit[-1]
    cop = "이에요" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "예요"  # 원이에요, 달러예요
    out = f"{q['name']} 주가는 {figure(q['price'], q['currency'])}{unit}{cop}."
    when = f"{q['at'].month}월 {q['at'].day}일 기준" if q.get("at") else ""
    move = ""
    if q.get("pct") is not None and abs(q["pct"]) >= 0.01:
        move = f"전날보다 {abs(q['pct']):.2f}% {'올랐어요' if q['pct'] > 0 else '내렸어요'}."
    if when and move:
        return f"{out} {when}이고, {move}"
    return f"{out} {when + '이에요.' if when else move}".strip()


async def fx(query, c):
    """'1달러는 1,337.8원이에요. …' for an exchange-rate question, or None when it is not one."""
    if not FX_Q.search(query):
        return None
    word = next((w for w in ("엔", "유로", "위안", "파운드") if w in query), "달러")
    symbol, per = FX[word]
    p = await yahoo_price(symbol, c)
    rate = p["price"] * per
    top = "은" if (ord(word[-1]) - 0xAC00) % 28 else "는"  # 100엔은, 1달러는
    out = f"{per}{word}{top} {rate:,.2f}".rstrip("0").rstrip(".") + "원이에요."
    when = f"{p['at'].month}월 {p['at'].day}일 기준" if p.get("at") else ""
    move = f"전날보다 {abs(p['pct']):.2f}% {'올랐어요' if p['pct'] > 0 else '내렸어요'}." if p.get("pct") else ""
    return {"spoken": " ".join(x for x in (out, f"{when}이고, {move}" if when and move else when + "이에요." if when
                                           else move) if x), "data": {**p, "rate": rate, "per": per, "at": None}}


async def quote(query, client=None):
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


if __name__ == "__main__":  # a live look: python quote.py 삼성전자
    import sys
    print(asyncio.run(quote(" ".join(sys.argv[1:]) or "Samsung Electronics"))["spoken"])
