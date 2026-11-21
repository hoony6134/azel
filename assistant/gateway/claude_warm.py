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
MAX_PROCS = int(os.environ.get("BISEO_WARM_MAX", "4"))  # warm processes per gateway; the least recently used goes
TURN_IDLE_S = float(os.environ.get("BISEO_WARM_TURN_S", "120"))  # a turn silent this long: the process is hung


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
                while True:
                    try:
                        raw = await asyncio.wait_for(self.proc.stdout.readline(), TURN_IDLE_S)
                    except asyncio.TimeoutError:  # hung (a stuck MCP server, a wedged CLI): kill it, never reuse
                        await self.close()
                        self.turns = MAX_TURNS
                        raise RuntimeError("warm claude produced nothing for too long")
                    if not raw:
                        break  # the process exited
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
    """Warm processes by voice session (at most MAX_PROCS). A changed prompt or device set starts a new one; every
    process that leaves the pool is closed (now, or right after its running turn), with its token-bearing config."""

    def __init__(self):
        self.procs: dict[str, WarmClaude] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._loop = None

    def _lock(self, session):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # locks belong to one event loop
            self.locks, self._loop = {}, loop
        return self.locks.setdefault(session, asyncio.Lock())

    async def get(self, session, model, style, allowed, cwd, mcp_servers=None, resume=None, settings=None, env=None):
        """The session's warm process. One creation at a time per session, so a prewarm racing the first ask
        yields one process, not two."""
        async with self._lock(session):
            w = self.procs.get(session)
            if w and (not w.alive() or w.key != key(style, allowed, mcp_servers, settings)):
                self.procs.pop(session, None)
                await self._retire(w)
                w = None
            if w is None:
                await self._make_room()
                w = WarmClaude(model, style, allowed, cwd, mcp_servers, resume, settings, env)
                await w.start()
                self.procs[session] = w
            w.used = time.monotonic()
            return w

    async def _make_room(self):
        while len(self.procs) >= MAX_PROCS:
            s, w = min(self.procs.items(), key=lambda kv: kv[1].used)
            self.procs.pop(s)
            await self._retire(w)

    async def _retire(self, w):
        """Close now, or right after the turn it is serving (never kill an answer mid-sentence)."""
        if w.lock.locked():
            asyncio.get_running_loop().create_task(self._close_after_turn(w))
        else:
            await w.close()

    @staticmethod
    async def _close_after_turn(w):
        async with w.lock:
            pass
        await w.close()

    async def close(self, session):
        """The call ended: its process goes (it would otherwise idle until the reaper)."""
        w = self.procs.pop(session, None)
        if w:
            await self._retire(w)

    async def reap(self):
        """Close idle processes; call this every minute."""
        now = time.monotonic()
        for s, w in list(self.procs.items()):
            if not w.lock.locked() and (now - w.used > IDLE_S or not w.alive()):
                await w.close()
                self.procs.pop(s, None)

    async def close_all(self):
        for w in list(self.procs.values()):
            await w.close()
        self.procs.clear()
