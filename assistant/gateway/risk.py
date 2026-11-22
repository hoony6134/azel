"""Risk tiers for auto-approve: "safe" (runs without a tap while the user's grant is live) or "risky" (always a tap).
Commands the daemons hard-deny never get here.

Stages, cheapest first, the first confident one decides (and is logged):
  rules  deterministic: most commands, no cost
  qwen   the local model, for what the rules leave unclear
  haiku  Claude Haiku, when Qwen is not confident (skipped in local brain mode)
  default  still unclear: risky
Decisions are cached per exact (command, cwd, device).
"""
import json
import os
import re
import shlex

SAFE, RISKY = "safe", "risky"
CACHE_MAX = 2000
_cache: dict[tuple, tuple[str, str]] = {}

# Read or status only, whatever the arguments (as long as the rules below find nothing risky in them).
READ = {"ls", "cat", "head", "tail", "wc", "du", "df", "pwd", "rg", "grep", "egrep", "fgrep", "find", "echo", "date",
        "whoami", "id", "uname", "uptime", "ps", "top", "sw_vers", "which", "type", "file", "stat", "tree", "less",
        "nvidia-smi", "free", "vm_stat", "hostname", "ping", "dig", "nslookup", "traceroute", "lsof", "netstat",
        "journalctl", "dmesg", "say", "sort", "uniq", "cut", "awk", "sed", "jq", "diff", "md5", "shasum", "sha256sum",
        "basename", "dirname", "realpath", "readlink", "open", "pmset", "system_profiler", "ioreg", "sysctl"}
# Changes inside the working directory: fine when every path stays there.
LOCAL_WRITE = {"mkdir", "touch", "cp", "mv", "ln", "tee"}
INSTALL = re.compile(r"^(brew|apt|apt-get|dnf|yum|pacman|port|snap|pip|pip3|pipx|npm|pnpm|yarn|gem|cargo|uv|conda)\b.*\b"
                     r"(install|upgrade|uninstall|remove|update|add|tool\s+install|-g\b|global)\b")
SERVICE_RISKY = re.compile(r"\b(stop|disable|mask|unload|bootout|remove|kill|halt|poweroff|reboot|shutdown|suspend)\b")
SECRET_PATH = re.compile(r"(~|\$HOME|/Users/[^/\s]+|/home/[^/\s]+)?/?\.(ssh|gnupg|aws|kube|docker/config|netrc|env\b|"
                         r"config/biseo)|id_rsa|id_ed25519|\.pem\b|\.key\b|token|secret|password|keychain|/etc/(shadow|"
                         r"sudoers|passwd)", re.I)
UPLOAD = re.compile(r"(\s-T\s|--upload-file|\s-F\s|--form|\s-d\s*@|--data(-binary|-raw)?\s*@|\s-X\s*(POST|PUT|PATCH|"
                    r"DELETE)|--request\s+(POST|PUT|PATCH|DELETE))", re.I)
OFF_HOST = {"scp", "sftp", "ftp", "nc", "ncat", "netcat", "ssh", "mail", "sendmail", "mutt", "telnet", "rclone"}
USERS_PERMS = {"chmod", "chown", "chgrp", "chflags", "useradd", "usermod", "userdel", "groupadd", "passwd", "sudo", "su",
               "doas", "dscl", "visudo", "setfacl", "security", "spctl", "csrutil", "tccutil"}
ALWAYS_RISKY = {"dd", "mkfs", "diskutil", "fdisk", "shutdown", "reboot", "halt", "poweroff", "killall", "pkill",
                "osascript", "crontab", "env", "printenv", "eval", "exec", "xargs", "iptables", "ufw", "pfctl"}
SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _inside(path, cwd):
    """A path argument that stays in the working directory (relative, no ..), or /dev/null."""
    if path == "/dev/null":
        return True
    if not (path.startswith(("~", "/", "$")) or ".." in path.split("/")):
        return True
    if not cwd:
        return False
    p, base = os.path.normpath(os.path.expanduser(path)), os.path.normpath(os.path.expanduser(cwd))
    return p == base or p.startswith(base + os.sep)


def _one(part, cwd):
    """Tier for one simple command (no chaining), or None when the rules can't tell."""
    if re.search(r"\$\(|`|<\(|>\(", part):
        return RISKY  # command substitution: it could run anything
    if SECRET_PATH.search(part):
        return RISKY
    redirects = re.findall(r"(?:^|\s)\d?>>?\s*(\S+)", part)
    if any(not _inside(r, cwd) for r in redirects):
        return RISKY  # writing a file outside the working directory
    part = re.sub(r"(?:^|\s)\d?>>?\s*\S+", " ", part).strip()
    try:
        t = shlex.split(part)
    except ValueError:
        return RISKY
    if not t:
        return SAFE
    while t and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t[0]):
        t = t[1:]  # FOO=1 cmd
    if not t:
        return SAFE
    name, args = os.path.basename(t[0]).lower(), t[1:]
    if name in ALWAYS_RISKY or name in USERS_PERMS or name in OFF_HOST:
        return RISKY
    if INSTALL.search(" ".join(t)):
        return RISKY
    if name in ("rm", "rmdir", "unlink", "trash", "shred", "srm"):  # deleting always asks (the user's rule)
        return RISKY
    if name == "kill":
        return RISKY if any(a in ("-9", "-KILL", "-SIGKILL") for a in args) else None
    if name == "rsync":
        return RISKY if any(":" in a and not a.startswith("-") for a in args) or "--delete" in args else \
            (SAFE if all(_inside(a, cwd) for a in args if not a.startswith("-")) else RISKY)
    if name in ("curl", "wget"):
        if UPLOAD.search(" " + " ".join(args) + " ") or "--post-data" in " ".join(args) or "--post-file" in " ".join(args):
            return RISKY
        out = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-o", "--output", "-O", "--output-document")]
        return SAFE if all(_inside(o, cwd) for o in out) else RISKY
    if name == "git":
        sub = args[0] if args else ""
        if sub in ("push", "clean", "filter-branch", "gc", "reflog") or (sub == "reset" and "--hard" in args) or \
                (sub == "branch" and any(a in ("-D", "-d", "--delete") for a in args)) or "--force" in args:
            return RISKY
        return SAFE if sub in ("status", "diff", "log", "show", "branch", "add", "commit", "checkout", "switch",
                               "stash", "fetch", "pull", "remote", "tag", "rev-parse", "blame", "grep") else None
    if name in ("systemctl", "launchctl", "service"):
        flat = " ".join(args)
        if SERVICE_RISKY.search(flat) or "*" in flat:
            return RISKY
        if re.search(r"\b(status|is-active|is-enabled|list|list-units|print|show|cat)\b", flat):
            return SAFE
        units = [a for a in args if not a.startswith("-") and a not in ("restart", "kickstart", "start", "--user")]
        return SAFE if re.search(r"\b(restart|kickstart|start)\b", flat) and len(units) == 1 else RISKY
    if name in ("docker", "podman"):
        sub, rest = (args[0] if args else ""), args[1:]
        flat = " ".join(args)
        if sub in ("rm", "rmi", "prune", "kill", "stop") or re.search(r"\b(prune|volume\s+rm|system\s+prune|down\s+-v|"
                                                                       r"network\s+rm|rm\s)", flat):
            return RISKY
        if sub in ("ps", "logs", "inspect", "stats", "images", "top", "port", "version", "info"):
            return SAFE
        if sub == "restart":
            return SAFE if len([a for a in rest if not a.startswith("-")]) == 1 else RISKY
        if sub == "compose":
            if re.search(r"\b(ps|logs|config|top)\b", flat):
                return SAFE
            m = re.search(r"\brestart\b(.*)", flat)
            return SAFE if m and len([a for a in m.group(1).split() if not a.startswith("-")]) == 1 else RISKY
        return None
    if name == "find":
        return RISKY if any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args) else SAFE
    if name == "sed":
        return RISKY if any(a.startswith("-i") for a in args) and not all(
            _inside(a, cwd) for a in args[1:] if not a.startswith("-") and "/" in a) else SAFE
    if name in READ:
        return SAFE
    if name in LOCAL_WRITE:
        paths = [a for a in args if not a.startswith("-")]
        return SAFE if paths and all(_inside(p, cwd) for p in paths) else RISKY
    return None


def rules(command, cwd=""):
    """safe | risky | None: every part of a chain must be safe for the whole to be."""
    parts = [p for p in SPLIT.split((command or "").strip()) if p]
    if not parts:
        return RISKY
    tiers = [_one(p, cwd) for p in parts]
    if RISKY in tiers:
        return RISKY
    return SAFE if all(t == SAFE for t in tiers) else None


PROMPT = ("너는 개인 비서가 사용자 컴퓨터에서 명령을 실행하기 전에 위험도를 가르는 분류기야. JSON {\"tier\": \"safe\"|\"risky\", "
          "\"confidence\": 0~1} 하나만 답해. risky: 지우기·덮어쓰기(여럿·재귀), 서비스나 컴퓨터 끄기·재부팅, 패키지 설치·업그레이드, "
          "사용자·권한 변경, 파일이나 데이터를 다른 곳으로 보내기, 비밀(키·토큰·비밀번호) 근처, git push, 컨테이너·볼륨 삭제, 남의 "
          "프로세스 강제 종료, 돈이나 메시지 보내기, 그 밖에 되돌리기 어려운 것. safe: 조회·상태 보기, 서비스 하나 재시작, 작업 폴더 안의 "
          "파일 만들기·고치기. 모르겠으면 risky에 낮은 confidence.")
CONFIDENT = 0.8


def _parse(out):
    try:
        d = out if isinstance(out, dict) else json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, AttributeError):
        return None
    tier, conf = d.get("tier"), d.get("confidence")
    if tier not in (SAFE, RISKY) or not isinstance(conf, (int, float)):
        return None
    return tier, float(conf)


async def classify(command, cwd="", device="", qwen=None, haiku=None):
    """(tier, stage). `qwen(system, user)` / `haiku(system, user)`: model calls returning a dict or text (haiku None in
    local brain mode). Never raises: a failing stage just passes to the next one."""
    key = (command, cwd, device)
    if key in _cache:
        return _cache[key]
    tier, stage = rules(command, cwd), "rules"
    user = f"기기: {device}\n작업 폴더: {cwd or '(없음)'}\n명령: {command[:1000]}"
    for name, fn in (("qwen", qwen), ("haiku", haiku)):
        if tier is not None or fn is None:
            continue
        try:
            got = _parse(await fn(PROMPT, user))
        except Exception:
            got = None
        if got and got[1] >= CONFIDENT:
            tier, stage = got[0], name
    if tier is None:
        tier, stage = RISKY, "default"
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (tier, stage)
    return tier, stage
