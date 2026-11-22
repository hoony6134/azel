"""A host or alert read carries the services that are down (live 2026-11-23: NemoClaw down, "서버 상태" answered 정상)."""
import asyncio

import cygnus

HEALTH = "vd-spark 정상 · CPU 12% · 메모리 41%\nvd-core 정상 · CPU 3%"
DOWN = "문제 Vuja De claw (NemoClaw) (http) 멈춤 · 24h 2.85%"


class FakeCygnus:
    def __init__(self, status=DOWN, fail_status=False):
        self.status, self.fail_status, self.calls = status, fail_status, []

    async def call(self, name, args, raw=False):
        self.calls.append((name, args, raw))
        if name == "infra_status":
            if self.fail_status:
                raise RuntimeError("down")
            return self.status
        return {"infra_health": HEALTH, "infra_alerts": "지금 울리는 경보 없음."}[name]


def run(monkeypatch, fake, name, raw=False):
    monkeypatch.setattr(cygnus, "available", lambda: True)
    monkeypatch.setattr(cygnus, "_shared_client", fake)
    return asyncio.run(cygnus.call(name, {}, raw=raw))


def test_health_and_alerts_name_the_down_service(monkeypatch):
    fake = FakeCygnus()
    out = run(monkeypatch, fake, "infra_health")
    assert out.startswith("지금 문제가 있는 서비스: 문제 Vuja De claw (NemoClaw) (http) 멈춤") and HEALTH in out
    assert cygnus.HOST_NOTE in out and ("infra_status", {"problems_only": True}, False) in fake.calls
    out = run(monkeypatch, FakeCygnus(), "infra_alerts")
    assert out.startswith("지금 울리는 경보 없음.") and "NemoClaw" in out.splitlines()[1]  # alerts first, then the service


def test_nothing_added_when_all_is_up_or_the_extra_read_fails(monkeypatch):
    assert run(monkeypatch, FakeCygnus(status="모든 모니터 정상"), "infra_health") == HEALTH
    assert run(monkeypatch, FakeCygnus(fail_status=True), "infra_health") == HEALTH  # never breaks the main answer


def test_raw_reads_and_other_tools_are_unchanged(monkeypatch):
    fake = FakeCygnus()
    assert run(monkeypatch, fake, "infra_health", raw=True) == HEALTH  # the TV parses this text
    assert fake.calls == [("infra_health", {}, True)]
    assert run(monkeypatch, FakeCygnus(), "infra_status") == DOWN
    assert "infra_status도 같이 봐" in cygnus.TOOLS["infra_health"][0]
