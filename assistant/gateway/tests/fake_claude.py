#!/usr/bin/env python3
"""Stands in for `claude -p ... --output-format stream-json`; records its argv."""
import json, os, sys
open(os.environ["FAKE_CLAUDE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
if "느린" in sys.argv[2]:  # lets tests cancel a run mid-flight
    import time; time.sleep(3)
for t in ["네, ", "확인했어요."]:
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}}))
print(json.dumps({"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 5}, "session_id": "sess-1"}))
