"""Warm Claude: one long-lived `claude -p --input-format stream-json` process per voice session.

A fresh `claude -p` per turn pays process start plus claude.ai MCP connection (~5 s) before the model even
starts. A warm process pays that once per call (ideally while the user is still talking: `prewarm` on the
call's start) and keeps the conversation in-process, so later turns start at model latency.

Used by gateway.py's ask_claude_code: `pool.turn(...)` yields the same stream-json lines as a fresh process's
stdout, ending with the turn's `result` line, so the gateway's parser is unchanged.
"""
import asyncio
import json
import os
import tempfile
import time

IDLE_S = float(os.environ.get("BISEO_WARM_IDLE_S", "600"))  # recycle a process unused this long
MAX_TURNS = 40  # recycle long conversations (context grows; a fresh one resumes the session anyway)
DRAIN_S = 5  # after a barge-in, wait this long for the interrupted turn's result, then kill (and respawn)


def base_cmd(model, style, allowed, mcp_config=None, resume=None, settings=None):
    """The gateway's flags, without the prompt (`-p <text>` for a one-shot, stdin stream-json for a warm one).
    `allowed`: every MCP tool prefix the run may use; `settings`: run-scoped settings JSON (connectors.run_args)."""
    cmd = [os.environ.get("BISEO_CLAUDE", "claude"), "--model", model, "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--append-system-prompt", style,
           "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "--allowedTools", *allowed]
    if settings:
        cmd += ["--settings", settings]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    if resume:
        cmd += ["--resume", resume]
    return cmd


class WarmClaude:
    """`mcp_servers`: the --mcp-config servers (they carry the token): kept in a 0600 temp file for the process's
    life, deleted on close."""

    def __init__(self, model, style, allowed, cwd, mcp_servers=None, resume=None, settings=None, env=None):
        self.model, self.style, self.allowed, self.servers, self.resume = model, style, allowed, mcp_servers, resume
        self.settings, self.env = settings, env or {}
        self.cwd, self.proc, self.lock, self.cfg = cwd, None, asyncio.Lock(), None
        self.used, self.turns, self.interrupted = time.monotonic(), 0, False
        self.key = key(style, allowed, mcp_servers, settings)  # the model switches in place (set_model)

    async def start(self):
        if self.servers:
            fd, self.cfg = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump({"mcpServers": self.servers}, f)
        self.cmd = [*base_cmd(self.model, self.style, self.allowed, self.cfg, self.resume, self.settings),
                    "-p", "--input-format", "stream-json"]
        env = os.environ | {"MCP_CONNECTION_NONBLOCKING": "false"} | self.env
        self.proc = await asyncio.create_subprocess_exec(*self.cmd, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL, limit=2**24)

    def alive(self):
        return self.proc is not None and self.proc.returncode is None and self.turns < MAX_TURNS

    async def turn(self, text, model=None):
        """Raw stream-json lines for one user message, through its `result` line. Barge-in (aclose/cancel): an
        interrupt control request ends the turn in the CLI (~20 ms); the next turn first reads up to that turn's
        result, under the same lock, so it never sees the old answer. A process that does not stop is killed."""
        async with self.lock:
            if self.interrupted:
                try:
                    await asyncio.wait_for(self._until_result(), DRAIN_S)
                    self.interrupted = False
                except asyncio.TimeoutError:
                    await self.close()
                    raise RuntimeError("warm claude did not stop after an interrupt")
            self.turns += 1
            if model and model != self.model:  # e.g. Haiku for a lookup in a call that started on Sonnet
                self._write({"type": "control_request", "request_id": f"model-{self.turns}",
                             "request": {"subtype": "set_model", "model": model}})
                self.model = model
            self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
            await self.proc.stdin.drain()
            done = False
            try:
                async for raw in self.proc.stdout:
                    if _is_result(raw):
                        done = True
                    yield raw
                    if done:
                        break
            finally:
                self.used = time.monotonic()
                if not done and self.proc.returncode is None:
                    self.interrupted = True  # drained at the start of the next turn
                    try:
                        self._write({"type": "control_request", "request_id": f"int-{self.turns}",
                                     "request": {"subtype": "interrupt"}})
                    except (BrokenPipeError, ConnectionResetError):
                        self.turns = MAX_TURNS
                elif not done:
                    self.turns = MAX_TURNS  # died mid-turn: never reuse

    def _write(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    async def _until_result(self):
        async for raw in self.proc.stdout:
            if _is_result(raw):
                return
        raise asyncio.TimeoutError  # stdout closed: the process is gone

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self.cfg and os.path.exists(self.cfg):
            os.unlink(self.cfg)


def key(style, allowed, mcp_servers, settings=None):
    return style, tuple(allowed), json.dumps(mcp_servers, sort_keys=True), settings


def _is_result(raw):
    try:
        return json.loads(raw).get("type") == "result"
    except ValueError:
        return False


class Pool:
    """Warm processes by voice session. A changed model, prompt or device set starts a new one."""

    def __init__(self):
        self.procs: dict[str, WarmClaude] = {}

    async def get(self, session, model, style, allowed, cwd, mcp_servers=None, resume=None, settings=None, env=None):
        w = self.procs.get(session)
        if w and (not w.alive() or w.key != key(style, allowed, mcp_servers, settings)):
            if not w.lock.locked():
                await w.close()
            w = None  # one still mid-turn is left to the reaper
        if w is None:
            w = WarmClaude(model, style, allowed, cwd, mcp_servers, resume, settings, env)
            await w.start()
            self.procs[session] = w
        return w

    async def reap(self):
        """Close idle processes; call this every minute."""
        now = time.monotonic()
        for s, w in list(self.procs.items()):
            if not w.lock.locked() and (now - w.used > IDLE_S or not w.alive()):
                await w.close()
                self.procs.pop(s, None)

    async def close_all(self):
        for w in self.procs.values():
            await w.close()
        self.procs.clear()
