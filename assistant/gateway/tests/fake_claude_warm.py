#!/usr/bin/env python3
"""Stands in for a warm `claude -p --input-format stream-json`: one answer per user message, same process.
A message with "느린" answers slowly (3 s) unless an interrupt control request arrives first."""
import json, os, select, sys, time
open(os.environ["FAKE_CLAUDE_ARGS"], "a").write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:]}) + "\n")
buf, n = b"", 0


def out(o):
    print(json.dumps(o), flush=True)


def lines(timeout=None):
    """Next stdin line, or None after `timeout` seconds."""
    global buf
    end = None if timeout is None else time.monotonic() + timeout
    while b"\n" not in buf:
        wait = None if end is None else max(0, end - time.monotonic())
        if not select.select([0], [], [], wait)[0]:
            return None
        chunk = os.read(0, 65536)
        if not chunk:
            sys.exit(0)
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return json.loads(line)


def delta(t):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}})


while True:
    m = lines()
    if m.get("type") == "control_request":
        open(os.environ["FAKE_CLAUDE_ARGS"] + ".ctl", "a").write(json.dumps(m["request"]) + "\n")
    if m.get("type") != "user":
        continue
    n += 1
    text = m["message"]["content"][0]["text"]
    delta("네, ")
    if "느린" in text:
        got = lines(3)
        if got and got.get("type") == "control_request":
            open(os.environ["FAKE_CLAUDE_ARGS"] + ".ctl", "a").write(json.dumps(got["request"]) + "\n")
            out({"type": "control_response", "response": {"subtype": "success", "request_id": got["request_id"]}})
            out({"type": "result", "subtype": "error_during_execution", "is_error": True, "usage": {}})
            continue
    delta(f"{n}번째예요.")
    out({"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 1, "output_tokens": 1}, "session_id": "warm-1"})
