#!/usr/bin/env python3
"""Stands in for `claude -p ... --output-format stream-json`; records its argv."""
import json, os, sys
open(os.environ["FAKE_CLAUDE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
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
