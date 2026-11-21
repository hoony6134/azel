#!/usr/bin/env python3
"""Stands in for `claude -p ... --output-format stream-json`; records its argv."""
import json, os, sys
open(os.environ["FAKE_CLAUDE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
prompt = sys.argv[2]
if "목표:" in prompt or "--resume" in sys.argv and "sess-t" in sys.argv:  # a background task (gateway tasks)
    import time
    out = lambda e: print(json.dumps(e), flush=True)  # noqa: E731
    say = lambda t, **u: out({"type": "assistant", "message": {"content": [{"type": "text", "text": t}], "usage": u}})  # noqa: E731
    if "TASK-SPEND" in prompt:
        for _ in range(5):
            say("계속 작업 중", input_tokens=100, output_tokens=1_000_000)  # $10 per message at Sonnet prices
            time.sleep(0.2)
    elif "TASK-SLOW" in prompt:
        say("시작했어요", input_tokens=10, output_tokens=10)
        time.sleep(30)
    elif "TASK-APPROVAL" in prompt:
        out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__macbook__run_command",
                                                           "input": {"command": "rm -rf ~/tmp/SECRET-ARG"}}], "usage": {}}})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                     "content": [{"type": "text", "text": '{"status": "pending_approval", "approval_id": "ap1"}'}]}]}})
        out({"type": "result", "result": "APPROVAL_PENDING ap1", "session_id": "sess-t", "total_cost_usd": 0.05})
    elif "--resume" in sys.argv:
        say("다시 실행했어요", input_tokens=10, output_tokens=10)
        out({"type": "result", "result": "승인받고 정리했어요." if "approval_id=ap1" in prompt else "거절돼서 나머지만 했어요.",
             "session_id": "sess-t", "total_cost_usd": 0.05})
    else:
        out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "mcp__macbook__list_dir",
                                                           "input": {"path": "~/SECRET-ARG"}}], "usage": {"input_tokens": 10}}})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "SECRET-RESULT"}]}})
        out({"type": "result", "result": "파일 세 개를 정리했어요.", "session_id": "sess-t", "total_cost_usd": 0.1})
    sys.exit(0)
if "느린" in sys.argv[2]:  # lets tests cancel a run mid-flight
    import time; time.sleep(3)
if "한도" in sys.argv[2]:  # the subscription's usage limit: an error result and no text
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached. Your limit will reset at 10pm (Asia/Seoul).",
                      "total_cost_usd": 0, "usage": {}}))
    sys.exit(0)
if "도구" in sys.argv[2]:  # one tool call, with arguments and a result that must never reach the app
    ev = lambda e: print(json.dumps({"type": "stream_event", "event": e}))  # noqa: E731
    ev({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "mcp__claude_ai_Cygnus_MCP__gistra_today", "input": {}}})
    ev({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q": "SECRET-ARG"}'}})
    ev({"type": "message_stop"})
    print(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "SECRET-RESULT"}]}}))
for t in ["네, ", "확인했어요."]:
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}}))
print(json.dumps({"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 5}, "session_id": "sess-1"}))
