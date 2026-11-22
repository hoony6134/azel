"""Stock quotes: a Korean name via Naver, an English name or ticker via Yahoo, the exact name first, unlisted."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quote  # noqa: E402

AC = {"삼성전자": [{"code": "005930", "name": "삼성전자", "nationCode": "KOR"}]}
SEARCH = {"Kakao": [{"symbol": "377300.KS", "shortname": "kakaopay", "quoteType": "EQUITY"},
                    {"symbol": "035720.KS", "shortname": "Kakao", "quoteType": "EQUITY"}],
          "Anthropic": [{"symbol": "ANT", "shortname": "Anthropic, PBC", "longname": "Anthropic, PBC", "quoteType": "EQUITY"}]}
NAVER = {"005930": {"stockName": "삼성전자", "closePrice": "180,900", "compareToPreviousClosePrice": "-16,000",
                    "fluctuationsRatio": "-8.13", "marketStatus": "CLOSE", "localTradedAt": "2026-11-20T20:00:00+09:00"},
         "035720": {"stockName": "카카오", "closePrice": "30,650", "compareToPreviousClosePrice": "-1,250",
                    "fluctuationsRatio": "-3.92", "marketStatus": "CLOSE", "localTradedAt": "2026-11-20T20:00:00+09:00"}}
CHART = {"JPYKRW=X": {"symbol": "JPYKRW=X", "currency": "KRW", "regularMarketPrice": 8.468, "chartPreviousClose": 8.329,
                      "regularMarketTime": 1795208402},
         "KRW=X": {"symbol": "KRW=X", "currency": "KRW", "regularMarketPrice": 1337.8, "chartPreviousClose": 1333.62,
                   "regularMarketTime": 1795318231},
         "ANT": {"symbol": "ANT", "currency": "USD", "regularMarketPrice": 88.91, "chartPreviousClose": 97.17,
                 "regularMarketTime": 1795208402, "longName": "Anthropic, PBC"}}


def client(seen):
    def handle(req):
        seen.append(req.url.host)
        if req.url.host == "ac.stock.naver.com":
            return httpx.Response(200, json={"items": AC.get(req.url.params["q"], [])})
        if req.url.host == "polling.finance.naver.com":
            return httpx.Response(200, json={"datas": [NAVER[req.url.path.rsplit("/", 1)[1]]]})
        if "search" in req.url.path:
            return httpx.Response(200, json={"quotes": SEARCH.get(req.url.params["q"], [])})
        return httpx.Response(200, json={"chart": {"result": [{"meta": CHART[req.url.path.rsplit("/", 1)[1]]}]}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("asked,said,hosts", [
    ("삼성전자 주가 지금 알려줘", "삼성전자 주가는 180,900원이에요. 11월 20일 기준이고, 전날보다 8.13% 내렸어요.",
     ["ac.stock.naver.com", "polling.finance.naver.com"]),
    ("Kakao", "카카오 주가는 30,650원이에요. 11월 20일 기준이고, 전날보다 3.92% 내렸어요.",
     ["query2.finance.yahoo.com", "polling.finance.naver.com"]),
    ("Anthropic", "Anthropic, PBC 주가는 88.91달러예요. 11월 21일 기준이고, 전날보다 8.50% 내렸어요.",
     ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]),
])
def test_quotes(asked, said, hosts):
    seen = []
    assert asyncio.run(quote.quote(asked, client(seen)))["spoken"] == said and seen == hosts


def test_exchange_rates():
    assert asyncio.run(quote.quote("지금 1달러 환율 얼마야?", client([])))["spoken"] == \
        "1달러는 1,337.8원이에요. 11월 22일 기준이고, 전날보다 0.31% 올랐어요."
    assert asyncio.run(quote.quote("엔화 환율", client([])))["spoken"].startswith("100엔은 846.8원이에요.")


def test_an_unlisted_company_says_so():
    with pytest.raises(LookupError, match="비상장.*영어 이름"):
        asyncio.run(quote.quote("엔트로픽", client([])))  # Naver and Yahoo both know nothing by that name


def test_clean_keeps_the_company():
    assert quote.clean("엔트로픽 주가 알려줘") == "엔트로픽" and quote.clean("삼성전자 주가 지금 얼마야?") == "삼성전자"
    assert quote.clean("엔비디아 주식 얼마야?") == "엔비디아"
