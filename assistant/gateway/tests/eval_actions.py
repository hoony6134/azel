"""One eval case per action (the conformance test checks every registry action has one): a request a person would
make, run through agent_loop on Qwen and the Claude API (Haiku), checking the model picked that action. The world is
the in-memory one from test_tools_bridge: nothing rings, nothing runs, no approval is filed. schedule_create is not in
model tool lists (call_user covers it there), so its case runs the app's way, through the same handler.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_actions.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import agent_loop  # noqa: E402
from test_tools_bridge import Rules  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=KST)
QWEN = {"url": os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions"),
        "model": os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next"), "prices": [0, 0]}
# action -> (request, how it is reached: "model" or "app" (args given))
CASES = {
    "call_user": ("지금 바로 나한테 전화 좀 해줘", "model"),
    "schedule_create": ({"reason": "약 먹기", "when_text": "3시"}, "app"),
    "schedule_list": ("예약된 전화 뭐 있어?", "model"),
    "schedule_update": ("약 먹기 알림 내용을 물 마시기로 바꿔줘", "model"),
    "schedule_cancel": ("내일 오전 10시 스트레칭만 빼줘", "model"),
    "create_task": ("맥북 다운로드 폴더 정리하는 거 작업으로 맡길게, 끝나면 알려줘", "model"),
    "task_status": ("아까 맡긴 작업 어떻게 됐어?", "model"),
    "overnight_list": ("밤새 하기로 한 거 뭐 있어?", "model"),
    "task_cancel": ("작업 a00000000001 그만해", "model"),
    "approval_status": ("승인 기다리는 거 있어?", "model"),
    "memory_recall": ("내가 좋아하는 커피 뭐였지?", "model"),
    "people_lookup": ("내 주변 사람 누구 있어?", "model"),
    "remember": ("내가 좋아하는 커피는 아이스 바닐라 라떼야, 기억해 줘", "model"),
    "forget": ("커피 취향 기억한 거 지워 줘", "model"),
    "memory_list": ("너 나에 대해 뭐 기억하고 있어? 목록 보여 줘", "model"),
    "web_search": ("오늘 광주 날씨 어때?", "model"),
    "web_fetch": ("https://example.com 이 페이지에 뭐라고 써 있어?", "model"),
    "agenda": ("이번 주 일정 뭐 있어?", "model"),
    "weather": ("내일 서울 날씨 어때?", "model"),
    "music_play": ("아이유 노래 틀어줘", "model"),
    "music_control": ("다음 곡으로 넘겨줘", "model"),
    "home_scene": ("영화 모드로 집 불 꺼줘", "model"),
    "auto_approve": ("권한 줄 테니까 이번 통화 동안은 알아서 해", "model"),
    "end_call": ("이제 됐어 끊어", "model"),
    "web_task": ("네이버에서 도쿄 왕복 항공권 알아보고 비교표 PDF로 만들어줘", "model"),
    "phone_health": ("오늘 걸음 수 얼마야?", "model"),
    "phone_sleep": ("어제 몇 시간 잤어?", "model"),
    "phone_workouts": ("이번 주 운동 얼마나 했어?", "model"),
    "phone_calendar": ("아이폰 캘린더에 내일 뭐 있어?", "model"),
    "phone_reminders": ("미리 알림에 뭐 있어?", "model"),
    "phone_contacts": ("연락처에서 김철수 번호 찾아 줘", "model"),
    "phone_location": ("나 지금 어디야?", "model"),
    "phone_now_playing": ("지금 듣고 있는 노래 뭐야?", "model"),
}


class World(Rules):
    def __init__(self):
        super().__init__()
        self.called = []

    def bridge(self):
        b = super().bridge()
        b.d["cygnus"] = lambda: ([], None)
        b.d["now"] = lambda: NOW
        b.d["task_cancel"] = self.task_cancel
        b.d["approval_list"] = self.approval_list
        real = b.dispatch

        async def spy(name, *a, **k):
            self.called.append(name)
            return await real(name, *a, **k)
        b.dispatch = spy
        return b

    async def task_cancel(self, tid):
        return {"id": tid, "status": "cancelled"}, "작업을 멈췄어요."

    async def approval_list(self):
        return []


async def one(backend, request):
    w = World()

    async def never():
        return False
    cfg = QWEN if backend == "qwen" else {"api_key": (Path.home() / ".config/biseo/anthropic_api_key").read_text().strip(),
                                          "model": "haiku"}
    system = f"너는 음성 비서야. 지금은 {NOW:%Y-%m-%d %H:%M} (KST, 월요일). 도구로 처리해."
    async for _ in agent_loop.run(backend, system, request, w.bridge(), cfg, 0.05, 2, never):
        pass
    return w.called


async def main():
    score = {}
    for backend in ("qwen", "claude_api"):
        score[backend] = 0
        for action, (request, via) in CASES.items():
            if via == "app":
                w = World()
                r = await w.bridge().run(action, request, caller="app", surface="rest")
                good, called = r["ok"], [action]
            else:
                try:
                    called = await one(backend, request)
                except Exception as e:  # noqa: BLE001
                    called = [f"ERROR {type(e).__name__}"]
                good = action in called
            score[backend] += good
            print(f"{backend:10} {'OK  ' if good else 'FAIL'} {action:16} {called}")
    print(" | ".join(f"{b} {s}/{len(CASES)}" for b, s in score.items()))

if __name__ == "__main__":
    asyncio.run(main())
