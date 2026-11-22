"""Server/infra questions in local mode: does Qwen call a tool instead of saying it has none? The live gateway's
own Qwen tool list (GET /v1/tools?caller=qwen) goes into the voice prompt exactly as respond() builds it; Qwen's
first move must be a [TOOL] line naming a fitting tool. Read-only, free (nothing is executed).

  uv run --no-project --with fastapi,uvicorn,httpx,numpy,python-multipart,websockets,soundfile,pysilero-vad \
      python eval_tools_local.py [gateway_url]
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

GW = sys.argv[1] if len(sys.argv) > 1 else "http://100.64.0.8:8892"
sys.argv = [sys.argv[0], "--check"]
import voice_server as V  # noqa: E402

CASES = [  # (utterance, the tools that answer it)
    ("서버 상태 확인해줘", {"infra_health", "infra_status", "infra_alerts"}),
    ("인프라 상태 어때", {"infra_health", "infra_status", "infra_alerts"}),
    ("대내부 상태 확인해줘", {"infra_health", "infra_status", "infra_alerts"}),  # the live STT slip
    ("스파크 디스크 얼마나 남았어", {"infra_health", "spark__run_command", "spark__list_dir"}),
]


async def main():
    token = (Path.home() / ".config/biseo/token").read_text().strip()
    async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
        defs = (await c.get(f"{GW}/v1/tools", params={"caller": "qwen"},
                            headers={"Authorization": f"Bearer {token}"})).json()
    system = V.local_prompt(V.system_prompt("미아", "normal", "", "밝고 다정한 비서", "")) + V.gateway_tools_text(defs) \
        + V.now_kst()
    good = 0
    for said, want in CASES:
        out = "".join([d async for d in V.unthink(V.llm_stream([{"role": "system", "content": system},
                                                               {"role": "user", "content": said}]), V.Unthink())])
        called = None
        if V.TOOL_TAG in out:
            try:
                tail = out[out.index(V.TOOL_TAG) + len(V.TOOL_TAG):]
                called = json.loads(tail[tail.index("{"):tail.rindex("}") + 1]).get("name")
            except ValueError:
                called = "?"
        ok = called in want
        good += ok
        print(f"{'OK  ' if ok else 'FAIL'} {said:20} → {called} | {out.strip()[:100]!r}")
    print(f"{good}/{len(CASES)} with {len(defs)} tools in the prompt")

if __name__ == "__main__":
    asyncio.run(main())
