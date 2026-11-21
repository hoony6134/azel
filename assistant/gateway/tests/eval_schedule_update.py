"""Editing schedules by voice-style requests, end to end through agent_loop on Qwen and the Claude API (Haiku):
the model must find the item, pick the scope, and the bridge must hand the gateway the right change. The schedule is
the in-memory one from test_tools_bridge (nothing real changes, nothing rings). A few tenths of a cent.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_schedule_update.py
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import agent_loop  # noqa: E402
import late_night  # noqa: E402
from test_tools_bridge import Rules  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=KST)  # Monday
QWEN = {"url": os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions"),
        "model": os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next"), "prices": [0, 0]}
CLAUDE = {"api_key": (Path.home() / ".config/biseo/anthropic_api_key").read_text().strip(), "model": "haiku"}


def at_(h, d=23):
    return f"2026-11-{d}T{h:02d}:00:00+09:00"


# (request, check(world, final text) -> bool, expected)
CASES = [
    ("오늘 오후 10시 알림 6시로 바꿔줘", lambda w, t: w.updates == [("one", {"when": at_(18)}, None)], "one → 18:00"),
    ("오늘 2시 스트레칭은 3시로 미뤄줘", lambda w, t: w.updates == [("rule", {"when": at_(15)}, at_(14))], "occurrence 14→15"),
    ("매일 아침 7시에 깨우는 거 8시로 바꿔줘",
     lambda w, t: len(w.updates) == 1 and w.updates[0][0] == "old" and w.updates[0][2] is None and
     (w.updates[0][1].get("repeat_cron", "").split()[:2] == ["0", "8"] or
      w.updates[0][1].get("when", "").endswith("08:00:00+09:00")), "rule old → 8:00"),
    ("약 먹기 알림 내용을 물 마시기로 바꿔줘", lambda w, t: w.updates == [("one", {"purpose": "물 마시기"}, None)], "purpose"),
    ("스트레칭은 전화 말고 알림으로 바꿔줘", lambda w, t: w.updates == [("rule", {"notify": "push"}, None)], "notify push, rule"),
    ("내일 오전 10시 스트레칭만 빼줘", lambda w, t: w.cancels == [("rule", None, at_(10, 24))] and not w.updates,
     "skip one fire"),
    # bare hours: the gateway resolves the words, never the model
    ("약 먹기 알림 11시 반으로 미뤄줘", lambda w, t: w.updates == [("one", {"when": "2026-11-23T23:30:00+09:00"}, None)],
     "inherit 오후 → 23:30"),
    ("7시에 전화해줘", lambda w, t: [x["at"] for x in w.schedule] == [at_(19)], "call 19:00 today"),
    ("내일 7시에 전화해줘", lambda w, t: not w.schedule and any(x in t for x in ("아침", "오전"))
     and any(x in t for x in ("저녁", "오후")), "asks 아침/저녁"),
    ("3시에 전화해줘", lambda w, t: [x["at"] for x in w.schedule] == [at_(15)], "call 15:00"),
]


def system():
    return (f"너는 음성 비서야. 지금은 {NOW:%Y-%m-%d %H:%M} (KST, 월요일). 사용자의 예약 전화를 도구로 바꾸거나 취소해. "
            "먼저 schedule_list로 목록을 봐." + late_night.prompt_rule(NOW))


class World(Rules):
    async def schedule_create(self, body, created_by):  # a realistic confirmation, so models don't retry
        self.schedule.append(body)
        t = datetime.fromisoformat(body["at"])
        return {"at": body["at"]}, f"{t.month}월 {t.day}일 {late_night.clock_ko(t.hour, t.minute)}에 전화드릴게요."

    async def schedule_text(self):
        return "\n".join(f"- id {i['id']}: {i.get('rule_text') or i['spoken']} 다음 {i['at']} 목적 {i['purpose'] or '-'}"
                         for i in self.items)

    def bridge(self):
        b = super().bridge()
        b.d["schedule_list_text"] = self.schedule_text
        b.d["cygnus"] = lambda: ([], None)
        b.d["now"] = lambda: NOW  # the fake Cygnus tool has no real schema (the Claude API would 400)
        return b


async def one(backend, text):
    w = World()

    async def never():
        return False
    last = ""
    async for ev in agent_loop.run(backend, system(), text, w.bridge(), QWEN if backend == "qwen" else CLAUDE,
                                   0.05, 2, never):
        if ev["type"] in ("done", "failed"):
            last = ev.get("result") or ev.get("message", "")
    return w, last


async def main():
    score = {}
    only = [c for c in CASES if os.environ.get("EVAL_ONLY", "") in c[0]]
    for backend in [b for b in ("qwen", "claude_api") if b in os.environ.get("EVAL_BACKENDS", "qwen,claude_api")]:
        score[backend] = 0
        for text, check, want in only:
            try:
                w, last = await one(backend, text)
            except Exception as e:  # noqa: BLE001
                w, last = World(), f"ERROR {type(e).__name__}: {e}"[:120]
            good = check(w, last)
            score[backend] += good
            print(f"{backend:10} {'OK  ' if good else 'FAIL'} {text:28} want {want:20} | {w.updates} {w.cancels} "
                  f"{[x.get('at') for x in w.schedule]} | {last[:70]!r}")
    print(" | ".join(f"{b} {s}/{len(only)}" for b, s in score.items()))

if __name__ == "__main__":
    asyncio.run(main())
