"""Spark voice server: STT -> router (Qwen or Claude via gateway) -> Qwen3-TTS per sentence.

Contract: ../PROTOCOL.md sections 1-2.
  WS   /v1/call    live call: server-side VAD, barge-in, optional call opening
  POST /v1/turn    one WAV turn -> NDJSON (benchmark)
  POST /v1/audio/transcriptions   OpenAI-compatible STT
  GET  /health     (no auth)
"""
import asyncio
import base64
import contextlib
import contextvars
import hashlib
import hmac
import io
import ipaddress
import json
import math
import logging
import os
import re
import sys
import time
import traceback
import uuid
from collections import defaultdict, deque
from datetime import date as Date, datetime, timedelta, timezone

import httpx
import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

import ko_read  # digits read out in Korean for TTS only, shipped beside this file (Dockerfile)
import late_night  # the gateway's spoken-time rules, shipped beside this file (Dockerfile)

LLM_URL = os.environ.get("LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3.8-Flash-Next")
TTS_URL = os.environ.get("TTS_URL", "http://100.64.0.11:8890/v1/audio/speech")
TTS_VOICE = os.environ.get("TTS_VOICE", "sohee")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-large-v3-turbo")
# Only SHA-256 digests of accepted tokens live on this shared team machine, never a token itself.
# One "<label> <hex digest>" per line: client (apps, /v1/call and /v1/turn), link (full gateways),
# link_limited (team-device gateways: always limited, whatever their hello says).
HASH_FILE = os.environ.get("TOKEN_HASH_FILE", "/run/secrets/biseo_token_hashes")
LABELS = ("client", "link", "link_limited")
DEFAULT_NAME = os.environ.get("ASSISTANT_NAME", "아젤")  # until the gateways push settings with assistant_name
MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
END_SILENCE_MS = int(os.environ.get("END_SILENCE_MS", "500"))
# Speech under way and no audio at all for this long: the client stopped sending, so the turn is over. The quiet that
# ends a turn has to arrive as frames; a one-breath command sent as one block, then no live frames, waited ~17 s for
# them (Mac, live 2026-11-23). Live audio comes every ~100 ms, so this never cuts a healthy stream.
END_STALL_S = float(os.environ.get("END_STALL_MS", "800")) / 1000
# A held recording (the Mac's one-breath command) comes faster than real time: one long message, or 100 ms chunks back
# to back. Its pauses are not the end of the turn (live 2026-11-23: "오늘 아침 … 일정 삭제해" was cut at a 0.5 s pause
# and the rest dropped piece by piece); the turn ends on live quiet after it, or on the stall.
BURST_S = 0.3  # one message longer than this is not live audio
BURST_GAP = 0.5  # a message that came sooner than half the previous one's length after it: still the burst
KST = timezone(timedelta(hours=9))
MAX_UPLOAD = 16000 * 2 * 60  # one minute of 16 kHz PCM16
ROUTE_TAG = "[CLAUDE]"
# Conversation, not dictation: what Qwen does with a proposal or a passing remark (user report: "끝말 입기 하자" got only
# "네, 끝말잇기 하자." back).
AGENCY = ("사용자의 말을 그대로 따라 하거나 '네, ~하자'로만 끝내지 마. 게임, 퀴즈, 이야기, 연습처럼 같이 하자는 말이면 바로 "
          "시작해서 네 첫 수를 두고, 다음 턴에도 이어서 진행해. 끝말잇기, 스무고개, 퀴즈는 game 도구로 시작하고 끝내. 이야기면 "
          "첫 장면을 두세 문장으로, 브레인스토밍이면 아이디어 두세 개, 연습이면 첫 연습 문제나 첫 대사로 이번 답에서 바로 시작해. "
          "음성 인식이 틀린 말('끝말 입기')은 뜻으로 알아들어. 잡담에는 질문이나 생각, 성격에 맞는 가벼운 농담을 하나 보태.")

# The app shows a card for what a lookup returns (cards-contract.md). Live 2026-11-23: "오늘 애플 주식 차트 보여줘" got
# "차트는 음성으로 보여드릴 수 없어요" and no lookup, so no card.
CARDS_LINE = ("사용자의 앱 화면에는 도구 결과가 카드로 떠: 주가는 실시간 차트(기간 전환 가능), 일정, 할 일, 날씨, 서버 상태. "
              "'차트 보여줘', '보여줘', '띄워줘'라고 하면 맞는 도구(주가는 stock_quote)를 불러서 카드를 띄우고, "
              "'애플 차트 띄워 드렸어요. 지금 …달러, …% 올랐어요.'처럼 짧게 말해. 화면에 보여 줄 수 없다거나 폰·PC에서 "
              "보라고 하지 마.")

# reply_length (shared setting, the app exposes it): how long answers run. The short first clause stays for latency.
LENGTH_STYLE = {
    "brief": "짧게 한두 문장으로 요점만 말해.",
    "normal": "질문에 맞는 길이로 말해. 잡담과 간단한 답은 한두 문장, 설명·요약·일정·상태 보고는 네다섯 문장 정도로 자연스럽게.",
    "detailed": "질문에 맞는 길이로 말해. 잡담은 짧게, 설명·요약·일정·상태 보고는 여섯에서 여덟 문장까지 자세히, 말하듯 자연스럽게.",
}
LENGTH_TOKENS = {"brief": 250, "normal": 600, "detailed": 1000}  # Qwen max_tokens per reply_length
SYSTEM_BODY = (
    "지금은 음성 통화 중이라 답이 그대로 소리로 읽혀. "
    "한국어로 자연스럽게 말해. 말투(존댓말/반말)는 비서 성격을 따르고, 성격에 정해져 있지 않으면 존댓말로 말해. "
    "'친구', '동생' 같은 관계 말만으로는 반말로 바꾸지 마: 성격에 반말이라고 쓰여 있을 때만 반말이야. "
    "한 번의 답 안에서 첫 문장부터 마지막 문장까지 말투를 똑같이 유지해(해요체면 끝까지 해요체, 반말이면 끝까지 반말). "
    "한자, 일본어, 중국어, 영어 문장은 섞지 마(제품이나 회사 이름 같은 고유명사만 예외). "
    "'네'나 '알겠습니다'만 하고 끝내지 마. 상장 여부, 주가, 환율, 날씨, 뉴스 같은 지금의 사실은 이번 대화의 도구 결과로만 말하고, "
    "네가 원래 알던 지식으로는 말하지 마(오래됐을 수 있어). 사용자의 일정, 할 일, 마감, 메일은 절대 짐작하거나 지어내지 마. "
    "전화, 예약, 알림, 실행, 전송 같은 행동은 이번 대화에서 도구 결과로 확인됐을 때만 했다거나 하겠다고 말해. 너 혼자서는 예약할 수 "
    "없으니, 도구가 없으면 할 수 없다고 하고 다시 물어봐. "
    "마크다운, 목록, 이모지, 괄호 설명, URL은 쓰지 마. 숫자는 한글로 풀어 쓰지 말고 아라비아 숫자와 단위로 써(1,333원, 2도, 18.5%, 3시 30분). "
    "도구 결과의 영어 상태어(firing, resolved, pending, DOWN, critical 같은 것), 키 이름, 기호는 그대로 읽지 말고 "
    "'경보가 울리고 있는', '해결된', '멈춘'처럼 자연스러운 한국어로 바꿔 말해. 서버 이름은 그대로 말해. "
    f"중요: 일정, 할 일, 과제, 수업, 메일, 서버 상태, 예전에 한 말, 검색, 날씨, 주가, macbook이나 서버 작업은 네가 가진 도구와 "
    f"참고 메모로 먼저 직접 해(Claude에 넘기지 마). 그 도구로 해 보고도 정말 할 수 없을 때만(코드 짜기, 긴 여러 단계 작업, "
    f"맞는 도구가 없는 일) 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 간단한 상식, 계산, 방금 대화 내용 정리는 네가 바로 답해. " + AGENCY
)


def assistant_name(settings):
    return settings.get("assistant_name") or DEFAULT_NAME


def reply_length(settings):
    return settings.get("reply_length") if settings.get("reply_length") in LENGTH_STYLE else "normal"


# ---------- personas (PROTOCOL.md section 3): the call's persona sets name, character and voice ----------
CALL_PERSONA: contextvars.ContextVar = contextvars.ContextVar("call_persona", default=None)  # set per /v1/call


def personas_of(settings):
    """Live personas from the shared settings; a gateway from before personas gives the one assistant."""
    ps = [p for p in settings.get("personas") or [] if isinstance(p, dict) and p.get("id") and not p.get("deleted_at")]
    return ps or [{"id": "azel", "name": assistant_name(settings), "voice": TTS_VOICE, "voice_style": "",
                   "personality": settings.get("personality") or "", "primary": True}]


def pick_persona(settings, pid=None):
    ps = personas_of(settings)
    return next((p for p in ps if p["id"] == pid), None) or next((p for p in ps if p.get("primary")), ps[0])


def persona_text(p, settings):
    """A persona's character ("" = the default the gateway sends as personality_default)."""
    t = p.get("personality")
    return t.strip() if isinstance(t, str) and t.strip() else str(settings.get("personality_default") or "")


def current_persona(settings):
    return CALL_PERSONA.get() or pick_persona(settings)


def personality(settings):
    """Azel's character from the shared settings ("" = the default the gateway sends as personality_default)."""
    p = settings.get("personality")
    return p.strip() if isinstance(p, str) and p.strip() else str(settings.get("personality_default") or "")


def character_line(name, text, title=""):
    """The persona's character as the assistant's own (same wording in gateway.py and voice_server.py). Without the
    framing a character like "귀엽고 따뜻한 동생" was read as a description of the user ("귀여우시네요"). `title`: how to
    address the user (the user_title setting); without one, never a made-up 오빠/형/언니/누나."""
    text = text.strip().rstrip(".。 ")
    address = (f"사용자를 '{title}'(이)라고 부른다. 그 호칭은 한 답에 많아야 한 번, 자연스러운 곳에서만 쓰고, '네, {title}'로 답을 시작하는 버릇은 들이지 마. " if title else
               "사용자를 오빠, 형, 언니, 누나처럼 새로 부르지 말고 지금까지 부르던 대로 불러. ")
    return (f"너({name})의 성격과 말투: {text}. 이것은 너 자신의 성격이야. 사용자를 이 말로 묘사하거나 칭찬하지 말고, "
            "말투와 태도로만 드러내. 성격에 나오는 관계 말(동생, 친구, 선배, 비서 같은 것)은 너의 말투와 친밀도만 정해. "
            + address + "장난스러운 한마디는 답 하나에 한 번까지만 하고, 진지한 주제에는 하지 마. ")


def user_title(settings, persona):
    """The title the user set for this persona, else the shared one ("" = none). Up to 12 letters, no quotes."""
    for t in ((persona or {}).get("user_title"), settings.get("user_title")):
        t = re.sub(r"[\"'\\\x00-\x1f]", "", t).strip()[:12] if isinstance(t, str) else ""
        if t:
            return t
    return ""


def dueum(syl):
    """두음법칙: the other syllable a word may start with ('력' -> '역', '리' -> '이', '뉴' -> '유'), else None."""
    c = ord(syl) - 0xAC00
    if not 0 <= c < 11172:
        return None
    lead, vowel, tail = c // 588, c % 588 // 28, c % 28
    if lead == 5 and vowel in (2, 6, 7, 12, 17, 20):  # ㄹ + ㅑㅕㅖㅛㅠㅣ -> ㅇ
        lead = 11
    elif lead == 5 and vowel in (0, 1, 8, 11, 13, 18):  # ㄹ + ㅏㅐㅗㅚㅜㅡ -> ㄴ
        lead = 2
    elif lead == 2 and vowel in (6, 12, 17, 20):  # ㄴ + ㅕㅛㅠㅣ -> ㅇ
        lead = 11
    else:
        return None
    return chr(0xAC00 + lead * 588 + vowel * 28 + tail)


GAME_STOP = re.compile(r"그만|졌어|항복|끝내자|안 ?할래|이제 됐어|다른 거 하자")
# A game in progress, per history key. The server keeps what must not drift (the chain's last word and used words,
# the twenty-questions answer and count, the quiz's answer); Qwen only says it in the persona's voice.
games: dict[str, dict] = {}


async def qwen_line(system, max_tokens=80):
    """One short, non-conversational Qwen answer (a word, a pair): the game's move, chosen apart from the reply."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "출력해."}]
    return "".join([d async for d in unthink(llm_stream(msgs, max_tokens=max_tokens), Unthink())]).strip()


def chain_word(text):
    """The word a player said: the last Hangul run of 2+ syllables, without a polite ending ("과자요" -> "과자")."""
    words = re.findall(r"[가-힣]{2,}", text)
    return re.sub(r"(이에요|예요|이요|입니다|요)$", "", words[-1]) if words else ""


def chain_error(prev, word, used):
    if len(word) < 2:
        return "두 글자 이상 낱말이 아니에요"
    if word in used:
        return f"'{word}'는 이미 나왔어요"
    if prev and word[0] not in (prev[-1], dueum(prev[-1])):
        return f"'{prev[-1]}'(으)로 시작해야 해요"
    return None


async def chain_pick(prev, used):
    """The assistant's next word: Qwen proposes, the referee decides (3 tries); None means the assistant loses."""
    need, alt = (prev[-1], dueum(prev[-1])) if prev else ("", None)
    for _ in range(3):
        raw = await qwen_line("끝말잇기 낱말 고르기. " + (f"'{need}'" + (f" 또는 '{alt}'" if alt else "") + "로 시작하는 "
                              if need else "") + "두 글자 이상의 흔한 한국어 명사 하나만 출력해. 다른 말은 쓰지 마. "
                              + (f"쓰면 안 되는 낱말: {', '.join(used)}" if used else ""), max_tokens=20)
        words = re.findall(r"[가-힣]{2,}", raw)
        if words and not chain_error(prev, words[0], used):
            return words[0]
    return None


async def quiz_pick(done):
    """(question, answer) for a quick general-knowledge quiz; None when Qwen gives nothing usable."""
    raw = await qwen_line("짧은 상식 퀴즈 하나를 만들어. 답이 하나로 분명하고 네가 확실히 아는 사실만. 다음 문제와 겹치지 마: "
                          + ("; ".join(done) or "없음") + '. JSON 하나만: {"q": "문제", "a": "정답"}', max_tokens=160)
    try:
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        return (str(d["q"]).strip(), str(d["a"]).strip()) if d.get("q") and d.get("a") else None
    except (ValueError, KeyError):
        return None


GAME_TOOL = {"name": "game", "description": "끝말잇기(chain), 스무고개(twenty), 퀴즈(quiz)를 시작(start)하거나 끝낸다(stop). "
             "심심하다고 하면 quiz. 진행 중인 놀이의 차례는 서버가 심판하니, 놀이 중에는 부르지 말고 그만할 때만 stop.",
             "input_schema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["start", "stop"]},
                 "kind": {"type": "string", "enum": ["chain", "twenty", "quiz"]}}, "required": ["action"]}}
GAME_STOP_NOTE = "사용자가 그만하자고 했어. 즐거웠다고 짧게 마무리해. 문제나 낱말은 더 내지 마."


async def game_tool(hkey, args):
    """The game tool: Qwen starts or stops a game; the state it starts is kept here, per history key."""
    action, kind = args.get("action"), args.get("kind")
    if action == "stop":
        return GAME_STOP_NOTE if games.pop(hkey, None) else "진행 중인 놀이가 없어요."
    if action != "start" or kind not in ("chain", "twenty", "quiz"):
        return "action은 start 또는 stop, kind는 chain, twenty, quiz 중 하나예요."
    games.pop(hkey, None)
    if kind == "chain":
        first = await chain_pick("", [])
        if not first:
            return "지금은 끝말잇기를 시작하지 못했어요."
        games[hkey] = {"kind": "chain", "last": first, "used": [first]}
        return (f"[끝말잇기] 따라 말하지 말고 바로 시작해: 네 첫 낱말은 '{first}'야. 이 낱말을 작은따옴표로 감싸 말하고 "
                "사용자 차례라고 짧게 알려.")
    if kind == "twenty":
        secret = re.findall(r"[가-힣]{2,}", await qwen_line(
            "스무고개 정답으로 쓸 흔한 동물이나 사물 하나를 한 낱말로만 출력해.", max_tokens=20))
        if not secret:
            return "지금은 스무고개를 시작하지 못했어요."
        games[hkey] = {"kind": "twenty", "secret": secret[0], "asked": 0}
        return (f"[스무고개] 네가 정답을 '{secret[0]}'(으)로 정했어. 절대 말하지 마. '제가 하나 정했어요. 스무 번 안에 "
                "맞혀 보세요. 첫 질문 해 주세요.'처럼 시작해.")
    qa = await quiz_pick([])
    if not qa:
        return "지금은 퀴즈를 시작하지 못했어요."
    games[hkey] = {"kind": "quiz", "q": qa[0], "a": qa[1], "done": [qa[0]], "right": 0}
    return (f"[퀴즈] 사용자가 심심하다고 했으면 퀴즈로 놀자고 짧게 말하고, 이 문제를 그대로 내: \"{qa[0]}\" 그리고 "
            f"'정답은요?'로 물어. 정답('{qa[1]}')은 말하지 마. 되묻지 말고 이번에 바로 문제를 내.")


async def play_note(hkey, said):
    """A game in progress (started with the game tool): the referee's note for this turn's move, or ""."""
    game = games.get(hkey)
    if game is None:
        return ""
    if GAME_STOP.search(said):  # "그만" is the player quitting, never a chain word
        games.pop(hkey, None)
        return "\n[놀이] " + GAME_STOP_NOTE
    if game["kind"] == "chain":
        word = chain_word(said)
        if err := chain_error(game["last"], word, game["used"]):
            return (f"\n[끝말잇기] 사용자가 '{word}'라고 했는데 규칙에 안 맞아: {err}. 부드럽게 알려 주고 다시 해 보라고 "
                    "해. 네 낱말은 말하지 마.")
        game["used"].append(word)
        game["last"] = word
        mine = await chain_pick(word, game["used"])
        if not mine:
            games.pop(hkey, None)
            return f"\n[끝말잇기] 사용자의 '{word}' 좋아. 이어 갈 낱말이 없어서 네가 졌어. 졌다고 웃으며 인정하고 마무리해."
        game["used"].append(mine)
        game["last"] = mine
        return (f"\n[끝말잇기] 사용자의 '{word}' 좋아. 네 낱말은 '{mine}'야. 이 낱말만 작은따옴표로 감싸 말하고 사용자 "
                "차례라고 해. 다른 낱말이나 글자를 말하지 마.")
    if game["kind"] == "twenty":
        game["asked"] += 1
        if game["secret"] in said:
            games.pop(hkey, None)
            return f"\n[스무고개] 사용자가 정답 '{game['secret']}'을(를) {game['asked']}번 만에 맞혔어! 축하하고 끝내."
        if game["asked"] >= 20:
            games.pop(hkey, None)
            return f"\n[스무고개] 스무 번이 다 됐어. 정답이 '{game['secret']}'였다고 알려 주고 끝내."
        return (f"\n[스무고개] 정답은 '{game['secret']}'야(절대 말하지 마). 사용자의 질문에 이 정답 기준으로 사실대로 "
                f"'네' 또는 '아니요'로 답하고 한마디 덧붙여. 남은 질문은 {20 - game['asked']}개라고 알려 줘.")
    nxt = await quiz_pick(game["done"])  # quiz: grade this answer, then the next question
    note = (f"\n[퀴즈] 방금 문제 \"{game['q']}\"의 정답은 '{game['a']}'야. 사용자의 답 '{said}'이(가) 맞는지 판단해서 "
            "맞으면 칭찬하고 틀리면 정답을 알려 줘.")
    if not nxt:
        games.pop(hkey, None)
        return note + " 그리고 퀴즈를 마무리해."
    game.update(q=nxt[0], a=nxt[1], done=[*game["done"], nxt[0]])
    return note + f" 그다음 새 문제를 그대로 내: \"{nxt[0]}\" 정답('{nxt[1]}')은 말하지 마."


def no_handover(text):
    """Qwen's prompt without the hand-over tag's sentence."""
    return re.sub(r"[^.。]*" + re.escape(ROUTE_TAG) + r"[^.。]*[.。]\s*", "", text)


def local_prompt(text):
    """Qwen's prompt in local mode: no hand-over tag to emit (there is no Claude to hand over to)."""
    return no_handover(text) + " 날씨, 주가, 일정처럼 도구가 있는 건 꼭 도구로 확인하고, 도구로도 확인할 수 없는 사실만 모른다고 말해. 잡담과 네 이야기는 성격대로 편하게 해."


def system_prompt(name, length="normal", context="", persona="", title=""):
    """Qwen's system prompt for this turn, built from the shared settings (name, reply_length), so changes apply
    at once. `context`: the brain's block, appended last; with it, Qwen answers personal questions from the context
    or its tools (BRAIN_BODY) instead of always handing them to Claude. Stable parts come first, so the LLM
    endpoint's prefix cache covers them; callers append the clock and per-turn memory hits after this."""
    body = SYSTEM_BODY if not context else SYSTEM_BODY.split("중요:")[0] + BRAIN_BODY
    return f"너는 사용자의 개인 비서 '{name}'야. " + (character_line(name, persona, title) if persona else
                                                   f"사용자를 '{title}'(이)라고 부른다. " if title else "") + body + " " \
        + LENGTH_STYLE.get(length, LENGTH_STYLE["normal"]) + " " + CARDS_LINE \
        + (f"\n\n참고할 상황:\n{context}" if context else "")


def strip_vocative(text, name):
    """'아젤아, 서버 괜찮아?' -> '서버 괜찮아?': calling the assistant by name says nothing about the route."""
    return re.sub(rf"^\s*{re.escape(name)}\s*(아|야|씨|님)?[\s,!.~]*", "", text) or text
FILLER = "잠시만요, 확인해 볼게요."
FILLER_LONG = "조금만 더 기다려 주세요."
FILLER_AFTER_S = float(os.environ.get("FILLER_AFTER_MS", "1200")) / 1000  # say FILLER only if Claude is this slow
NUDGE_AFTER_S = 9.0  # then FILLER_LONG once, counted from the start of the Claude turn
# Whisper's classic outputs on silence or noise; never treat them as the user speaking.
HALLUCINATIONS = {"시청해 주셔서 감사합니다", "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다", "MBC 뉴스 이덕영입니다"}
# Whisper's sign-offs from its subtitle/news training data, heard in noise. Only a WHOLE utterance like this is dropped.
HALLUCINATION_RE = re.compile(
    r"((MBC|KBS|SBS|YTN|JTBC|TV조선|채널A|MBN|연합)\s*)?뉴스\s*\S{2,4}\s*(입니다|이었습니다|기자입니다)"
    r"|시청\s*해\s*주셔서\s*(정말\s*)?감사(합니다|드립니다)|구독\s*(과|와|,)?\s*좋아요(\s*(부탁\S*|눌러\s*\S*|알림\S*))*"
    r"|(구독|좋아요|알림\s*설정)\s*(부탁\S*|해\s*주세요)|다음\s*(영상|시간)에\s*(서\s*)?(만나요|뵙겠습니다)"
    r"|(한국어\s*)?자막\s*(제공|제작)\S*(\s*\S+)?|영상\s*편집\s*\S*"
    r"|지금까지\s*.{0,24}?(였|이었)습니다|(\S+\s*)?뉴스\s*스토리(였습니다|입니다)?"  # "지금까지 뉴스 스토리였습니다"
    r"|((MBC|KBS|SBS|YTN|JTBC|MBN|연합)\s*)+(뉴스)?", re.I)  # a broadcaster's name alone
STT_KWARGS = {"language": "korean", "task": "transcribe"}
# Whisper's own guards (transformers 4.57): no conditioning on earlier text, and a segment that is too repetitive
# (compression), too unsure (log-prob) or silent (no-speech) is re-decoded or dropped.
STT_QUALITY = {**STT_KWARGS, "condition_on_prev_tokens": False, "compression_ratio_threshold": 2.4,
               "logprob_threshold": -1.0, "no_speech_threshold": 0.6, "temperature": (0.0, 0.2, 0.4)}
stt_quality = [True]  # turned off once if this transformers build refuses the kwargs (STT must keep working)
MIN_STT_S = 0.3  # shorter audio is a click, not a word


def collapse_repeats(text: str) -> str:
    """'아니 아니 아니 아니' -> '아니'; '매우 매우 매우 좋아' -> '매우 좋아'; a word or a two-word phrase said 3+ times in a
    row counts once. Whisper loops like this on noise; people rarely say a word three times on purpose."""
    words = text.split()
    for n in (1, 2):
        out, i = [], 0
        while i < len(words):
            k = 1
            while words[i + k * n:i + (k + 1) * n] == words[i:i + n]:
                k += 1
            if k >= 3:
                out += words[i:i + n]
                i += k * n
            else:
                out.append(words[i])
                i += 1
        words = out
    return re.sub(r"(\S{1,4}?)\1{3,}", r"\1", " ".join(words))  # "아니아니아니아니" inside one token


def clean_transcript(text: str) -> str:
    """'' for noise: a subtitle/news sign-off as the whole utterance, or an utterance that is mostly a repeat loop."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    collapsed = collapse_repeats(text)
    before, after = len(text.split()), len(collapsed.split())
    if before >= 6 and after <= before * 0.4:  # "아니 아니 아니 아니 아니 아니 …": a loop, not speech
        return ""
    bare = collapsed.strip(" .!?~,")
    if bare in HALLUCINATIONS or HALLUCINATION_RE.fullmatch(bare):
        return ""
    return collapsed
# A model name counts only as a request to use it ("오퍼스로", "소넷한테"), not as a topic ("하이쿠가 뭐야").
_USE = r"\s*(으?로|한테|에게|모델)"
MODEL_WORDS = {"opus": rf"(오퍼스|opus){_USE}", "sonnet": rf"(소넷|sonnet){_USE}", "haiku": rf"(하이쿠|haiku){_USE}",
               "nim": rf"(엔비디아|nvidia|\bnim){_USE}"}  # NVIDIA NIM on a full gateway (gateway/nim.py)
USE_CLAUDE = re.compile(rf"(클로드|claude){_USE}|(클로드|claude)(가|이)\s*(해|답|풀|찾|봐|알아)", re.I)
# "클로드는 쓰지마", "클로드 말고 네가 해": this turn stays on the local model, whatever else it says.
CLAUDE_NO = re.compile(r"(클로드|claude)\s*(는|은|를|도|가|이)?\s*(쓰지|쓰면|부르지|호출하지|빼고|없이|말고|안 ?써|안 ?불러|필요 ?없)"
                       r"|(로컬|네가|니가|너가)\s*(로|만|혼자)?\s*(해|처리|답)", re.I)
# Personal data only full-mode gateways may touch (same list as the gateway's PERSONAL).
# "내일 내가 해야 될 일", "오늘 뭐 해야 돼?", "이번 주 계획 정리해 줘": the user's own plans, said without 일정/할 일
PLANS_ASK = (r"해야 ?(할|될|하는|되는) ?(일|거|것)|할 ?(거|것|게) ?(뭐|있|정리|알려)"
             r"|(오늘|내일|모레|글피|이번 ?주|다음 ?주|주말|월요일|화요일|수요일|목요일|금요일|토요일|일요일)"
             r".{0,10}(뭐 ?(해|하|있)|정리|계획|바빠|바쁘|일과|해야)")
PERSONAL = re.compile(r"일정(?!한)|캘린더|스케줄|회의|약속|메일|편지함|기억|메모리|연락처|전화번호|과제|수업|시험|성적|마감"
                      r"|할 ?일|브리핑|지스트라|GISTRA|플래닛|Planit|지난번|저번에|예전에 (내가|우리)|" + PLANS_ASK, re.I)
NO_PERSONAL = "개인 비서 기능은 지금 쓸 수 없어요."
TURN_FAILED = "잠깐 문제가 생겼어요. 다시 말씀해 주세요."  # an unexpected error in a turn: never the exception's own text
# "오늘 일정", "내일 뭐 있어", "이번 주 일정": the gateway's agenda tool (Google Calendar and Gistra merged) is read
# once before any model runs, so every path answers from both, not from whichever one tool the model picks.
AGENDA_ASK = re.compile(r"일정(?!한)|스케줄|캘린더|약속|브리핑|" + PLANS_ASK)
AGENDA_WAIT_S = 8


def agenda_range(text, now):
    """(from, to) dates of the schedule a question asks about, or None when it is not a schedule question. A named day
    reads from today through that day ("월요일 그러니까 내일 일정도" -> today and tomorrow); "다음 주까지" through next
    Sunday; "다음 주" alone is next Monday to Sunday; "이번 주" today to Sunday."""
    if not AGENDA_ASK.search(text):
        return None
    today = late_night.day_for(0, now)
    monday = today - timedelta(days=today.weekday())
    if re.search(r"(다음 ?주|담주)\s*(까지|안에|중에)", text):
        return today, monday + timedelta(days=13)
    ends = []
    for m in late_night.WEEKDAY_RE.finditer(text):
        wd = late_night.DAYS.index(m[2])
        ends.append(monday + timedelta(days=7 + wd) if m[1] and re.match(r"다음|담", m[1]) else
                    today + timedelta(days=(wd - today.weekday()) % 7))
    word, add = late_night.day_word(text)
    if word:
        ends.append(late_night.day_for(add, now))
    if ends:
        return today, max(max(ends), today)
    if re.search(r"다음 ?주|담주", text):
        return monday + timedelta(days=7), monday + timedelta(days=13)
    if re.search(r"이번 ?주", text):
        return today, monday + timedelta(days=6)
    if "주말" in text:
        return (today if today.weekday() == 6 else monday + timedelta(days=5)), monday + timedelta(days=6)
    return today, today


async def agenda_prefetch(text, gw_names, timings):
    """The merged schedule as a context block for this turn ('' when there is no gateway agenda or no question)."""
    span = agenda_range(text, datetime.now(KST)) if "agenda" in gw_names else None
    if not span:
        return ""
    try:
        got = await asyncio.wait_for(gateway_tool("agenda", {"from": span[0].isoformat(), "to": span[1].isoformat()}),
                                     AGENDA_WAIT_S)
    except (RuntimeError, asyncio.TimeoutError, httpx.HTTPError):
        timings["agenda"] = "failed"  # the model still has the agenda tool
        return ""
    timings["agenda"] = "ok"
    return f"\n\n[일정, 방금 확인: 구글 캘린더와 Gistra를 합친 것]\n{got.strip()}\n"
NO_GATEWAY = "지금 Claude 게이트웨이에 연결이 안 돼요."
TASK_WORDS = {"code": r"코드|레포|빌드|버그|커밋|테스트 ?돌|에러|함수 ?짜|스크립트", "mac": r"macbook|맥북|파일|폴더|터미널|바탕화면|다운로드",
              "schedule": r"일정(?!한)|할 ?일|과제|캘린더|회의|수업|마감|알람|브리핑|플래닛|planit|" + PLANS_ASK}

def load_hashes(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in LABELS}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        label, digest = line.split()
        assert label in LABELS and re.fullmatch(r"[0-9a-f]{64}", digest), f"bad token hash line for {label!r}"
        out[label].append(digest)
    assert out["client"] and out["link"], "token hash file needs client and link entries"
    return out


class RedactQuery(logging.Filter):
    """Strip query strings (which may carry ?token=) from uvicorn's log lines, whatever the log level."""
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in record.args)
        return True


def redact_query(text):
    return re.sub(r"\?[^\s\"']*", "?<redacted>", text)


for _name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).addFilter(RedactQuery())

CHECK_ONLY = "--check" in sys.argv
if not CHECK_ONLY:
    import torch
    from pysilero_vad import SileroVoiceActivityDetector
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model=STT_MODEL, torch_dtype=torch.float16, device="cuda:0")
    HASHES = load_hashes(open(HASH_FILE).read())
asr_lock = asyncio.Lock()
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))
# The conversation (persona:session) of the turn being answered: link requests that create a task or a reminder carry
# it, and the call that later reports back is seeded with that conversation's recent turns.
CALL_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("call_origin", default="")
# brain_mode for this call: auto | local (Qwen only, never Claude or a paid API) | claude_api | claude_code. From
# /v1/call?mode=, else the shared setting; the app can switch it mid-call. Link requests carry it to the gateway.
BRAIN_MODES = ("auto", "local", "claude_api", "claude_code")
CALL_MODE: contextvars.ContextVar = contextvars.ContextVar("call_mode", default="")
# The call's own state dict: its "mode" is what the app's badge shows right now. A turn copies CALL_MODE when it starts;
# the hand-off re-reads this, so a switch to local mid-turn still never reaches Claude (live 2026-11-23: 로컬 badge
# with "Claude에게 물어보는 중").
CALL_LIVE: contextvars.ContextVar = contextvars.ContextVar("call_live", default=None)
LOCAL_BRIEFING = "로컬 모드라서 지금은 브리핑을 준비하지 못했어요."


def local_now():
    """This call is in local mode right now (as the app's badge shows): nothing may reach Claude."""
    return "local" in (CALL_MODE.get(), (CALL_LIVE.get() or {}).get("mode"))
CALL_TURN: contextvars.ContextVar = contextvars.ContextVar("call_turn", default="")  # one id per user turn, for the gateway
CALL_CARDS: contextvars.ContextVar = contextvars.ContextVar("call_cards", default=None)  # (send, kinds sent) this turn
CALL_EVENTS: contextvars.ContextVar = contextvars.ContextVar("call_events", default=None)  # the call's on_event
CARD_MAX = 32_000  # bytes of JSON; a bigger card is dropped (cards-contract.md)
MODE_TYPES = ("ask", "tool_call", "schedule", "task")
LOCAL_LINE = "로컬 모드라서 그건 못 해요. Claude로 바꿀까요?"
ORIGIN_TYPES = ("tool_call", "schedule", "task")
ORIGIN_TURNS = 8
RESUME_RE = re.compile(r"[0-9a-f]{32}")


async def seed_from_call(key, call):
    """Continue a past call (the app's call list, "이어서 통화"): its turns from the call history become this call's
    history, so the model picks up where it left off even after a voice-server restart emptied memory."""
    if not RESUME_RE.fullmatch(call or ""):
        return 0
    try:
        reply = await links.request({"type": "history_turns", "call": call}, timeout=3)
    except (RuntimeError, asyncio.TimeoutError):
        return 0
    turns = [t for t in reply.get("turns") or [] if isinstance(t, dict)][-ORIGIN_TURNS:]
    for t in turns:
        if str(t.get("user") or "").strip():
            history[key].append({"role": "user", "content": str(t["user"])[:1000]})
        if str(t.get("assistant") or "").strip():
            history[key].append({"role": "assistant", "content": str(t["assistant"])[:1500]})
    return len(turns)
ORIGIN_RE = re.compile(r"[A-Za-z0-9_:.-]{1,120}")
# Who each history was built as (persona name, user title). The app reuses one session id all day, so without this a
# renamed persona or a new title would keep being contradicted by the earlier turns Qwen sees and imitates.
history_sig: dict[str, tuple] = {}
client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5))


class Link:
    """One gateway connection. Gateways dial in here (Spark may not open connections to jh hosts, and
    personal Claude credentials stay off this shared team machine)."""

    def __init__(self, ws, limited=False):
        self.ws, self.lock, self.locked = ws, asyncio.Lock(), limited  # locked: a link_limited token
        self.device, self.priority = "?", 50  # until hello (older gateways never send one)
        self.mode = "limited" if limited else "full"
        self.fails = 0  # consecutive failed asks; 3 or more = unhealthy, tried last
        self.claude = True  # False: a gateway without Claude (scheduled calls, history, settings only)
        self.brain = False  # the resident brain: sends contexts, serves Qwen's tools, never takes asks or logs
        self.tools: tuple = ()  # the tools a brain link serves (its hello), e.g. read-only Cygnus tools

    async def send(self, obj):
        if obj.get("type") in ORIGIN_TYPES and (origin := CALL_ORIGIN.get()) and "origin_session" not in obj:
            obj = {**obj, "origin_session": origin}  # the conversation a task or reminder will continue
        if obj.get("type") in MODE_TYPES and (mode := CALL_MODE.get()) and "mode" not in obj:
            obj = {**obj, "mode": mode}  # the gateway answers and runs tasks in this call's mode
        async with self.lock:
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))


class GatewayLinks:
    """Every connected gateway at once. Asks go to the healthiest full-mode link by priority (lower first);
    with no full link, limited links take only non-personal asks. Settings and call openings may come
    from any link (the gateways share state)."""

    def __init__(self):
        self.links: list[Link] = []
        self.queues: dict[str, tuple[asyncio.Queue, Link]] = {}
        self.settings: dict = {}
        self.openings: dict[str, str] = {}
        self.persona_ctx: dict[str, tuple[float, str]] = {}  # (received at, block) per persona ("" = the primary)
        self.session_ctx: dict[str, tuple[float, str]] = {}  # per call session (e.g. why the assistant called)

    def add(self, ws, limited=False):
        lk = Link(ws, limited)
        self.links.append(lk)
        return lk

    def remove(self, lk):
        if lk in self.links:
            self.links.remove(lk)
        for q, owner in self.queues.values():
            if owner is lk:
                q.put_nowait({"type": "error", "message": "게이트웨이 연결이 끊겼어요."})

    async def log(self, event):
        """A call_log event for the call history. Only to a full-mode link (history is personal); dropped when
        none is up. This shared machine never keeps history itself."""
        await self.notify({"type": "call_log", **event})

    async def notify(self, msg):
        """A one-way message for a full-mode link (any, Claude or not); dropped when none is up."""
        try:
            await self.pick(personal=True, need_claude=False).send(msg)
        except Exception as e:
            print(msg.get("type"), "dropped:", msg.get("event", ""), type(e).__name__, flush=True)

    async def request(self, msg, timeout=10, need_claude=False):
        """Ask a full-mode link and wait for its one reply (e.g. a schedule request)."""
        lk, rid, q = self.pick(personal=True, need_claude=need_claude), uuid.uuid4().hex, asyncio.Queue()
        self.queues[rid] = (q, lk)
        try:
            await lk.send({**msg, "id": rid})
            return await asyncio.wait_for(q.get(), timeout)
        finally:
            self.queues.pop(rid, None)

    def pick(self, personal: bool, need_claude: bool = True) -> Link:
        """The link for one ask; RuntimeError carries a speakable reason when none may take it."""
        ranked = sorted((lk for lk in self.links if (lk.claude or not need_claude) and not lk.brain),
                        key=lambda lk: (lk.fails >= 3, lk.priority))
        full = [lk for lk in ranked if lk.mode == "full"]
        if full:
            return full[0]
        if personal:
            raise RuntimeError(NO_PERSONAL)
        limited = [lk for lk in ranked if lk.mode == "limited"]
        if limited:
            return limited[0]
        raise RuntimeError(NO_GATEWAY)

    def receive(self, lk, msg):
        """One frame from a gateway. Malformed frames are ignored, never allowed to drop the link."""
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        if t == "hello":
            device = str(msg.get("device", "?"))
            mode = "limited" if lk.locked or msg.get("mode") == "limited" else "full"  # limited tokens stay limited
            same = [o for o in self.links if o is not lk and o.device == device]
            if any(o.mode != mode for o in same):
                return  # never let a link of the other mode evict (or pose as) that device's link
            lk.device, lk.mode = device, mode
            lk.claude = msg.get("claude") is not False
            lk.brain = msg.get("role") == "brain" and mode == "full"
            tools = msg.get("tools") if lk.brain and isinstance(msg.get("tools"), list) else []
            lk.tools = tuple(t for t in tools[:40] if isinstance(t, str) and TOOL_NAME.fullmatch(t))
            try:
                lk.priority = int(msg.get("priority", 50))
            except (TypeError, ValueError):
                lk.priority = 50
            for old in same:
                self.remove(old)  # that gateway reconnected; its old socket is dead
        elif t == "context":
            if lk.brain and isinstance(msg.get("value"), str):
                self.take_context(msg.get("session"), msg["value"], msg.get("persona"))
        elif t in ("settings", "call", "announce", "auto_state", "phone_query", "end_call") and lk.mode != "full":
            return  # shared settings, call openings, announcements and phone data: full gateways only
        elif t == "phone_query":
            PHONE_TASKS.add(task := asyncio.create_task(phone_query(lk, msg)))
            task.add_done_callback(PHONE_TASKS.discard)
        elif t == "settings":
            self.settings = msg.get("value") if isinstance(msg.get("value"), dict) else {}
        elif t == "call":
            if not isinstance(msg.get("call_id"), str) or not msg["call_id"] \
                    or not isinstance(msg.get("opening", ""), str):
                return  # malformed: ignore it rather than drop the link
            if len(self.openings) > 100:
                old = self.openings.pop(next(iter(self.openings)))
                if old["audio"]:
                    old["audio"].cancel()
            self.openings[msg["call_id"]] = prepare_opening(msg.get("opening", ""), str(msg.get("reason", "")),
                                                            str(msg.get("persona") or ""))
            origin = msg.get("origin_session")
            self.openings[msg["call_id"]]["origin"] = origin if isinstance(origin, str) and ORIGIN_RE.fullmatch(origin) else ""
            self.openings[msg["call_id"]]["detail"] = str(msg.get("detail") or "")[:4000]
            ctx = msg.get("context")
            self.openings[msg["call_id"]]["context"] = {k: str(ctx.get(k) or "")[:60] for k in ("kind", "label", "id")} \
                if isinstance(ctx, dict) and ctx.get("kind") in ("task", "schedule") else None
        elif t == "end_call":  # the assistant hangs up (the user said "끊어"): after its goodbye has played
            if hook := end_hooks.get(msg.get("session")):
                asyncio.get_running_loop().create_task(hook(msg))
        elif t == "auto_state":  # the gateway started or ended the call's auto-approve grant
            if hook := call_hooks.get(msg.get("session")):
                asyncio.get_running_loop().create_task(hook(msg))
        elif t == "announce":  # a task finished while its user is on this call: say it here, not in a new call
            q = active_calls.get(msg.get("session")) if isinstance(msg.get("opening"), str) and msg["opening"] else None
            if q is not None:
                q.put_nowait(msg)
            asyncio.get_running_loop().create_task(lk.send({"type": "announce_result", "id": msg.get("id"),
                                                            "ok": q is not None}))
        elif isinstance(msg.get("id"), str) and isinstance(t, str) and msg["id"] in self.queues:
            self.queues[msg["id"]][0].put_nowait(msg)


links = GatewayLinks()
active_calls: dict[str, asyncio.Queue] = {}  # "<persona>:<session>" of each live call -> its announcements
call_hooks: dict = {}  # "<persona>:<session>" -> the call's handler for the gateway's auto_state frames
end_hooks: dict = {}  # "<persona>:<session>" -> the call's handler for the gateway's end_call frames
FALLBACK_OPENING = "전화드렸어요. 맡기신 일 때문에 연락드렸는데, 무엇을 도와드릴까요?"
DETAIL_NOTE = "(맡기신 작업의 전체 결과예요. 전화로는 요약만 말씀드렸어요.)"


# ---------- iPhone data relay (PHONE_DATA.md §3a): gateway <-> the phone in a call; nothing is stored ----------
PHONE_KINDS = {"health", "sleep", "workouts", "calendar", "reminders", "contacts", "location", "now_playing"}
phones: list = []  # [{"send", "kinds"}], the latest call last; a secret call says no kinds
phone_asks: dict = {}  # query id -> the link that asked
PHONE_TASKS: set = set()


async def phone_query(lk, msg):
    qid, kind = msg.get("id"), msg.get("kind")
    peer = next((p for p in reversed(phones) if kind in p["kinds"]), None)
    if not isinstance(qid, str) or peer is None:
        await lk.send({"id": qid, "type": "phone_result", "ok": False, "error": "offline"})
        return
    if len(phone_asks) > 50:
        phone_asks.pop(next(iter(phone_asks)))
    phone_asks[qid] = lk
    await peer["send"]({"type": "phone_query", "id": qid, "kind": kind,
                        "args": msg.get("args") if isinstance(msg.get("args"), dict) else {}})


async def phone_result(msg):
    if (lk := phone_asks.pop(msg.get("id"), None)) is not None:
        await lk.send({k: msg[k] for k in ("id", "ok", "data", "error") if k in msg} | {"type": "phone_result"})


# ---------- brain: context and local tools for Qwen (PROTOCOL.md section 6) ----------
BRAIN_FRESH_S = 900  # an older context block is ignored (the brain pushes one at least every 5 min while it changes)
RECALL_WAIT_S = 0.4  # per-turn memory retrieval may delay the answer this much at most
TOOL_WAIT_S = 3.0  # a slow brain never stalls a turn
TOOL_TAG = "[TOOL]"
BRAIN_TOOLS = ("search_memory", "add_todo", "done_todo", "device_status", "situation")  # a brain without a tool list
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Live data a web search answers (weather, news, prices).
WEB_LIVE = re.compile(r"날씨|비 ?와|눈 ?와|뉴스|환율|주가|시세|가격|코인|비트코인|이더리움|주식|증시|코스피|나스닥|검색|찾아(서|줘|봐)|맛집")


def _take_context(self, session, value, persona=None):
    value = value[:6000]
    if isinstance(session, str) and session:
        if len(self.session_ctx) > 200:
            self.session_ctx.pop(next(iter(self.session_ctx)))
        self.session_ctx[session] = (time.time(), value)
    else:  # a persona's shared block; frames without `persona` are the primary's (as before personas)
        self.persona_ctx[persona if isinstance(persona, str) and persona else ""] = (time.time(), value)


def _context_for(self, session, persona=None):
    """The freshest block for this call ('' when no brain is up): the session's own, else the persona's."""
    if not self.brain_link():
        return ""
    pid = persona or (CALL_PERSONA.get() or {}).get("id") or ""
    shared = self.persona_ctx.get(pid) or (self.persona_ctx.get("") if (CALL_PERSONA.get() or {}).get("primary", True)
                                           else None)
    got = [c for c in (self.session_ctx.get(session), shared) if c and time.time() - c[0] < BRAIN_FRESH_S]
    return max(got)[1] if got else ""


def _brain_tools(self):
    """What Qwen may call through the brain: the tools its hello listed (the brain re-checks each call)."""
    lk = self.brain_link()
    return set(lk.tools or BRAIN_TOOLS) if lk else set()


def _brain_link(self):
    return next((lk for lk in reversed(self.links) if lk.brain), None)


async def _brain_request(self, msg, timeout):
    """One request to the brain and its one reply; RuntimeError when no brain is up."""
    lk = self.brain_link()
    if lk is None:
        raise RuntimeError("no brain")
    rid, q = uuid.uuid4().hex, asyncio.Queue()
    self.queues[rid] = (q, lk)
    try:
        await lk.send({**msg, "id": rid})
        return await asyncio.wait_for(q.get(), timeout)
    finally:
        self.queues.pop(rid, None)


async def _brain_event(self, msg):
    if lk := self.brain_link():
        try:
            await lk.send(msg)
        except Exception:
            pass


GatewayLinks.take_context, GatewayLinks.context_for, GatewayLinks.brain_link = _take_context, _context_for, _brain_link
GatewayLinks.brain_tools = _brain_tools
GatewayLinks.brain_request, GatewayLinks.brain_event = _brain_request, _brain_event

BRAIN_BODY = (
    "아래 '참고할 상황'은 사용자 프로필, 지금 상황(일정, 마감, 할 일, 예약된 전화, 기기), 최근 통화 요약이야. "
    "여기에 답이 있으면 Claude 없이 네가 바로 답해. 없는 내용은 절대 지어내지 마. "
    "상황의 일정은 줄마다 붙은 확인 시각 기준이야. 여섯 시간보다 오래된 일정은 '몇 시 기준으로는'이라고 밝히고 말해. "
    f"더 찾아봐야 하면 다른 말 없이 한 줄로 {TOOL_TAG} {{\"name\": 도구, \"args\": {{…}}}} 만 출력해. 도구: "
    "search_memory(query) 예전 통화와 메모 검색, add_todo(text) 할 일 메모, done_todo(text) 메모한 할 일 지우기, "
    "device_status() 기기 연결 상태, situation() 최신 상황, scheduled_calls() 예약된 전화 목록. "
    "참고할 상황에 'Cygnus 도구' 목록이 있으면 일정, 과제, 할 일, 서버 상태는 그 도구로 확인해서 답해. "
    "일정이나 브리핑을 물으면 '알려 드릴게요' 같은 예고 없이 바로 내용부터 말해. "
    "일정 브리핑, 일정 확인, 날씨, 주가, 검색, 서버나 기기 작업도 가진 도구(agenda, weather, stock_quote, web_search, 기기 도구)로 "
    "먼저 직접 해. 이것들은 Claude에 넘기지 마. "
    f"참고할 상황과 도구로 해 보고도 정말 안 되는 일(일정 정보도 일정 도구도 없을 때, 메일 내용, 일정 등록이나 수정, 코드 짜기, "
    f"긴 여러 단계 작업)일 때만 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 상식, 계산, 방금 대화 정리는 바로 답해. " + AGENCY
)


def memory_hits(hits):
    return ("\n[이번 말과 관련된 기록]\n" + "\n".join(f"- {h}" for h in hits)) if hits else ""


# ---------- the gateway's tools (devices, call_user, create_task): gateway/tools_bridge.py over the link ----------
GW_TOOLS_TTL_S = 300
GW_TOOL_WAIT_S = 330  # an approval may take the user up to 5 minutes
gw_tools: dict = {"at": 0.0, "defs": []}


GW_TOOLS_FIRST_S = 3.0  # with no list at all, a turn waits this long for one


async def gateway_tools():
    """The gateway's tool list from the cache: a stale list is refreshed in the background, but with none at all the
    turn waits for it (live 2026-11-23 03:13: the first turn after a restart ran with no tools, so "서버 확인해 볼게요"
    could neither call nor be held, and nothing followed)."""
    if not gw_tools["defs"] and time.time() - gw_tools.get("first_try", 0) > 30:  # no gateway: one wait per 30 s
        gw_tools["first_try"] = time.time()
        try:
            await asyncio.wait_for(_refresh_gateway_tools(), GW_TOOLS_FIRST_S)
        except asyncio.TimeoutError:
            pass
    if time.time() - gw_tools["at"] > GW_TOOLS_TTL_S and time.time() - gw_tools.get("refreshing", 0) > 10:
        gw_tools["refreshing"] = time.time()  # a refresh that never finished (cancelled) blocks nothing past 10 s
        asyncio.get_running_loop().create_task(_refresh_gateway_tools())
    return gw_tools["defs"]


async def _refresh_gateway_tools():
    try:
        reply = await links.request({"type": "tools_list"}, timeout=3)
        defs = reply.get("tools") if isinstance(reply.get("tools"), list) else []
        gw_tools.update(at=time.time(), defs=[t for t in defs if isinstance(t, dict) and isinstance(t.get("name"), str)])
    except (RuntimeError, asyncio.TimeoutError):
        gw_tools["at"] = time.time() - GW_TOOLS_TTL_S + 30  # try again in 30 s
    finally:
        gw_tools["refreshing"] = 0


DEVICE_TOOL_KO = {"list_dir": "폴더 보기", "read_file": "파일 읽기", "search": "파일 내용 찾기",
                  "run_command": "명령 실행(df -h 같은 조회도, 위험하면 앱 승인)", "job_status": "맡긴 작업 상태",
                  "list_jobs": "맡긴 작업 목록", "browse": "웹 페이지 열기", "delegate": "여러 단계 작업 맡기기"}


# ---------- which tools fit the user's words (live 2026-11-22: "일주일 동안 내가 얼마나 잤는지 수면시간 확인해줘" got
# "불러올게요" three times and no call, with phone_sleep 1 of 115 tools) ----------
GRAM_STOP = {"해줘", "알려", "려줘", "확인", "인해", "줘요", "어요", "해요", "하는", "있어", "에서", "으로", "이야", "는지", "얼마",
             "마나"}
TOOL_MATCH_MIN = 6.0  # a request this close to one tool's description is that tool's job (small talk stays under ~3)
SHORTLIST = 8  # tools described in full each turn, by relevance; the rest by name (any of them may still be called)
CORE_TOOLS = ("call_user", "create_task", "memory_recall", "web_search", "weather", "agenda", "stock_quote", "game")
_tool_index: dict = {"key": None}


def grams(text):
    """Character bigrams of Korean words (particles and endings barely matter), whole Latin words and their parts."""
    t = re.sub(r"[^가-힣A-Za-z0-9]+", " ", str(text).lower())
    out = set()
    for w in t.split():
        if not re.search(r"[가-힣]", w):
            out |= {w} | set(w.split("_"))
        elif len(w) == 1:
            out.add(w)
        else:
            out |= {w[i:i + 2] for i in range(len(w) - 1)}
    return out - GRAM_STOP


def tool_doc(t):
    props = (t.get("input_schema") or {}).get("properties") or {}
    return grams(t["name"].replace("_", " ") + " " + str(t.get("description", "")) + " " + " ".join(
        f"{p.get('description', '')} {' '.join(map(str, p.get('enum', [])))}" for p in props.values() if isinstance(p, dict)))


def rank_tools(text, defs):
    """[(score, tool)] for the assistant's own tools, best first: bigrams shared with each tool's name and
    descriptions, weighted by how rare they are among the tools (idf), so "수면" counts and "확인" hardly."""
    own = [t for t in defs if "__" not in t["name"]]
    key = tuple(t["name"] for t in own)
    if _tool_index["key"] != key:
        docs = {t["name"]: tool_doc(t) for t in own}
        df: dict = {}
        for g in docs.values():
            for x in g:
                df[x] = df.get(x, 0) + 1
        # shortcut: at least 30 tools for the weights, so TOOL_MATCH_MIN means the same with a short test list; tune both
        # if the live list changes a lot (it has ~44 own tools)
        _tool_index.update(key=key, docs=docs, df=df, n=max(len(docs), 30))
    q, docs, df, n = grams(text), _tool_index["docs"], _tool_index["df"], _tool_index["n"]
    return sorted(((sum(math.log((n + 1) / (df[x] + 1)) for x in q & docs[t["name"]]), t) for t in own),
                  key=lambda st: -st[0])


def matched_tool(text, defs):
    """The one tool a request plainly asks for, or None."""
    ranked = rank_tools(text, defs) if text else []
    return ranked[0][1] if ranked and ranked[0][0] >= TOOL_MATCH_MIN else None


def gateway_tools_text(defs, text=""):
    """Every tool Qwen may call, in its prompt. Device tools are one line per kind (13 devices × 7 tools listed one by
    one used up the old 40-line cap, so call_user, memory, web and the Cygnus tools never reached Qwen). Own tools:
    the ones that fit the user's words and the core ones in full (no 160-character cut: four lost their ends), the
    rest by name only."""
    if not defs:
        return ""
    devices: dict[str, list] = {}
    own = []
    for t in defs:
        if "__" in t["name"]:
            dev, tool = t["name"].split("__", 1)
            devices.setdefault(tool, []).append(dev)
        else:
            own.append(t)
    fit = [t["name"] for score, t in rank_tools(text, defs)[:SHORTLIST] if score > 0] if text else []
    full = [t for t in own if t["name"] in fit or t["name"] in CORE_TOOLS or len(own) <= SHORTLIST + len(CORE_TOOLS)]
    full.sort(key=lambda t: fit.index(t["name"]) if t["name"] in fit else len(fit))
    lines = [f"- {t['name']}{arg_hint(t)}: {str(t.get('description', ''))}" for t in full]
    rest = [t["name"] for t in own if t not in full]
    if rest:
        lines.append("- 그 밖의 도구(이름만, 필요하면 불러도 돼): " + ", ".join(rest))
    if devices:
        lines.append("- 기기 도구, 이름은 '기기__도구'(예: spark__run_command, macbook__list_dir): " + "; ".join(
            f"{tool}={DEVICE_TOOL_KO.get(tool, tool)} [{', '.join(ds)}]" for tool, ds in devices.items()))
    names = {t["name"] for t in own}
    return ("\n\n[도구] 쓰려면 다른 말 없이 한 줄로 정확히 " + TOOL_TAG + ' {"name": "도구 이름", "args": {…}} 만 출력해. '
            "결과를 받기 전에는 확인했다거나 결과를 말하지 마. 목록:\n" + "\n".join(lines) + "\n사용자에게 전화를 걸거나 예약하려면 call_user, 여러 단계 "
            "일은 create_task로 맡겨(끝나고 전화를 원하면 notify call). 기기에서 무언가 실행하는 도구는 사용자가 앱에서 "
            "허용해야 실행돼. 전화를 걸 수 없다고 말하지 마."
            + ("\n서버, 인프라, 노드, 홈서버, 내부 시스템 상태(켜짐, CPU, 메모리, 디스크, 온도, 컨테이너)는 infra_health로, 문제 "
               "있는 서비스만은 infra_status, 울리는 경보는 infra_alerts로 확인해. 특정 기기의 디스크나 파일은 그 기기의 "
               "run_command(예: df -h)나 list_dir로." if "infra_health" in names else "")
            + "\n'도구가 없다'고 말하기 전에 위 목록을 다시 봐. 딱 맞는 게 없으면 가장 가까운 읽기 도구를 먼저 불러. 음성 인식이 "
            "틀린 말('대내부 상태'처럼)도 뜻으로 알아들어."
            + ("\n사용자, 주변 사람, 지난 일, 취향, 계획을 물으면 '모르겠어요' 하기 전에 memory_recall이나 people_lookup을 먼저 "
               "불러. 결과에 없는 사실은 지어내지 마. '기억해', '잊지 마'라고 하면 remember로 저장하고 결과 문장을 그대로 말해."
               if "memory_recall" in names else "")
            + ("\n날씨는 weather로(장소를 말하지 않으면 place는 비워)." if "weather" in names else "")
            + ("\n주가와 환율은 stock_quote로(query는 회사 영어 이름이나 티커, 환율이면 '달러 환율')." if "stock_quote" in names else "")
            + ("\n뉴스, 가격처럼 지금 정보는 web_search로 찾아. 출처는 '○○ 기준으로'처럼 결과에 나온 사이트 이름만 말하고 주소는 읽지 마."
               if "web_search" in names else "")
            + "\n'찾아볼게요'라고만 하고 끝내지 마: 찾을 거면 바로 도구를 불러.")


def arg_hint(t):
    props = (t.get("input_schema") or {}).get("properties") or {}
    return "(" + ", ".join(props) + ")" if props else ""


TOOL_TEXT_MAX = 2000
TOOL_TEXT_MAX_OF = {"agenda": 6000}  # a month of days: cut at 2000, the late days read as "no events"


def clip_tool(text, name=""):
    """A tool result as a model reads it: whole lines only, and a cut is said, never silent."""
    cap = TOOL_TEXT_MAX_OF.get(name, TOOL_TEXT_MAX)
    return text if len(text) <= cap else text[:cap].rsplit("\n", 1)[0] + "\n(이후 생략)"


async def relay_card(card):
    """A gateway tool's card (built from the tool's own data, never model text) to the call, once per kind a turn."""
    sink = CALL_CARDS.get()
    if not sink or not isinstance(card, dict) or not isinstance(card.get("title"), str) \
            or not isinstance(kind := card.get("kind"), str) or not 0 < len(kind) <= 32 or kind in (sink[1] or ()):
        return
    if len(json.dumps(card, ensure_ascii=False).encode()) > CARD_MAX:
        return
    if sink[1] is not None:  # None: a holding sink (infra_read) that merges, so nothing is deduplicated yet
        sink[1].add(kind)
    await sink[0]({"type": "card", "turn": CALL_TURN.get(), "card": card})


async def gateway_tool(name, args, on_say=None):
    """One gateway tool over the link, as the text a model reads."""
    msg = await gateway_tool_msg(name, args, on_say)
    return clip_tool(str(msg.get("result") or msg.get("message") or ""), name)


async def gateway_tool_msg(name, args, on_say=None, on_output=None):
    """One gateway tool over the link: the whole tool_result frame (result for a model; spoken and needs for the
    fast path, ACTIONS.md §2). A tool_progress frame ("앱에서 허용해 주세요") is spoken as it arrives."""
    lk = links.pick(personal=True, need_claude=False)
    rid, q = uuid.uuid4().hex, asyncio.Queue()
    links.queues[rid] = (q, lk)
    try:
        await lk.send({"type": "tool_call", "id": rid, "name": name, "args": args,
                       "persona": (CALL_PERSONA.get() or {}).get("id"),
                       **({"turn": CALL_TURN.get()} if CALL_TURN.get() else {})})  # a confirm never counts in its own turn
        end = time.monotonic() + GW_TOOL_WAIT_S
        while True:
            msg = await asyncio.wait_for(q.get(), max(0.1, end - time.monotonic()))
            if msg.get("type") == "tool_progress" and on_say and isinstance(msg.get("say"), str):
                await on_say(msg["say"][:120])
            if msg.get("type") == "tool_progress" and on_output and isinstance(msg.get("output"), str):
                await on_output(msg["output"])  # a command's streamed output, for the app's tool detail
            elif msg.get("type") in ("tool_result", "error"):
                await relay_card(msg.get("card"))  # as soon as the data is here, before any sentence about it
                return msg
    finally:
        links.queues.pop(rid, None)


async def _run_tool(raw, session, on_say=None, meta=None, on_output=None):
    """Run one '[TOOL] {...}' line from Qwen: brain tools over the brain link, the gateway's tools (devices,
    call_user, create_task) over a gateway link, scheduled calls via a gateway."""
    call = raw.strip().removeprefix(TOOL_TAG).strip()
    try:
        data = json.loads(call[call.index("{"):call.rindex("}") + 1])
        name, args = str(data.get("name", "")), data.get("args") if isinstance(data.get("args"), dict) else {}
    except ValueError:
        return "도구 호출 형식이 틀렸어요."
    if name == "scheduled_calls":
        return await schedule_reply("", "list", session)
    if name == GAME_TOOL["name"]:  # served here: the game's state lives with the conversation
        return await game_tool(CALL_ORIGIN.get() or session, args)
    if name in {t["name"] for t in gw_tools["defs"]} and name not in links.brain_tools():
        try:
            msg = await gateway_tool_msg(name, args, on_say, on_output)
        except (RuntimeError, asyncio.TimeoutError):
            return "지금은 그 도구를 쓸 수 없어요."
        if meta is not None:
            meta["msg"] = msg
        needs = msg.get("needs") if isinstance(msg.get("needs"), dict) else None
        if needs and needs.get("kind") == "confirm" and isinstance(needs.get("args"), dict):
            pending_confirm[session] = (time.time(), name, needs["args"])  # a read-back: the next turn's yes resends it
        return clip_tool(str(msg.get("result") or msg.get("message") or ""), name)
    if name not in links.brain_tools():
        return f"없는 도구예요: {name[:40]}"
    try:
        reply = await links.brain_request({"type": "tool", "name": name, "args": args,
                                           "persona": (CALL_PERSONA.get() or {}).get("id")}, TOOL_WAIT_S)
        return clip_tool(str(reply.get("result", "")), name)
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 그 도구를 쓸 수 없어요."


TOOL_FAILED = ("도구 호출 형식이 틀렸어요.", "지금은 그 도구를 쓸 수 없어요.", "없는 도구예요")
COMMAND_ARGS = ("command", "cmd", "script")  # a command tool's command line: shown as is, not as JSON
OUTPUT_EVERY_S = 0.5  # tool_output chunks: at most two a second per call


async def run_tool(raw, session, on_say=None):
    """_run_tool, shown to the app (tool-activity-contract.md): tool_start with the input, tool_output chunks while a
    command streams, tool_end with the output, exit code and duration. Everything redacted and clipped."""
    emit = CALL_EVENTS.get()
    call = raw.strip().removeprefix(TOOL_TAG).strip()
    try:
        data = json.loads(call[call.index("{"):call.rindex("}") + 1])
        name, args = str(data.get("name", "")), data.get("args") if isinstance(data.get("args"), dict) else {}
    except ValueError:
        emit = None  # not a tool call: nothing to show
    if not emit:
        return await _run_tool(raw, session, on_say)
    cid, t0, meta, label_kind = uuid.uuid4().hex[:12], time.perf_counter(), {}, tool_activity(name)
    cmd = next((args[k] for k in COMMAND_ARGS if isinstance(args.get(k), str)), None)
    host = name.split("__", 1)[0] if "__" in name else next((str(args[k]) for k in ("host", "device") if args.get(k)), "")
    base = {"type": "activity", "call_id": cid, "tool": name, "kind": label_kind[1], "route": "qwen"}
    await emit({**base, "phase": "tool_start", "label": label_kind[0], **({"host": host[:64]} if host else {}),
                "input": clip_head(redact(cmd if cmd is not None else json.dumps(args, ensure_ascii=False, separators=(",", ":"))))})
    pending, last = [""], [0.0]

    async def on_output(chunk):  # shortcut: a secret split across two chunks may escape redaction; the gateway redacts first
        pending[0] += chunk
        if time.monotonic() - last[0] >= OUTPUT_EVERY_S:
            last[0] = time.monotonic()
            text, pending[0] = pending[0], ""
            await emit({**base, "phase": "tool_output", "chunk": clip_tail(redact(text), 2048)[0]})
    text, ok = "", False
    try:
        text = await _run_tool(raw, session, on_say, meta, on_output)
        msg = meta.get("msg") or {}
        ok = not msg.get("is_error") and not text.startswith(TOOL_FAILED)
        return text
    finally:  # also when the lookup's wait cancels it: the app never keeps a spinner for a call that ended
        msg = meta.get("msg") or {}
        out, cut = clip_tail(redact(msg["output"] if isinstance(msg.get("output"), str) else text), 8192)
        extra = {k: v for k, v in (("exit_code", msg.get("exit_code")), ("task_id", msg.get("task_id")),
                                  ("host", msg.get("host"))) if v is not None}
        end = {**base, "phase": "tool_end", "label": label_kind[0], "ok": ok,
               "duration_ms": round((time.perf_counter() - t0) * 1000), "output": out,
               **({"truncated": True} if cut else {}),
               **({"exit_code": extra["exit_code"]} if isinstance(extra.get("exit_code"), int) else {}),
               **({"task_id": str(extra["task_id"])[:64]} if extra.get("task_id") else {}),
               **({"host": str(extra["host"])[:64]} if extra.get("host") else {})}
        try:
            if pending[0]:  # what streamed after the last chunk: the app appends chunks, so nothing may be left out
                await emit({**base, "phase": "tool_output", "chunk": clip_tail(redact(pending[0]), 2048)[0]})
            await emit(end)
        except Exception:  # noqa: BLE001  the call may be gone; the tool's own result still returns
            pass


# Secrets in a tool's input or output, before the app shows them (the gateway redacts at the source too)
REDACT = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s\"',;]+"), r"\1«redacted»"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer «redacted»"),
    (re.compile(r"(?i)\b([\w-]*(?:token|secret|passwd|password|api[_-]?key|apikey|access[_-]?key|private[_-]?key|\bkey)[\w-]*)"
                r"(\s*[:=]\s*\"?|\"\s*:\s*\")([^\s\"',;&]+)"), r"\1\2«redacted»"),
    (re.compile(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]+"), r"\1?«redacted»"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), "«redacted»"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "«redacted»"),
]


def redact(text):
    for pat, rep_ in REDACT:
        text = pat.sub(rep_, text)
    return text


def clip_head(text, n=2048):
    """At most n bytes of UTF-8, from the start ("…" when cut)."""
    b = text.encode()
    return text if len(b) <= n else b[:n - 3].decode(errors="ignore") + "…"


def clip_tail(text, n=8192):
    """(the last n bytes of UTF-8, cut?)."""
    b = text.encode()
    return (text, False) if len(b) <= n else (b[-n:].decode(errors="ignore"), True)


async def recall(text, timings):
    """Up to two memory hits for this utterance, or none if the brain cannot answer within RECALL_WAIT_S."""
    t = time.perf_counter()
    try:
        reply = await links.brain_request({"type": "recall", "text": text[:300],
                                           "persona": (CALL_PERSONA.get() or {}).get("id")}, RECALL_WAIT_S)
        hits = [str(h)[:300] for h in reply.get("hits", [])][:2] if isinstance(reply.get("hits"), list) else []
    except (RuntimeError, asyncio.TimeoutError):
        hits = []
    timings["recall_ms"] = ms(t)
    return hits


OPENING_CHUNK_SYL = 45  # an opening's later chunks: a few sentences per TTS call
CHUNK_GROWTH = 1.4  # each chunk at most this much longer than the one before: TTS runs ~0.65x real time, so the
#                     one playing still covers the next one's synthesis


def opening_chunks(parts):
    """The first sentence alone (ready soonest), then sentences joined into chunks that grow by CHUNK_GROWTH up to
    OPENING_CHUNK_SYL: fewer TTS calls and smoother prosody, while each chunk's synthesis stays covered by the one
    playing before it."""
    out = parts[:1]
    for p in parts[1:]:
        cap = min(OPENING_CHUNK_SYL, max(FIRST_MIN_SYL, int(syllables(out[-2] if len(out) > 1 else out[0]) * CHUNK_GROWTH)))
        if len(out) > 1 and syllables(out[-1]) + syllables(p) <= cap:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def prepare_opening(text, reason="", persona=""):
    """Split a placed call's opening and start synthesizing it now, chunk by chunk, while the phone rings: the
    callee hears it as soon as enough is ready to play the rest without a gap, not after all of it."""
    parts = opening_chunks(split_ready(text, final=True)[0]) if text else []
    op = {"text": text, "parts": parts, "audio": None, "chunks": None, "started": {}, "reason": reason,
          "persona": persona}
    if parts:
        try:
            start_opening_synth(op)
        except RuntimeError:
            pass  # no event loop (self-checks): synthesize on connect instead
    return op


def start_opening_synth(op):
    loop = asyncio.get_running_loop()
    op["chunks"] = [loop.create_future() for _ in op["parts"]]
    op["audio"] = loop.create_task(_synth_seq(op))


async def _synth_seq(op):
    """Synthesize the opening's chunks in order (one TTS request at a time), each into its future as it lands."""
    CALL_PERSONA.set(pick_persona(links.settings, op.get("persona", "")))  # this task only: the call's voice
    out = []
    for i, (p, fut) in enumerate(zip(op["parts"], op["chunks"])):
        op["started"][i] = time.monotonic()
        try:
            pcm = (await tts(p))[0]
        except Exception as e:  # the speaker retries this chunk itself
            if not fut.done():
                fut.set_exception(e)
                fut.exception()  # retrieved: an opening nobody plays (replaced, never answered) logs nothing
            continue
        if not fut.done():
            fut.set_result(pcm)
        out.append(pcm)
    return out


async def opening_audio(op):
    """(sentence, pcm) of a placed call's opening, in order, starting only once the ready audio covers the
    synthesis of the rest (gap_free), or after HOLD_MAX_S at the latest."""
    parts = op["parts"]
    if op.get("chunks") is None:
        start_opening_synth(op)
    chunks, t0 = op["chunks"], time.monotonic()
    while time.monotonic() - t0 < HOLD_MAX_S:
        n = next((i for i, f in enumerate(chunks) if not f.done()), len(chunks))  # chunks ready in a row
        if n == len(chunks):
            break
        ok = [f for f in chunks[:n] if not f.cancelled() and f.exception() is None]
        now = time.monotonic()
        if n and gap_free(now, sum(len(f.result()) / 2 / 24000 for f in ok), [syllables(p) for p in parts[n + 1:]],
                          (op["started"].get(n, now), syllables(parts[n]))):
            break
        await asyncio.sleep(0.05)
    for s, fut in zip(parts, chunks):
        try:
            pcm = await fut
        except Exception:  # its synthesis failed (or was cancelled): once more, here
            pcm = (await tts(s))[0]
        yield s, pcm


GREETING_MAX_S = 1.5


PICKUP_WORDS = {"여보세요", "여보세용", "여보", "네", "네네", "예", "응", "응응", "어", "왜", "왜요", "누구세요", "누구야", "누구",
             "안녕", "안녕하세요", "말씀하세요", "그래", "듣고", "있어", "있어요"}


def is_ack(text, name=""):
    """Picking up a placed call: "여보세요", "네", "어 왜", "누구세요", "아젤?" (any length). Never a turn of its own."""
    words = [w for w in re.split(r"[\s.,!?~…]+", (text or "").strip()) if w]
    names = tuple(n for n in (name, DEFAULT_NAME) if n)
    return bool(words) and all(w in PICKUP_WORDS or w.startswith(names) for w in words)


class OpeningGuard:
    """Keeps a placed call's opening from being cut off by the callee's "여보세요".

    From the moment a placed call connects until its opening has played, speech is held instead of interrupting,
    and the call handler decides on the words (opening_heard): greetings and acks never cut it, only a stop word
    or a real question does, after the first sentence. Times are time.monotonic() seconds."""

    def __init__(self):
        self.first_end = self.play_end = 0.0
        self.speech_at = None

    def active(self, now):
        return now < self.play_end

    def start(self, now):
        """Speech began while the assistant is talking: 'interrupt' now, or 'hold'."""
        if not self.active(now):
            return "interrupt"
        self.speech_at = now
        return "hold"

    def protect(self, now):
        """A placed call's opening is starting: protected until speak_opening knows when sentence one ends."""
        self.first_end = self.play_end = now + 3600

    def wait(self, now):
        """Seconds until an interrupt may act: the rest of the opening's first sentence, else 0."""
        return max(0.0, self.first_end - now) if self.active(now) else 0.0

    def end(self, now, seconds):
        """An utterance ended: 'answer' (normal turn), 'check' (maybe a greeting) or 'interrupt' then answer."""
        held, self.speech_at = self.speech_at is not None, None
        if not held:
            return "answer"
        return "check" if seconds < GREETING_MAX_S else "interrupt"

_SENT_END = re.compile(r"(?<=[.!?。…~])\s+|\n+")
# Qwen sometimes slips Chinese characters (or kana, Cyrillic, Thai) into a Korean reply; the Korean voices read
# them as garble. They are dropped before TTS, captions and history.
FOREIGN = re.compile(r"[\u2e80-\u2fdf\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u0400-\u04ff\u0e00-\u0e7f]+")
foreign_dropped = [0]  # sentences cleaned since start (route_eval reports it)


URL = re.compile(r"\s*[(（]?\s*(?:https?://|www\.)[^\s)）]+[)）]?")


CUT_MARK = " (여기서 끊김)"  # history only: the user heard the reply up to here (hung up or cut in)


def heard_part(sent, cut_at, finished):
    """(the sentences the phone had started playing by cut_at, whether the reply was cut short). `sent`: (sentence,
    start, end) in phone playback time; audio is sent ahead of playback, so "sent" is not "heard"."""
    heard = [s for s, start, _ in sent if start < cut_at]
    return heard, not finished or len(heard) < len(sent) or bool(sent and sent[-1][2] > cut_at)


def keep_heard(hist, entry, heard, cut):
    """Rewrite a reply's history turn to what was actually heard, marked when cut off; nothing heard: no turn."""
    for i in range(len(hist) - 1, -1, -1):
        if hist[i] is entry:
            if heard:
                hist[i] = {"role": "assistant", "content": " ".join(heard) + (CUT_MARK if cut else "")}
            else:
                del hist[i]
            return


def korean_only(s: str) -> str:
    s = URL.sub("", s).replace(CUT_MARK.strip(), "")  # a model echoing the history marker never says it  # sources are said by name ("기상청 기준으로"), never read out as addresses
    t = FOREIGN.sub("", s)
    if t == s:
        return s
    foreign_dropped[0] += 1
    t = re.sub(r"\s{2,}", " ", re.sub(r"[(（]\s*[)）]", "", t)).strip()
    return t if re.search(r"[가-힣A-Za-z0-9]", t) else ""


MIN_SYL = 5  # Qwen3-TTS garbles very short inputs ("네," came out as "쵸-"): never synthesize less alone
_LETTER = re.compile(r"[가-힣A-Za-z0-9]")


def syllables(s: str) -> int:
    return len(_LETTER.findall(s))


def split_ready(buf: str, final: bool, first: bool = False):
    """Return (chunks ready for TTS, remainder) from the streamed text buffer.

    Chunks end at a sentence end or at ", " (never mid-word or inside "3,000") and have at least MIN_SYL
    syllables: a short sentence waits for the next one. first=True: nothing has been spoken yet, so a leading
    clause ending in a comma goes out alone if it is long enough. TTS time grows with length, so this starts
    the voice soonest. final=True: everything goes; a short tail joins the chunk before it.
    """
    if first and not final:
        m = re.match(r"\s*([^,.!?\n]{1,20},)\s", buf)
        if m and syllables(korean_only(m.group(1))) >= MIN_SYL:
            return [korean_only(m.group(1).strip())], buf[m.end():]
    parts = _SENT_END.split(buf)
    rest = "" if final else parts.pop()  # unstripped: "에너지를 " + "이용" must keep its space
    parts = [p.strip() for p in parts]
    out, cur = [], ""
    for p in parts:
        p = korean_only(p)
        if not p:
            continue
        cur = f"{cur} {p}".strip()
        if syllables(cur) >= MIN_SYL:
            out.append(cur)
            cur = ""
    if cur and final:
        if out and syllables(cur) < MIN_SYL:
            out[-1] += " " + cur
        else:
            out.append(cur)
    elif cur:
        rest = f"{cur} {rest}"
    if not final and len(rest) > 80:  # flush an over-long clause at its last ", "
        cuts = [m.end() for m in re.finditer(r",\s", rest)]
        if cuts and syllables(rest[:cuts[-1]]) >= MIN_SYL:
            out.append(korean_only(rest[:cuts[-1]].strip()))
            rest = rest[cuts[-1]:]
    return [c for c in out if c], rest


def pick(table, text):
    return next((k for k, pat in table.items() if re.search(pat, text, re.I)), None)


def pre_route(text, settings, local=False, tools=()):
    """(straight to Claude?, model) before any LLM call. Local first (user decision 2026-11-23): only a model or Claude
    the user asked for by name, or a default model set in the settings, goes straight to Claude. Everything else
    (schedules, weather, prices, servers, devices, files) starts on Qwen with its tools; Qwen hands over with the routing
    tag when it truly can't, and the claim/promise guards in respond() hand over when it says it did what no tool did.
    `local` and `tools` are kept for the callers: the answer no longer depends on them."""
    forced_model = pick(MODEL_WORDS, text)
    default = settings.get("default_model", "auto")
    model = forced_model or (default if default in MODEL_WORDS else None)
    if CLAUDE_NO.search(text):
        return bool(forced_model) or default in MODEL_WORDS, model  # "클로드는 쓰지마": never by keyword
    return bool(forced_model) or default in MODEL_WORDS or bool(USE_CLAUDE.search(text)), model


# A reply with no content: acknowledgements and promises only ("네.", "알겠습니다", "네, 확인해 볼게요").
ACK_WORDS = re.compile(r"네네|넵|네|예|응|알겠습니다|알겠어요|알았어요|좋아요|그럼요|물론이죠|맞아요|그렇죠|그래요|"
                       r"(확인|찾아|알아|검색)(해\s*)?(볼게요|드릴게요)|잠시만요|기다려\s*주세요")
TRIVIAL = re.compile(r"(안녕\S*|고마워\S*|고맙\S*|감사\S*|응|어|네|예|그래|그래요|알겠어|알았어|좋아|오케이|ok|여보세요|여보|"
                     r"잘\s*자|수고\S*|아니\S*|맞아\S*|됐어|괜찮아\S*|오키)[\s.!~?,]*", re.I)
# "엔트로픽 주가 찾아볼게요." with no tool call: a promise, not an answer. Held back, spoken only once a tool runs.
ANNOUNCE = re.compile(r"(찾아|알아|확인해|검색해|살펴|조회해|점검해)\s*(볼게요|보겠습니다|드릴게요)"
                      r"|(확인|검색|조회|점검|파악)\s*(할게요|하겠습니다|해\s*드릴게요)|알려\s*드릴게요"
                      r"|^(잠시만|잠깐만)\s*(기다려\s*주세요|기다려\s*줘|요)?[.!~]*$|기다려\s*주세요[.!~]*$")
# "잠깐만요." alone in small talk is not a promise: only a lookup verb with what is looked up, or any of these words
# when the user's turn asked for current info or their schedule (user decision: minimal guards on no-tool chat).
LOOKUP_VERB = re.compile(r"확인|찾아|알아보|알아봐|검색|조회|점검|살펴")
AGENDA_WORDS = re.compile(r"일정|캘린더|스케줄|수업|마감|과제|할\s*일|브리핑")  # what the agenda prefetch already holds
# Schedules, todos and calendar edits are answered on Qwen from the prefetched agenda and the gateway's tools
# (agenda, gistra/planit, calendar_write with its confirm), never handed to Claude (user decision 2026-11-23).
SCHEDULE_TURN = re.compile(AGENDA_WORDS.pattern + r"|시험|약속|회의|투두|todo|플래닛|planit|" + PLANS_ASK, re.I)
SCHEDULE_FAILED = "일정을 지금 확인하지 못했어요. 다시 말씀해 주세요."
LOOKUP_OBJ = re.compile(r"일정|캘린더|스케줄|수업|마감|과제|할\s*일|날씨|기온|주가|주식|환율|시세|가격|뉴스|브리핑|상태")


def is_promise(s, wants_lookup):
    return bool(ANNOUNCE.search(s)) and (wants_lookup or bool(LOOKUP_VERB.search(s) and LOOKUP_OBJ.search(s)))


def future(s):
    """A first-person future ending: '불러올게요', '드릴게요', '갈게', '하겠습니다' (a syllable with final ㄹ before 게)."""
    if re.search(r"겠습니다|겠어요", s):
        return True
    return any((ord(m[1]) - 0xAC00) % 28 == 8 for m in re.finditer(r"([가-힣])\s*게요?(?![가-힣])", s))


# The user pushing on an unkept promise ("아니 말로만 하지 말고 진짜 하라고", "일로 와"): the request is the earlier one.
PUSH = re.compile(r"말로만|진짜\s*(해|하라)|빨리|아직(도)?\s*(안|멀)|왜\s*안|해\s*달라고|하라고|하라니까|일로\s*와|이리\s*와|어서|됐어\?|했어\?")
REQUEST = re.compile(r"해\s*줘|해\s*봐|해\s*달라|하라고|하라니까|알려\s*줘|보여\s*줘|불러\s*줘|찾아\s*줘|확인\s*해|읽어\s*줘")
pending_lookup: dict[str, tuple[float, str]] = {}  # history key -> (when, the request a promise was not kept for)
RANGE_WORDS = ((r"일주일|7\s*일|칠\s*일|한\s*주|최근\s*주", "last_7_days"), (r"지난\s*주", "last_week"),
               (r"이번\s*주", "this_week"), (r"이번\s*달|한\s*달", "this_month"), (r"어제", "yesterday"), (r"오늘", "today"))


def infer_args(tool, text):
    """Arguments for a forced call from the user's own words: a range word, the words themselves for a required text
    field, the first choice for a required enum. Nothing invented beyond that."""
    schema = tool.get("input_schema") or {}
    props, need = schema.get("properties") or {}, set(schema.get("required") or [])
    args = {}
    if "range" in props and (r := next((r for w, r in RANGE_WORDS if re.search(w, text)), None)):
        args["range"] = r
    for k, p in props.items():
        if k in args or k not in need or not isinstance(p, dict):
            continue
        if p.get("enum"):
            args[k] = next((e for e in p["enum"] if str(e) in text), p["enum"][0])
        elif p.get("type") == "string":
            args[k] = text[:int(p.get("maxLength") or 200)]
    return args


# a promise that never got its tool call (after a nudge and, where one fits, a direct lookup): said, not dropped
LOOKUP_FAILED = "죄송해요, 지금은 그걸 찾아보지 못했어요."
WEATHER_Q = re.compile(r"날씨|기온|몇\s*도|비\s*(와|올|오)|눈\s*(와|올|오)|우산|추워|더워|미세먼지")
# Today's figures (rates, prices, weather): a number said before any lookup is a guess ("환율은 약 1,310원대",
# live 2026-11-22), so it is held back and Qwen is asked to look it up (re-asked, never cut to a filler).
# "시세 정보를 드리기 어렵습니다", "증권사 앱에서 확인하는 게 빨라" (live 2026-11-23, typed stock questions on both
# personas): a refusal to look up what a tool answers. Held like a guess; the lookup runs instead.
REFUSED_LOOKUP = re.compile(r"(드리기|알려\s*드리기|확인하기|제공하기|보여\s*드리기|찾기)\s*(가\s*)?어렵|확인할\s*수\s*(가\s*)?없|"
                            r"알\s*수\s*(가\s*)?없|실시간.{0,12}(못|없|어렵|않)|(앱|사이트|증권사|포털|검색).{0,14}(확인|보시|보는|찾아\s*보)|"
                            r"보여\s*(드릴|줄|드리지)\s*(수\s*(가\s*)?없|못)|음성으로.{0,10}(없|어렵|못)|"
                            r"(폰|휴대폰|핸드폰|PC|컴퓨터|화면).{0,12}(확인|보시|보는|보세요|보시는)")
LIVE_Q = re.compile(r"환율|주가|주식|시세|가격|금리|유가|코스피|코스닥|나스닥|비트코인|날씨|기온|몇\s*도|상장|IPO")
# a listing claim from memory ("엔트로픽은 비상장이라"): live 2026-11-22 it was stale, ANT had listed on Nasdaq
LISTING = re.compile(r"비상장|상장\s*(전|안|되지|않|된|됐|했|돼)|상장사|기업\s*공개|IPO")
# a figure, in digits or in words ("천삼백삼십 원"): Qwen writes numbers both ways
FIGURE = re.compile(r"\d|[일이삼사오육칠팔구십백천만억]{2,}\s*(원|달러|엔|도|퍼센트|%)")
TOOL_NUDGE = (f"방금 찾아보겠다고만 하고 도구를 부르지 않았어. 지금 바로 도구를 불러: 다른 말 없이 {TOOL_TAG} "
              '{"name": "도구 이름", "args": {…}} 한 줄만 출력해. 일정·브리핑은 agenda, 날씨는 weather, 주가와 환율은 stock_quote, 그 밖의 지금 정보(가격, 뉴스)는 '
              'web_search.')
RECHECK = {"action": "방금 '{s}'라고 했는데, 이번 대화에서 그 일을 한 도구가 없었어. 하지 않은 일을 했다고 하지 말고, 그 말은 빼고 "
                      "이어서 자연스럽게 다시 말해. 이미 한 말은 반복하지 마.",
           "personal": "방금 '{s}'라고 했는데, 사용자의 일정이나 개인 정보는 확인된 게 없어. 지어내지 말고 그 말은 빼고 이어서 "
                        "자연스럽게 다시 말해. 이미 한 말은 반복하지 마."}
# ---------- tool-first for read-only lookups (PM decision 2026-11-23): before Qwen answers, a short Qwen call decides
# whether the words need data from a read-only tool. If so the tool runs first and Qwen answers from its result, so a
# lookup no longer depends on Qwen choosing to call one mid-answer. Action tools keep the [TOOL] path.
def read_only(name):
    """The gateway's flag on its tools frame: no side effect, safe to call speculatively. A tool without it is an action."""
    return any(t["name"] == name and t.get("read_only") is True for t in gw_tools["defs"])


ROUTE_S = 4.0  # the decision (~0.8 s alone, slower beside Qwen's own answer on the shared GPU); then as before
LOOKUP_S = 8.0  # the lookup itself; past this, it is said that it failed
ROUTE_SYSTEM = ("너는 음성 비서의 조회 판단기야. 사용자의 마지막 말에 답하려면 아래 읽기 도구로 지금 데이터를 봐야 하는지 정해. "
                "필요 없으면 NONE 한 단어만 출력해. 필요하면 다른 말 없이 JSON 배열 하나만 출력해: "
                "[{\"name\": 도구, \"args\": {…}}, …]. 여러 대상(여러 종목, 여러 도시처럼)을 함께 물으면 대상마다 하나씩 넣어"
                "(최대 6개). '차트 보여줘', '띄워줘'처럼 화면에 보여 달라는 것도 조회야(앱이 결과를 카드로 보여 줘). "
                "잡담, 감정, 농담, 게임, 상식, 계산, 비서 자신에 대한 질문은 NONE. 확실하지 않으면 NONE.\n도구:\n")
ROUTE_MAX = 6
# the user's own things ("내가 가진 주식", "내 포트폴리오"): what they are is in memory, so it is read before the domain
# tools (live 2026-11-23: "내 주식 얼마나 올랐어?" quoted one stock, or said nothing was saved, with the holdings in memory)
HOLDINGS_Q = re.compile(r"주식|종목|포트폴리오|보유|주가|수익|손익|평가")
MINE = re.compile(r"포트폴리오|내가\s*(갖고|가지고|가진|보유|산|들고|하는|다니는|키우는)|내\s*(주식|종목|포트폴리오|자산|계좌|보유|구독|차|집|친구)|"
                  r"보유\s*(한|하고|현황|종목|주식)|우리\s*(집|가족|회사)")
# "서버 괜찮아?" read host load only and answered "모두 정상" while a service was down (live 2026-11-23 03:14): any
# server question reads hosts, problem services and alerts together, and the answer leads with what is wrong
INFRA_READ = ("infra_status", "infra_alerts", "infra_health")
INFRA_Q = re.compile(r"서버|인프라|노드|홈서버|컨테이너|경보|알림 떴|다운|죽었|터졌")
INFRA_HEAD = {"infra_status": "문제 있는 서비스", "infra_alerts": "울리는 경보", "infra_health": "서버별 상태"}
INFRA_NOTE = ("(멈추거나 문제 있는 서비스와 경보가 있으면 그것부터, 어느 서버에서 언제부터인지 말하고, 나머지 서버 상태는 그다음에 "
              "짧게 말해. '모두 정상'은 세 항목 모두 문제가 없을 때만 말해. 사용자가 말한 경보나 서비스 이름이 있으면 여기서 찾아 답해.)")
TOOL_ACTIVITY = (("infra_", "서버 상태 확인 중", "server"), ("phone_", "아이폰 데이터 확인 중", "generic"),
                 ("agenda", "일정 확인 중", "calendar"), ("gistra_", "학사 일정 확인 중", "calendar"),
                 ("calendar_", "캘린더 확인 중", "calendar"), ("gmail_", "메일 확인 중", "mail"),
                 ("memory_", "기억 찾는 중", "memory"), ("people_", "기억 찾는 중", "memory"),
                 ("weather", "날씨 확인 중", "web"), ("stock_quote", "시세 확인 중", "web"), ("portfolio", "보유 종목 확인 중", "web"),
                 ("web_", "웹에서 찾는 중", "web"))


def tool_activity(name):
    return next(((label, kind) for pre, label, kind in TOOL_ACTIVITY if name.startswith(pre)), ("확인 중", "generic"))


async def infra_read(names, session):
    """The three infra reads at once, under their own headings, with how to answer from them."""
    lines = [TOOL_TAG + " " + json.dumps({"name": n, "args": {}}) for n in names]
    sink, held = CALL_CARDS.get(), []

    async def hold(ev):
        held.append(ev["card"])
    CALL_CARDS.set((hold, None) if sink else None)  # this task's context only: each read's card is held here
    got = await asyncio.gather(*(run_tool(x, session, None) for x in lines), return_exceptions=True)
    CALL_CARDS.set(sink)
    if held:  # one infra card a turn: the host numbers, the alert groups and the stopped services together
        hosts = [h for c in held for h in c.get("hosts") or [] if isinstance(h, dict)]
        down = [d for c in held for d in c.get("down") or [] if isinstance(d, str)]
        await relay_card({"kind": "infra", "title": "서버 상태", **({"hosts": hosts} if hosts else {}),
                          **({"down": down} if down else {})})
    parts = [f"[{INFRA_HEAD[n]}]\n{r if isinstance(r, str) else '가져오지 못했어요.'}" for n, r in zip(names, got)]
    return "\n".join(parts) + "\n" + INFRA_NOTE


LOOKUP_ACK = "네, 바로 볼게요."  # said as a lookup starts: 5+ syllables (shorter garbles, and waited for the answer)
FILLERS = (FILLER, FILLER_LONG, LOOKUP_ACK)  # played as they come (no hold gate, no merging), cached, not captioned
LOOKUP_SLOW = "죄송해요, 조회가 늦어져서 지금은 가져오지 못했어요."  # a lookup past LOOKUP_S: said, never silence
LOOKUP_SLOW_S = 1.2


def route_lines(tools):
    """One line per tool with what each argument takes (its choices or description): "내일 서울 날씨" got when=today
    when the router saw only the argument names."""
    def arg(k, p):
        p = p if isinstance(p, dict) else {}
        what = "|".join(map(str, p["enum"])) if p.get("enum") else str(p.get("description", ""))[:60]
        return f"{k}: {what}" if what else k
    out = []
    for t in tools:
        props = (t.get("input_schema") or {}).get("properties") or {}
        args = "; ".join(arg(k, p) for k, p in props.items())
        out.append(f"- {t['name']}({args}): {str(t.get('description', ''))[:150]}")
    return "\n".join(out)


async def route_lookup(text, recent, tools):
    """[{"name", "args"}, …] of the read-only lookups these words need now (one per item asked about), or None."""
    msgs = [{"role": "system", "content": ROUTE_SYSTEM + route_lines(tools) + now_kst()},
            *[m for m in recent if m.get("role") in ("user", "assistant")][-2:], {"role": "user", "content": text}]
    body = {"model": LLM_MODEL, "messages": msgs, "max_tokens": 400, "temperature": 0}  # 80 cut a 4-call list to 2
    r = await client.post(LLM_URL, json=body)
    r.raise_for_status()
    out = re.sub(r"(?s)<think>.*?</think>", "", r.json()["choices"][0]["message"].get("content") or "").strip()
    line = (native_call(out) or "") if NATIVE_TAG in out else out  # unparsable markup: no pick (None.find, live 2026-11-23)
    try:
        start = min(i for i in (line.find("["), line.find("{")) if i >= 0)
        got = json.loads(line[start:max(line.rfind("]"), line.rfind("}")) + 1])
    except ValueError:
        return None
    names, picks = {t["name"] for t in tools}, []
    for d in got if isinstance(got, list) else [got]:
        call = {"name": d.get("name"), "args": d.get("args") or d.get("arguments") or {}} if isinstance(d, dict) else {}
        if call.get("name") in names and isinstance(call["args"], dict) and call not in picks:
            picks.append(call)
    return picks[:ROUTE_MAX] or None


# "확인해 봤는데, 모두 정상으로 나와요" with no tool behind it (live 2026-11-23 03:14, a real service was down): the
# outcome of a check nobody made. Never spoken; the lookup runs instead.
CHECKED = re.compile(r"(확인|조회|살펴|점검|찾아)\s*(해\s*)?(봤|보니|본\s*결과)|(확인|조회)\s*(했는데|했더니|결과)|(나와|나오)(요|네요|는데)")
# "다음과 같아요." and nothing after it (live 2026-11-23 03:41): held until content follows
LEAD_IN = re.compile(r"(다음|아래)(과|와)\s*같(아요|습니다|아|은데요?)[.!:]?\s*$")
LEAD_NOTE = "방금 목록을 말하겠다고만 하고 내용이 없었어. 그 내용을 바로 말해."
TOOL_TALK = re.compile(r"도구(를|로|가|는)|툴(을|로)")  # our own nudge wording, parroted (live 2026-11-23): never spoken
PROMISE_NOTE = "방금은 하겠다는 말만 하고 내용이 없었어. 같은 말은 반복하지 말고, 지금 바로 그 내용을 말로 이어서 해."
STUB_NOTE = "방금 답이 너무 짧아서 아무 내용이 없었어. 이미 한 말은 반복하지 말고, 질문에 네 성격대로 내용을 담아 이어서 답해."
INTERJECTION = re.compile(r"아+|어+|오+|음+|응+|네+|예|그거|그게|뭐+|헐|와+|우와|아이고|진짜|정말|그러니까|있잖아|맞다|글쎄|"
                          r"미안|미안해|미안해요|죄송|죄송해요")  # "아, 미안." to "무슨 일 하냐고" answered nothing


class Recheck(Exception):
    def __init__(self, kind, sentence):
        super().__init__(sentence)
        self.kind, self.sentence = kind, sentence


def stub(reply, title=""):
    """True when a reply is only an interjection ("아, 그거!", "오빠, 아, 그거!"): nothing else but one syllable at most.
    A short real answer ("세 개야.") is not one."""
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", reply.replace(title, " ") if title else reply)
             if not INTERJECTION.fullmatch(w)]
    return len("".join(words)) < 2


NUDGE = ("방금 대답에 내용이 없었어. 질문에 실제 내용으로 바로 답해. "
         f"직접 답할 수 없으면 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해.")


# A sentence about the user's own plans ("내일 일정은 두 건이에요", "오전 열 시에 팀 회의") is spoken only when its source is
# in front of Qwen: the brain's context, a tool result or the user's own words in this call. Its hours must appear
# there, and a plan with no hour at all needs some timed source. Otherwise Claude answers (it checks the real data).
SCHED_NOUN = re.compile(r"일정|할 ?일|마감|회의|약속|미팅|수업|과제|시험|스케줄|제출|데드라인")
KO_HOURS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
            "열한": 11, "열두": 12}
_HOUR_RE = re.compile(r"(\d{1,2})\s*(?:시(?!간)|:\d{2})|(열한|열두|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*시(?!간)")


# "오 분 뒤에 전화드릴게요" with nothing scheduled: a sentence saying the assistant did or will do an action is spoken only
# when a tool in this turn confirmed one. Otherwise a scheduling request is asked back ("몇 분 뒤에…?"), anything else
# goes to Claude.
# A future promise only: "제가 전화드렸어요" on a placed call (the assistant DID call) is a fact, not a claim.
ACTION_CLAIM = re.compile(r"전화(를)? ?(드릴게요|드릴께요|걸게요|걸어 ?드릴게요|해 ?드릴게요)|예약(을)? ?(했|해 ?드렸|해 ?둘게요|"
                          r"해 ?드릴게요|할게요|잡았|해 ?놨)|깨워 ?드릴게요|알람(을)? ?(맞췄|맞춰 ?(둘|드릴)게요)|맡겼|맡겨 ?(둘|드릴)게요|"
                          r"실행했|열었(어요|습니다)|보냈(어요|습니다)|취소했|"
                          r"(기억|저장|추가|등록|메모|삭제|재시작|재시동|시동|다시 ?시작|접속|실행)\S*?\s*(했|해 ?(두|놨|드렸)|두었|뒀|놨|드렸|됐)|"
                          r"(시도|재시작|재시동|시동|접속|실행|처리)\S*?\s*(해\s*)?(볼게요|드릴게요|할게요|하겠습니다|보겠습니다)")
TOOL_REFUSED = re.compile(r"아무것도 바꾸지 않았|이미 통화 중|다 걸었어요|없는 도구|쓸 수 없어요|틀렸어요|실패|거절|만료|허용되지 않아|할 수 없어요|없어요\.?$")
ASK_WHEN = "몇 분 뒤에 걸어 드릴까요?"
ACTION_TOOLS = {"calendar_write", "call_user", "create_task", "schedule_cancel", "run_command", "delegate", "add_todo", "done_todo", "remember", "forget"}
CLAIM_FALLBACK = "그건 제가 직접 처리하지 못했어요. 어떻게 할지 다시 말씀해 주시겠어요?"
LOOKUP_TOOL = re.compile(r"task_status|schedule_list|overnight_list|list_jobs|job_status$")


# a sentence that is not finished: it ends on a connective or a particle ("…엔트로픽이랑", "그리고", "…인데")
DANGLING = re.compile(r"(그리고|그런데|근데|그러니까|그래서|그러면|또는|아니면|이랑|랑|하고|이고|거나|는데|은데|인데|니까|지만|에서|한테|에게|에다|에는)[\s.,…]*$")
DANGLING_WAIT_S = 1.3
# "오늘 일정 브리핑해 드릴게요.", "알려 드릴게요": an announcement with no content in it
INTRO_ANNOUNCE = re.compile(r"(브리핑|알려|말씀|정리|설명|안내|확인해|읽어)\s*(해\s*)?드릴게요[.!~]*$|(브리핑|알려|정리|설명)\s*(해\s*)?드리겠습니다[.!~]*$")
ASKS_ENGLISH = re.compile(r"영어|english|번역|translate", re.I)


# The request, not the reply's wording: an imperative that is not a question for information ("재시동해줘", "접속해서 켜 줘").
# When it was made and no action tool ran this turn, a local statement about doing it can only be a made-up claim.
IMPERATIVE = re.compile(r"(하라|하자|해라|해봐|[가-힣]\s?줘|[가-힣]\s?주세요|[가-힣]\s?줄래|[가-힣]\s?주라|달라|부탁\S*)[\s.!~요]*$")
INFO_ASK = re.compile(r"알려|설명|말해|얘기|이야기|추천|번역|읽어|찾아|검색|보여|가르쳐|요약|계산|비교|생각|대답|답해|맞혀|"
                      r"농담|웃겨|노래|확인|조회|점검|체크|열어|알아봐|궁금|도와|정리해|브리핑")
# what a local model may still say when nothing ran: a question back, a refusal, or "I don't know"
UNCLAIMED = re.compile(r"\?|못 ?(해|하|했)|할 수 (없|있)|수 없|없어요|없습니다|없네요|모르|어떤|어디|언제|몇|무엇|뭘|누구|어떻게|도구|"
                       r"할까요|드릴까요|볼까요|줄까요|말씀해|알려 ?주")


def requests_action(text):
    """An explicit do-it request. Not TASK_WORDS (a schedule or code question is answered from a lookup, not an action)."""
    return bool(SCHED_CALL.search(text) or SCHED_WAKE.search(text) or ASK_CALL.search(text) or ASK_STORE.search(text)
                or (IMPERATIVE.search(text.strip()) and not INFO_ASK.search(text)))


def unconfirmed_claim(sentence, user_text):
    """A sentence that says an action was or will be done (the wording list, or any plain statement when an action
    was requested): checked only against `confirmed`, never trusted on its own."""
    if ACTION_CLAIM.search(sentence):
        return asks_action(user_text) or requests_action(user_text)
    st = sentence.strip()
    return (requests_action(user_text) and len(re.findall(r"[가-힣]", st)) >= 6 and not UNCLAIMED.search(st)
            and bool(re.search(r"(요|다|죠|네|께)[.!~]*$", st)))


def asks_action(text):
    """The user's own words ask for something done (a call, a wake-up, a task): only then is a claim checked."""
    return bool(SCHED_CALL.search(text) or SCHED_WAKE.search(text) or ASK_CALL.search(text) or ASK_STORE.search(text)
                or pick(TASK_WORDS, text))


ASK_STORE = re.compile(r"(기억|저장|메모|추가|등록|삭제|재시작|재시동|시동|다시 ?시작|접속)\S*?\s*(해|하자|좀|부탁)|재시작|재시동|재부팅|잊지 ?마|지워 ?줘")
ASK_CALL = re.compile(r"(전화|연락|콜)\s*(좀|부탁|줘|주세요|줄래|줄 ?수)|(예약|알람|맡겨)\s*(좀|줘)")


def action_tool(name):
    """mcp__azel__call_user, call_user, mcp__macbook__run_command, macbook__run_command, …"""
    return str(name).rsplit("__", 1)[-1] in ACTION_TOOLS


class Unconfirmed(Exception):
    """Qwen claimed an action no tool confirmed this turn."""


class Ungrounded(Exception):
    """Qwen stated the user's plans without a source: the turn goes to Claude instead."""


def hours_in(text):
    return {(int(n) if n else KO_HOURS[w]) % 12 for n, w in _HOUR_RE.findall(text)}


# Not a statement of the user's plans: a question, a conditional or a suggestion ("일정이 빡빡하면 일찍 주무세요"), or the
# persona talking about itself ("나 내일 수업 있어").
NOT_A_CLAIM = re.compile(r"\?|면\s|면서|거든요?\s|(세요|시죠|자|봐|볼까|어때|할래|하세요|드세요)[.!~]*$|^\s*(나|내가|난|저|제가|전)\s")


ASSERTS_PLAN = re.compile(r"있(어|어요|습니다|고|네)|\d+\s*건|[한두세네]\s*(건|개)|잡혀|예정")
NEGATED = re.compile(r"안\s|않|없|못\s|모르")


def grounded(sentence, sources):
    """False for a claim about the user's plans that no source backs (see SCHED_NOUN above)."""
    if not SCHED_NOUN.search(sentence) or NOT_A_CLAIM.search(sentence.strip()):
        return True
    said, known = hours_in(sentence), hours_in(sources)
    if said:
        return said <= known
    # no hour: only "plans exist" counts as a claim ("내일 일정은 두 건이에요"), never a negation ("확인이 안 돼요")
    return bool(known) or not ASSERTS_PLAN.search(sentence) or bool(NEGATED.search(sentence))


def content_free(reply):
    """True when nothing is left once acknowledgements, promises and punctuation are removed."""
    rest = ACK_WORDS.sub("", reply)
    return not re.search(r"[가-힣A-Za-z0-9]", rest)


def needs_answer(text):
    """A question or request (not a greeting, thanks, confirmation or a plain statement): a bare "네" is no answer.
    Small talk ("네, 알겠습니다.", "할 일이 많아.", "자기야 안녕") is answered by whatever Qwen says, never escalated."""
    t = text.strip()
    return bool(QUESTION.search(t)) and not TRIVIAL.fullmatch(t)


# Qwen's own chat-template tool call, seen live 2026-11-22 instead of the [TOOL] line (and spoken aloud):
#   <tool_call> <function=weather> <parameter=place> 광주 </parameter> </function> </tool_call>
# or with JSON inside: <tool_call>{"name": "weather", "arguments": {"place": "광주"}}</tool_call>
NATIVE_TAG = "<tool_call>"


def native_call(text):
    """The [TOOL] line for Qwen's native tool-call markup, or None."""
    body = text.split(NATIVE_TAG, 1)[-1].split("</tool_call>")[0]
    if m := re.search(r"<function=([\w-]+)>", body):
        args = {}
        for k, v in re.findall(r"<parameter=([\w-]+)>\s*(.*?)\s*</parameter>", body, re.S):
            try:
                args[k] = json.loads(v)
            except ValueError:
                args[k] = v
        return TOOL_TAG + " " + json.dumps({"name": m[1], "args": args}, ensure_ascii=False)
    try:
        d = json.loads(body[body.index("{"):body.rindex("}") + 1])
        return TOOL_TAG + " " + json.dumps({"name": d.get("name"), "args": d.get("arguments") or d.get("args") or {}},
                                           ensure_ascii=False)
    except ValueError:
        return None


def hold_tag_prefix(buf):
    """(speakable text, held tail): a tail that could still grow into the routing tag is never spoken early."""
    at = max(buf.rfind("["), buf.rfind("<"))
    tail = buf[at:].rstrip()
    if at != -1 and any(t.startswith(tail) for t in (ROUTE_TAG, TOOL_TAG, NATIVE_TAG)) and len(buf) - at < len(ROUTE_TAG) + 2:
        return buf[:at], buf[at:]
    return buf, ""


def tag_state(head):
    """Qwen's reply so far (left-stripped): 'tag' = route to Claude, 'tool' = a tool call (collect it whole),
    'maybe' = too short to tell, 'no' = speak it."""
    if head.startswith(ROUTE_TAG):
        return "tag"
    if head.startswith((TOOL_TAG, NATIVE_TAG)):
        return "tool"
    return "maybe" if any(t.startswith(head) for t in (ROUTE_TAG, TOOL_TAG, NATIVE_TAG)) else "no"


def token_label(got: str, labels=("client",)):
    """Which of `labels` this token belongs to (None if none), comparing digests in constant time."""
    digest, hit = hashlib.sha256(got.encode()).hexdigest(), None
    for label in labels:
        for h in HASHES[label]:
            if hmac.compare_digest(digest, h):
                hit = label
    return hit


def check_token(got: str, labels=("client",)):
    label = token_label(got, labels)
    if label is None:
        raise HTTPException(401, "bad token")
    return label


def bearer(headers) -> str:
    return headers.get("authorization", "").removeprefix("Bearer ").strip()


def auth(request: Request):
    check_token(bearer(request.headers))


def call_token(headers, query_token: str) -> str:
    """The client token for /v1/call: the Authorization header, else the legacy ?token= query."""
    return bearer(headers) or query_token


TAILNET = ipaddress.ip_network("100.64.0.0/10")
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def link_peer_ok(host: str, headers) -> bool:
    """/v1/gateway-link is tailnet-only and never proxied. Fail closed: a non-tailnet peer, a non-IP peer,
    or any proxy header (the public proxy on deneb is itself a tailnet peer) is refused."""
    if any(h in headers for h in PROXY_HEADERS):
        return False
    try:
        return ipaddress.ip_address(host) in TAILNET
    except ValueError:
        return False


def decode_wav(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_UPLOAD + 1024:
        raise HTTPException(400, "audio missing or longer than 60 s")
    try:
        wav, sr = sf.read(io.BytesIO(data), dtype="float32")
    except Exception:
        raise HTTPException(400, "body must be a WAV file")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != 16000:
        raise HTTPException(400, "audio must be 16 kHz")
    return wav


async def transcribe(wav: np.ndarray) -> str:
    if len(wav) < MIN_STT_S * 16000:
        return ""
    async with asr_lock:  # one GPU decode at a time
        try:
            out = await asyncio.to_thread(asr, {"raw": wav, "sampling_rate": 16000},
                                          generate_kwargs=STT_QUALITY if stt_quality[0] else STT_KWARGS)
        except (TypeError, ValueError) as e:
            if not stt_quality[0]:
                raise
            print("whisper quality settings refused, using the plain ones:", type(e).__name__, e, flush=True)
            stt_quality[0] = False
            out = await asyncio.to_thread(asr, {"raw": wav, "sampling_rate": 16000}, generate_kwargs=STT_KWARGS)
    return clean_transcript(out["text"])


async def llm_stream(messages, max_tokens=600):
    body = {"model": LLM_MODEL, "messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.7}
    async with client.stream("POST", LLM_URL, json=body) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content")
            if delta:
                yield delta


# Reasoning models (Qwen with thinking on, some NIM models) may stream <think>…</think> before the answer, or only
# the closing tag (the chat template opened it). It must never be spoken, captioned or stored. Thinking stays on (it
# helps the answers); the "thinking" state simply lasts while nothing speakable has arrived.
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
THINK_FALLBACK = "생각이 길어져서 답을 못 정했어요. 한 번만 더 말씀해 주시겠어요?"
_HANGUL = re.compile(r"[가-힣]")


class Unthink:
    """Streamed text with reasoning removed: `feed(delta)` returns what may be spoken now, `close()` the rest.

    Before the first tag or Hangul, text is held (a stray `</think>` means all of it was reasoning). Inside a think
    block nothing comes out. A trailing partial tag ("<thi") is held until the next delta decides. `thought` tells
    whether any reasoning was dropped."""

    def __init__(self):
        self.buf, self.inside, self.decided, self.thought = "", False, False, False

    def feed(self, delta: str) -> str:
        self.buf += delta
        out = ""
        while True:
            if self.inside:
                i = self.buf.find(THINK_CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(THINK_CLOSE) - 1):]  # may be the start of a split closing tag
                    return out
                self.buf, self.inside, self.decided, self.thought = self.buf[i + len(THINK_CLOSE):], False, True, True
                continue
            o, c = self.buf.find(THINK_OPEN), self.buf.find(THINK_CLOSE)
            if c >= 0 and (o < 0 or c < o):  # a closing tag without its opening: everything held before it was reasoning
                self.buf, self.decided, self.thought = self.buf[c + len(THINK_CLOSE):], True, True
                continue
            if o >= 0:
                out += self.buf[:o] if self.decided else ""
                self.buf, self.inside, self.thought = self.buf[o + len(THINK_OPEN):], True, True
                continue
            if not self.decided:
                if not _HANGUL.search(self.buf):
                    return out  # still possibly untagged reasoning: hold
                self.decided = True
            keep = next((k for k in range(min(len(THINK_CLOSE) - 1, len(self.buf)), 0, -1)
                         if THINK_OPEN.startswith(self.buf[-k:]) or THINK_CLOSE.startswith(self.buf[-k:])), 0)
            out, self.buf = out + self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
            return out

    def close(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        if self.inside:
            self.thought = True
        return rest


async def unthink(stream, f: Unthink):
    async for d in stream:
        if t := f.feed(d):
            yield t
    if t := f.close():
        yield t


async def gateway_settings():
    return links.settings  # pushed by the gateways on connect and on every change


ROUTING = {"type": "activity", "phase": "routing", "label": "Claude에게 물어보는 중", "tool": "", "kind": "generic",
           "route": "claude"}  # only when a turn goes to Claude; a Qwen turn sends no activity at all
KINDS = {"calendar", "mail", "memory", "schedule", "server", "files", "web", "terminal", "notes", "generic"}


def client_activity(ev):
    """The app-facing activity event for Claude's tools: phase, label, tool, kind, and the detail fields of
    tool-activity-contract.md when the gateway sends them (typed, clipped and redacted again here)."""
    kind = ev.get("kind") if ev.get("kind") in KINDS else "generic"
    out = {"type": "activity", "phase": str(ev.get("phase", "")), "label": str(ev.get("label", "")),
           "tool": str(ev.get("tool", "")), "kind": kind, "route": "claude"}
    if isinstance(ev.get("call_id"), str) and re.fullmatch(r"[\w-]{1,40}", ev["call_id"]):
        out["call_id"] = ev["call_id"]
    for k in ("host", "task_id"):
        if isinstance(ev.get(k), str) and ev[k]:
            out[k] = ev[k][:64]
    if isinstance(ev.get("input"), str):
        out["input"] = clip_head(redact(ev["input"]))
    if isinstance(ev.get("chunk"), str):
        out["chunk"] = clip_tail(redact(ev["chunk"]), 2048)[0]
    if isinstance(ev.get("output"), str):
        out["output"], cut = clip_tail(redact(ev["output"]))
        if cut or ev.get("truncated") is True:
            out["truncated"] = True
    if isinstance(ev.get("ok"), bool):
        out["ok"] = ev["ok"]
    for k in ("exit_code", "duration_ms"):
        if isinstance(ev.get(k), int) and not isinstance(ev.get(k), bool):
            out[k] = ev[k]
    return out


async def claude_stream(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
    """Text deltas from a gateway over its link; RuntimeError carries a speakable message.

    `context` (recent turns of the call) goes only to full-mode links: earlier turns may be personal."""
    lk = links.pick(personal)
    text = (context if lk.mode == "full" else "") + text
    rid, q, finished = uuid.uuid4().hex, asyncio.Queue(), False
    links.queues[rid] = (q, lk)
    try:
        await lk.send({"type": "ask", "id": rid, "text": text, "session": session, "model": model, "task": task,
                       "persona": (CALL_PERSONA.get() or {}).get("id")})
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                lk.fails += 1
                raise RuntimeError("Claude 답이 너무 늦어서 끊었어요.")
            if ev["type"] == "activity":
                if on_event:
                    await on_event(client_activity(ev))
            elif ev["type"] == "text":
                yield str(ev.get("delta", ""))
            elif ev["type"] == "error":
                finished = True
                if not ev.get("code"):  # budget/personal refusals are answers, not a sick gateway
                    lk.fails += 1
                raise RuntimeError(ev.get("message", "Claude를 쓸 수 없어요."))
            elif ev["type"] == "done":
                finished, lk.fails = True, 0
                return
    finally:
        links.queues.pop(rid, None)
        if not finished and lk in links.links:  # barge-in or timeout: stop the Claude run too
            try:
                await lk.send({"type": "cancel", "id": rid})
            except Exception:
                pass


# ---------- briefing on request: WS /v1/call?intent=briefing ----------
BRIEFING_QWEN = (
    "너는 개인 비서 '{name}'야. 사용자가 오늘 브리핑을 요청했어. 아래 메모와 사실만 근거로 30~40초 분량, "
    "여섯에서 여덟 문장의 음성 브리핑을 해. '오늘 브리핑이에요.'로 시작하고 오늘 날짜를 말한 뒤 수업·회의, 마감, 할 일, "
    "서버 상태, 승인 대기, 다음 예약 전화 순으로 말하고, 없는 항목은 건너뛰어. 메모에 없는 건 지어내지 마. "
    "한국어(말투는 비서 성격대로, 정해져 있지 않으면 존댓말), 마크다운·목록·이모지 금지."
)
BRIEFING_CLAUDE = (  # the morning-briefing call's prompt, for when no situation note exists yet
    "사용자가 오늘 브리핑을 요청했어. 오늘 수업과 마감(GISTRA), 오늘 할 일(Planit), 캘린더 일정, 서버 상태(Cygnus "
    "인프라), 승인 대기를 확인해서 '오늘 브리핑이에요'로 시작하는 30초 안쪽 브리핑으로 말해."
)


async def briefing_sentences(session, name, on_event, persona="", title=""):
    """Sentences of a spoken briefing. Fast path: the local Qwen composes it from the situation note a full
    gateway hands over (first sentence within about 2 s). No note yet: a filler, then Claude via the link."""
    try:
        ctx = await links.request({"type": "briefing_context"}, timeout=2)
    except (RuntimeError, asyncio.TimeoutError):
        ctx = {}
    if ctx.get("situation"):
        facts = "\n".join(str(f) for f in ctx.get("facts") or [])
        messages = [{"role": "system", "content": BRIEFING_QWEN.format(name=name) + (" " + character_line(name, persona, title) if persona else "")},
                    {"role": "user", "content": f"{now_kst().strip()}\n\n메모:\n{ctx['situation']}\n\n사실:\n{facts}"}]
        stream = llm_stream(messages, 700)
    elif time.time() - last_claude_briefing[0] < BRIEFING_COOLDOWN_S and last_claude_briefing[1]:
        for s in split_ready(last_claude_briefing[1], final=True)[0]:  # asked again soon: no second Claude run
            yield s
        return
    elif local_now():  # no situation note and no Claude in local mode: say so
        yield LOCAL_BRIEFING
        return
    else:
        yield FILLER
        await on_event(dict(ROUTING))
        stream = claude_stream(BRIEFING_CLAUDE, session, None, "schedule", personal=True, on_event=on_event)
    buf, said, failed = "", [], False
    try:
        async for delta in unthink(stream, Unthink()):
            buf += delta
            done, buf = split_ready(buf, final=False)  # whole sentences: a lone clause leaves a hole after it
            for s in done:
                said.append(s)
                yield s
    except RuntimeError as e:  # no gateway / Claude failed: say why
        buf, failed = str(e), True
    rest = split_ready(buf, final=True)[0]
    if not ctx.get("situation") and not failed:  # remember a whole Claude briefing for the cooldown
        last_claude_briefing[:] = [time.time(), " ".join(said + rest)]
    for s in rest:
        yield s


BRIEFING_COOLDOWN_S = 600  # at most one Claude-fallback briefing per 10 min; within it the last text is reused
last_claude_briefing: list = [0.0, ""]


# ---------- scheduled calls: "내일 10시에 깨워 줘" (local; the gateway stores and fires them) ----------
KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
          "열한": 11, "열두": 12, "삼십": 30, "십": 10, "이십": 20, "사십": 40, "오십": 50}
_NATIVE = r"열한|열두|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉"
_SINO = r"[이삼사오]?십[일이삼사오육칠팔구]?|[일이삼사오육칠팔구]"  # 오, 십오, 이십오; "일"/"이" also mean work/this, so units must follow
_N = rf"(\d{{1,2}}|{_NATIVE}|{_SINO})"
_BEFORE_UNIT = rf"(\d{{1,2}}|{_NATIVE}|(?<![가-힣])(?:{_SINO}))"  # a Sino numeral never starts mid-word
# "오시면" (if you come) is not 5 o'clock: a Sino hour is not followed by a verb ending
_SI = r"시(?!간|[면는고다어지겠죠니오세려라길서가])"
# a native hour never starts mid-word either ("아이디어에 대한 시장" read "한 시" as 1 o'clock), except after 오후/밤…
_NATIVE_HOUR = rf"(?:(?<![가-힣])|(?<=오전|오후|아침|저녁|새벽)|(?<=밤|낮))(?:{_NATIVE})"
_HOUR = (rf"(\d{{1,2}}|{_NATIVE_HOUR}|(?<![가-힣])(?:[이삼사오]?십[일이삼사오육칠팔구]?(?=\s*{_SI})|[삼오육칠팔](?=\s*{_SI})"
         rf"|[일이사구](?=\s+{_SI})))")  # "일시" (date/time) needs a space to be 1 o'clock
DAYS = "월화수목금토일"
REL_RE = re.compile(_BEFORE_UNIT + r"\s*(분|시간)\s*만?\s*(뒤|후|있다가|지나서)")
CLOCK_RE = re.compile(r"(오전|오후|아침|저녁|밤|새벽|낮)?\s*" + _HOUR + r"\s*시(?!간|[장작험청설민스])\s*(반|" + _N + r"\s*분)?")
WEEKDAY_RE = re.compile(r"(다음\s*주|담주|이번\s*주)?\s*([월화수목금토일])요일")
SCHED_LIST = re.compile(r"(예약|알람|모닝 ?콜|깨우는 거).{0,8}(목록|뭐|있|확인|알려|된 거|돼 있)")
SCHED_CANCEL = re.compile(r"(예약|알람|모닝 ?콜|깨우는 거|전화).{0,8}취소|취소.{0,6}(예약|알람)|^\s*취소(해\s*줘|해요|요)?[.!]?\s*$")
SCHED_WAKE = re.compile(r"깨워|모닝 ?콜|알람.{0,10}(맞춰|설정|해\s*줘|부탁)")
SCHED_CALL = re.compile(r"(전화|콜)\s*(해|걸어|주)|(예약|알람).{0,10}(해\s*줘|잡아|걸어|맞춰)")
OTHER_TARGET = re.compile(r"(\S+)(한테|에게|께)")
# A gateway tool that read back what it would change ("…일정을 지울까요?") and waits: only a clear yes in the very next
# turn resends its args ({confirm: id}); any other answer drops it (the id expires after 120 s on the gateway too).
pending_confirm: dict[str, tuple[float, str, dict]] = {}
CONFIRM_TTL_S = 120
CONFIRM_YES = re.compile(r"\s*((응|어|네|예|그래)[\s,]*)?(지워|삭제해|추가해|넣어|바꿔|변경해|해)\s*(줘|주세요|줄래)?[.!~]*\s*")
pending_schedule: dict[str, tuple[float, dict]] = {}  # session -> (asked at, partial request) after "몇 시에…?"


SINO_DIGITS = {c: i for i, c in enumerate("일이삼사오육칠팔구", 1)}


def ko_int(tok):
    if tok.isdigit():
        return int(tok)
    if tok in KO_NUM:
        return KO_NUM[tok]
    if "십" not in tok:  # Sino: 오, 십오, 이십오
        return SINO_DIGITS[tok]
    tens, ones = tok.split("십")
    return (SINO_DIGITS[tens] if tens else 1) * 10 + (SINO_DIGITS[ones] if ones else 0)


def schedule_intent(text, session=""):
    """'create' / 'list' / 'cancel' when the utterance is about scheduled calls, else None (normal routing)."""
    if SCHED_LIST.search(text):
        return "list"
    if SCHED_CANCEL.search(text):
        return "cancel"
    if SCHED_WAKE.search(text):
        return "create"
    target = OTHER_TARGET.search(text)
    if SCHED_CALL.search(text) and (not target or target.group(1) in ("나", "저")) and parse_when(text)[0] \
            and not TASK_NOTIFY.search(text):  # "…다 끝나면 전화해줘" reports on work: a task, never a timed call
        return "create"  # "내일 3시에 전화해 줘"; not "엄마한테 전화해 줘"
    asked = pending_schedule.get(session)
    if asked and time.time() - asked[0] < 90:
        p = asked[1]
        if p.get("resend") and (YES.search(text) or NO.search(text)):
            return "create"  # the answer to "…맞죠?"
        if parse_when(text)[0] or (p.get("said") and DAY_PART.search(text)):
            return "create"  # the answer to "몇 시에 깨워 드릴까요?" or "아침 7시요, 저녁 7시요?"
    return None


def parse_when(text, now=None):
    """(datetime in KST or None, repeat or None) from a Korean time phrase."""
    now = now or datetime.now(KST)
    if m := REL_RE.search(text):
        n = ko_int(m.group(1))
        return (now + (timedelta(minutes=n) if m.group(2) == "분" else timedelta(hours=n))).replace(
            second=0, microsecond=0), None
    repeat = "daily" if "매일" in text else "weekdays" if re.search(r"평일|주중", text) else None
    if m := re.search(r"매주\s*([월화수목금토일])요일", text):
        repeat = f"weekly:{DAYS.index(m.group(1))}"
    clock = CLOCK_RE.search(text)
    if not clock:
        return None, repeat
    part, hour = clock.group(1), ko_int(clock.group(2))
    minute = 30 if clock.group(3) == "반" else ko_int(clock.group(4)) if clock.group(4) else 0
    if hour > 24 or minute > 59:
        return None, repeat
    if part in ("오후", "저녁", "밤", "낮") and hour < 12 and not (part == "낮" and hour >= 11):
        hour += 12
    elif part in ("오전", "아침", "새벽") and hour == 12:
        hour = 0
    hour %= 24
    day = None
    for word, add in (("글피", 3), ("모레", 2), ("내일", 1), ("오늘", 0)):
        if word in text:
            day = now.date() + timedelta(days=add)
            break
    if (m := WEEKDAY_RE.search(text)) and not repeat:
        want = DAYS.index(m.group(2))
        if m.group(1) and "다음" in m.group(1) or m.group(1) == "담주":
            day = now.date() + timedelta(days=7 - now.weekday() + want)
        else:
            day = now.date() + timedelta(days=(want - now.weekday()) % 7)
    at = datetime.combine(day or now.date(), datetime.min.time(), KST).replace(hour=hour, minute=minute)
    if day is None or repeat:  # "10시": today if still ahead, else tomorrow; repeats start at the next match
        while at <= now or (repeat == "weekdays" and at.weekday() >= 5) or \
                (repeat and repeat.startswith("weekly") and at.weekday() != int(repeat.split(":")[1])):
            at += timedelta(days=1)
    return at, repeat


def to_kst(dt):
    """A naive time (as Qwen may write it) means KST, never the container's own zone."""
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


async def schedule_reply(text, action, session, now=None):
    """What to say for a schedule utterance. The regexes only decide that it is one; the user's words then go to
    the same call_user / schedule_list / schedule_cancel tools every model path uses, the gateway resolves the time
    (late_night, cron), and the tool's own spoken result is said. No other code path writes schedules."""
    now = now or datetime.now(KST)
    if action == "list":
        msg, tool = {"type": "schedule", "action": "list"}, ("schedule_list", {})
    elif action == "cancel":  # the same day rule as creating (late_night): "내일 7시 취소" at 01:00 is today's
        at = late_night.when_from_words(text, now)["at"] if late_night.CLOCK_RE.search(text) else None
        msg = {"type": "schedule", "action": "cancel", **({"at": at.timestamp()} if at else {})}
        # a time that is still ambiguous ("내일 7시" at night) is passed as words, never as "the soonest one"
        tool = ("schedule_cancel", {"at": at.isoformat()} if at else
                {"description": text} if late_night.CLOCK_RE.search(text) else {})
    else:  # the user's own words go to the gateway, which resolves them (late_night, cron) like on every path
        prior = pending_schedule.pop(session, (0, {}))[1]
        if prior.get("resend"):
            if NO.search(text):
                return "알겠어요, 예약하지 않을게요."
            if YES.search(text):
                return await schedule_tool("call_user", prior["resend"], session, prior)
            prior = {}
        kind = "wake" if SCHED_WAKE.search(text) else prior.get("kind", "reminder")
        if prior.get("said") and late_night.CLOCK_RE.search(text):  # a new time: keep only the day it was for
            word = late_night.day_word(prior["said"])[0]
            said = f"{word} {text}" if word and not late_night.day_word(text)[0] else text
        else:
            said = f"{prior.get('said', '')} {text}".strip()
        note = "" if kind == "wake" else (prior.get("reason") or text[:100])
        args = {"reason": note or "깨우는 전화", "kind": kind,
                **({"repeat_text": said} if REPEAT_WORDS.search(said) else {"when_text": said})}
        return await schedule_tool("call_user", args, session, {"kind": kind, "said": said, "reason": note})
    if tool[0] in {t["name"] for t in gw_tools["defs"]}:
        try:
            return await gateway_tool(*tool)  # list and cancel: their result is already one spoken sentence
        except (RuntimeError, asyncio.TimeoutError):
            return "지금은 예약을 받을 수 없어요."
    try:  # shortcut: an older gateway without the tool registry; drop once every gateway serves tool_call
        reply = await links.request(msg)
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 예약을 받을 수 없어요."
    return str(reply.get("say") or "지금은 예약을 받을 수 없어요.")


# A whole short answer only: "어제 뭐 했지?", "어떻게 해?", "네가 해 줘", "노래 틀어 줘" are not answers to "…맞죠?".
YES = re.compile(r"^\s*(응+|어+|네+|넵|예|그래(요)?|맞아(요)?|좋아(요)?|ㅇㅇ|그렇게 ?해 ?(줘|주세요)?)\s*[.!?~]*\s*$")
NO = re.compile(r"^\s*(아니(요|야)?|아뇨|노+|취소(해 ?(줘|주세요))?|됐어(요)?|말고|안 ?해(요)?)\s*[.!?~]*\s*$")
DAY_PART = re.compile(r"오전|오후|아침|저녁|밤|새벽|낮|점심")
REPEAT_WORDS = re.compile(r"매일|평일|주말|주중|매주|매월|매달|마다|[월화수목금토일]{2,}\s*\d|[월화수목금토일]{2,}\s*[한두세네다열]")


async def schedule_tool(name, args, session, keep):
    """A schedule tool for the fast path: speak the gateway's own words; hold its question for the answer."""
    if name not in {t["name"] for t in gw_tools["defs"]}:
        return "지금은 예약을 받을 수 없어요."
    try:
        msg = await gateway_tool_msg(name, args)
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 예약을 받을 수 없어요."
    needs = msg.get("needs") if isinstance(msg.get("needs"), dict) else None
    if needs:
        pending_schedule[session] = (time.time(), {**keep, **({"resend": needs["args"]} if needs.get("args") else {})})
    return str(msg.get("spoken") or msg.get("result") or "지금은 예약을 받을 수 없어요.")[:300]


# ---------- background tasks: "…해 줘, 끝나면 알려 줘" (a full gateway runs them, gateway.py "tasks") ----------
TASK_NOTIFY = re.compile(r"(끝나면|다 ?되면|완료되면|마치면|끝내고|끝나고).{0,12}(알려|전화|연락|말해|불러|깨워)")
_DEV = {"맥북": "macbook", "macbook": "macbook", "알비레오": "albireo", "albireo": "albireo", "기에나": "gienah",
        "gienah": "gienah", "데네브": "deneb", "deneb": "deneb", "스파크": "spark", "spark": "spark",
        "코어": "core", "vd-core": "core"}
DEVICE_JOB = re.compile(r"(" + "|".join(_DEV) + r")\S*\s.{0,30}(정리|설치|업데이트|빌드|백업|돌려|분석|조사|옮겨|만들어|고쳐)"
                        r"\S*\s*(해\s*)?(줘|주세요|줄래)", re.I)


# "자기 전에 이거 해 둬", "밤새 ○○ 조사해 놔": work for the overnight queue (runs at night, told in the morning)
OVERNIGHT = re.compile(r"(자기 ?전에|밤새|밤사이|자는 ?동안|내일 ?아침까지).{0,40}(해 ?둬|해 ?놔|해 ?놓|해 ?줘|조사|정리|찾아|알아봐|만들어)")


TASK_FILLER = re.compile(r"그러고|그리고|그다음(에)?|그 다음(에)?|그럼|그거|그 작업|작업|다|나한테|저한테|좀|[\s.,!?~]")


def task_intent(text):
    """None, or how to report back ("call" / "push") when the request is a background task: an explicit
    "끝나면 알려 줘", or a job on a named device (several steps; the caller should not wait on the line)."""
    if (m := TASK_NOTIFY.search(text)) and len(TASK_FILLER.sub("", text[:m.start()] + text[m.end():])) < 4:
        return None  # "그러고 끝나고 전화해줘" alone has no work in it: the model answers it from the conversation
    if TASK_NOTIFY.search(text):
        return "push" if re.search(r"푸시|알림|메시지|문자", text) else "call"
    if OVERNIGHT.search(text):
        return "push"  # told in the morning report; nobody is woken
    return "call" if DEVICE_JOB.search(text) else None


# "알아서 해": the user's own words turn on auto-approve, read back once (user decision 2026-11-22). Handled here, not
# by Qwen: Qwen skipped the read-back and told the user to check the app, so the grant never started. The grant goes
# the way the app's toggle does (auto_grant), so a device output read a moment ago cannot block the user's own words.
AUTO_ON = re.compile(r"알아서\s*(해|하|진행|처리|실행|돌려)|묻지\s*말고|물어보지\s*말고|자동\s*(으로\s*)?(승인|실행|허용)")
AUTO_OFF = re.compile(r"자동\s*(으로\s*)?(승인|실행|허용)\s*(꺼|끄|그만|멈|중지|해제)|이제\s*(하나하나|매번)\s*물어")
AUTO_Q = "이번 통화 동안 위험하지 않은 명령은 묻지 않고 바로 실행할게요. 지우기나 설치 같은 건 여전히 여쭤볼게요. 맞죠?"
AUTO_GO = re.compile(r"진행|맡겨|시작|해서|하고")
AUTO_MAX_CHARS = 30  # longer utterances carry a request of their own ("재시작하고 알아서 해"): the model answers those
pending_auto: dict[str, float] = {}  # call key -> when the read-back was asked


def auto_intent(text, key):
    """"on" / "off" / "answer" (to the read-back) / None."""
    asked = pending_auto.get(key)
    if asked and time.time() - asked < 90 and (YES.search(text) or NO.search(text)):
        return "answer"
    if len(text) > AUTO_MAX_CHARS or AUTO_GO.search(text):  # "자동 승인으로 해서 진행해줘": a yes to the task, for the model
        return None
    return "off" if AUTO_OFF.search(text) else "on" if AUTO_ON.search(text) else None


async def auto_reply(text, action, key):
    if action == "on":
        pending_auto[key] = time.time()
        return AUTO_Q
    pending_auto.pop(key, None)
    if action == "answer" and NO.search(text):
        return "알겠어요, 명령마다 승인을 받을게요."
    try:
        links.pick(personal=True, need_claude=False)
    except RuntimeError:
        return "지금은 자동 실행을 바꿀 수 없어요."
    await links.notify({"type": "auto_grant", "session": key, "value": action == "answer"})
    if action == "off":
        return "자동 실행을 껐어요. 이제 명령마다 승인을 받을게요."
    return "네, 이번 통화 동안 위험하지 않은 명령은 바로 실행할게요. 이미 앱에 올라간 요청은 직접 눌러 주세요."


async def task_reply(text, notify):
    if local_now():  # a background task runs on Claude: never in local mode
        return LOCAL_LINE
    dev = next((v for k, v in _DEV.items() if re.search(k, text, re.I)), None)
    msg = {"type": "task", "goal": text, "notify": notify, "persona": (CALL_PERSONA.get() or {}).get("id"),
           **({"device": dev} if dev else {}), **({"when": "overnight"} if OVERNIGHT.search(text) else {})}
    try:
        reply = await links.request(msg, need_claude=True)
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 작업을 맡을 수 없어요."
    return str(reply.get("say") or "지금은 작업을 맡을 수 없어요.")


_tts_cache: dict[str, tuple[bytes, int]] = {}
# Measured 2026-11-21 against the shared TTS server (sohee): ~0.15 s of synthesis per syllable (about 0.7x real
# time), one request at a time behind a GPU lock: two concurrent requests took longer than the same two in a row.
PLAY_TAIL_S = float(os.environ.get("PLAY_TAIL_MS", "300")) / 1000  # after the phone's playback ends, then listen
# The server slows down when the shared GPU is busy, so the rate is tracked from real calls (the slowest of the
# default and a moving average), and merging leaves a 1.5x margin.
SYNTH_S_PER_SYL = float(os.environ.get("SYNTH_MS_PER_SYL", "150")) / 1000
synth_rate = [SYNTH_S_PER_SYL]
AUDIO_S_PER_SYL = 0.24  # Qwen3-TTS Korean speech, measured 2026-11-22; tracked from real audio like synth_rate
audio_rate = [AUDIO_S_PER_SYL]
SYNTH_MARGIN = 1.4  # synthesis estimates are padded this much (the TTS box is shared: rates swing ~0.7-1.0x real time)
FUTURE_SYL = 25  # an LLM still writing: assume one more sentence of about this size will follow
HOLD_MAX_S = 3.0  # never hold ready audio back longer than this: a short pause mid-reply beats a long silence first
FIRST_MIN_SYL = 12  # an opening's second chunk is at least this long
MAX_CHUNK_SYL = 60  # merged chunks stay a few sentences long (prosody, and a cut-in loses less)


def synth_est(syl):
    """Seconds Qwen3-TTS needs for a chunk of `syl` syllables, padded (it serves one request at a time)."""
    return SYNTH_MARGIN * max(SYNTH_S_PER_SYL, synth_rate[0]) * max(syl, 8)


def gap_free(now, buffered_s, pending, inflight=None):
    """Would playback started `now` run without a gap? `buffered_s`: audio ready and not yet played; `inflight`:
    (started_at, syllables) of the chunk being synthesized; `pending`: syllables of the chunks still to synthesize,
    in order. Synthesis is sequential, so chunk k is ready at the sum of the synth times before it, and must be
    ready before the audio queued ahead of it runs out."""
    ready, plays_until = now, now + buffered_s
    for start, syl in ([inflight] if inflight else []) + [(None, p) for p in pending]:
        ready = (max(now, start + synth_est(syl)) if start is not None else ready + synth_est(syl))
        if ready > plays_until:
            return False
        plays_until += audio_rate[0] * syl
    return True


async def tts(sentence: str) -> tuple[bytes, int]:
    try:
        sentence = ko_read.speakable(sentence)  # "1,333원" is captioned as written and spoken "천삼백삼십삼 원"
    except Exception:  # a 25+ digit id outruns 만/억/조/경; speak it as written rather than lose the sentence
        pass
    p = CALL_PERSONA.get() or {}  # the call's persona: its speaker and optional style instruction
    voice, style = p.get("voice") or TTS_VOICE, p.get("voice_style") or ""
    # Fillers and short openers ("네, 알겠습니다.") repeat constantly: synthesize them once (per voice). Under
    # MIN_SYL is never cached (such a chunk is only ever a whole reply, and garbles more often).
    cacheable = sentence in FILLERS or MIN_SYL <= syllables(sentence) <= 8
    key = (voice, style, sentence)
    if cacheable and key in _tts_cache:
        return _tts_cache[key]
    body = {"input": sentence, "voice": voice, "response_format": "wav", **({"instructions": style} if style else {})}
    t = time.perf_counter()
    r = await client.post(TTS_URL, json=body)
    r.raise_for_status()
    wav, sr = sf.read(io.BytesIO(r.content), dtype="int16")
    if (n := syllables(sentence)) >= MIN_SYL:
        synth_rate[0] = 0.7 * synth_rate[0] + 0.3 * (time.perf_counter() - t) / n
        audio_rate[0] = 0.7 * audio_rate[0] + 0.3 * (len(wav) / sr) / n
    if cacheable and len(_tts_cache) < 500:
        _tts_cache[key] = (wav.tobytes(), sr)
    return wav.tobytes(), sr


def day_part(hour):
    """The spoken part of day: 00:xx is 밤 12시 and 12:xx 낮 12시, never 오전 12시."""
    return "밤" if hour == 0 else "낮" if hour == 12 else "오전" if hour < 12 else "오후"


def with_title(sentence, title):
    """'정훈님, …' at the start unless the title is already there (same rule as the gateway's openings)."""
    if not sentence or not title or title in sentence[:len(title) + 12]:
        return sentence
    return f"{title}, {sentence}"


def night_rule():
    """The gateway's late-night day rule for Qwen (late_night.prompt_rule): empty after 05:00."""
    return late_night.prompt_rule(datetime.now(KST))


def now_kst():
    now = datetime.now(KST)
    ampm, h12 = day_part(now.hour), (now.hour % 12 or 12)  # Qwen misreads 24-hour "17시"
    return f" 현재 시각: {now:%Y년 %-m월 %-d일} {'월화수목금토일'[now.weekday()]}요일 {ampm} {h12}시 {now.minute}분."


def ms(t0):
    return round((time.perf_counter() - t0) * 1000)


CUT_NOTE = "(직전 답은 사용자가 '(여기서 끊김)'까지만 들었어. 그 뒤에 네가 쓴 내용은 전달되지 않았으니, 말한 것으로 치지 마.)"


def recent_context(hist, n=6):
    turns = list(hist)[-n:]
    lines = [f"{'사용자' if m['role'] == 'user' else '비서'}: {m['content']}" for m in turns]
    # Claude's own transcript keeps the whole reply it wrote; this says how much of the last one was heard
    cut = any(m["role"] == "assistant" and m["content"].endswith(CUT_MARK) for m in turns[-2:])
    return ("최근 통화 내용:\n" + "\n".join(lines) + ("\n" + CUT_NOTE if cut else "") + "\n\n") if lines else ""


title_gap: dict[str, int] = {}  # history key -> replies since the user's title was last said


ACK_OPEN = re.compile(r"^\s*(?:네네|넵|네|예|알겠습니다|알겠어요|좋아요)\s*[,.!~]+\s*")


class Address:
    """The user's title (호칭): the call's first reply starts with it when the model left it out. Nothing else is
    rewritten: the model's own vocatives stay as it said them (user decision 2026-11-22, fewer filters)."""

    def __init__(self, title, hkey, first_reply):
        self.title, self.hkey, self.started, self.named, self.acked = title, hkey, False, False, False
        self.due = bool(title) and first_reply

    def __call__(self, s):
        """'' for a sentence that was only the title again ("네, 대표님."): the title at most once per reply (live
        2026-11-23: "네, 대표님" opened and ended every other sentence). One ack per reply: past the first sentence
        (or the lookup ack) a sentence never opens with "네," again, nor with the title (live 2026-11-23: a re-asked
        model began over with "네, 정훈님. 오늘 일정 안내해 드릴게요." mid-reply)."""
        first, self.started = not self.started, True
        if not first or self.acked:
            s = ACK_OPEN.sub("", s)
            if not first and self.title:
                s = re.sub(rf"^\s*{re.escape(self.title)}\s*[,.!~]+\s*", "", s)
            if not re.search(r"[가-힣A-Za-z0-9]", s):
                return ""
        t = self.title
        if not t:
            return s
        if self.named:
            s = re.sub(rf"^\s*(?:(?:네|예)\s*,\s*)?{re.escape(t)}\s*[,.!~]+\s*", "", s)
            s = re.sub(rf"\s*,\s*{re.escape(t)}(?=\s*[,.!?~]|\s*$)", "", s).strip()
            return s if re.search(r"[가-힣A-Za-z0-9]", s) else ""
        if first and self.due and t not in s:
            s = f"{t}, {s}"
        self.named = t in s
        return s

    def done(self):
        pass


async def respond(text, session, t0, timings, on_event=None, kept=None, speak=True):
    """Yield (sentence, route, pcm, sr) for one user utterance; records history as it goes. `kept` (a dict) gets
    the history and the assistant turn added, so the call can cut it down to what the phone actually played."""
    settings = await gateway_settings()
    persona = current_persona(settings)
    hkey = f"{persona['id']}:{session}"  # history and the Claude session map are per persona
    CALL_ORIGIN.set(hkey)
    hist = history[hkey]
    name = persona["name"]
    sig = (name, user_title(settings, persona))
    if history_sig.get(hkey, sig) != sig:
        hist.clear()  # the name or title changed since these turns: start this conversation fresh
    history_sig[hkey] = sig
    prior = list(hist)  # earlier turns only: this utterance is added to history below and sent once, as the last turn
    bare = strip_vocative(text, name)
    CALL_TURN.set(uuid.uuid4().hex[:12])
    CALL_CARDS.set((on_event, set()) if on_event else None)
    CALL_EVENTS.set(on_event)
    confirm = pending_confirm.pop(session, None)  # asked last turn: this turn answers it, yes or not
    confirm = confirm if confirm and time.time() - confirm[0] < CONFIRM_TTL_S and (
        YES.search(bare) or CONFIRM_YES.fullmatch(bare)) else None
    auto = None if confirm else auto_intent(bare, hkey)
    sched = None if auto or confirm else schedule_intent(bare, session)
    job = None if sched or auto or confirm else task_intent(bare)
    ctx = links.context_for(session)  # '' without a brain: the old prompt and routing
    gw_names = tuple(t["name"] for t in gw_tools["defs"])
    # The gateway's memory and people tools answer personal questions too: Qwen looks first, Claude only after
    local = bool(ctx) or "memory_recall" in gw_names
    to_claude, model = (False, None) if sched or job or auto or confirm else pre_route(
        bare, settings, local=local, tools=(tuple(links.brain_tools()) if ctx else ()) + gw_names)
    offline = (CALL_MODE.get() or settings.get("brain_mode") or "auto") == "local" or bool(CLAUDE_NO.search(bare))
    play = "" if sched or job or auto else await play_note(hkey, bare)  # Qwen plays, no hand-over
    if play:  # a game note is terse; the persona's own register and title still hold ("너 차례!" was 반말 for 아젤)
        play += " 말투(존댓말/반말)와 호칭은 평소 네 성격대로 지키고, 사용자를 '당신'이라고 부르지 마."
    stay = not offline and not (sched or job or auto or confirm) and bool(SCHEDULE_TURN.search(bare)) \
        and not USE_CLAUDE.search(bare) and not pick(MODEL_WORDS, bare)  # unless Claude was asked for by name
    if offline or play or stay:
        to_claude = False  # local mode: Qwen answers what it can and says plainly what it can't
    sentences: asyncio.Queue = asyncio.Queue()
    thinking: list[Unthink] = []  # one filter per model stream this turn; any dropped reasoning is remembered
    route = {"value": "auto" if auto else "schedule" if sched else "task" if job else "claude" if to_claude else "qwen"}
    address = Address(user_title(settings, persona), hkey, not any(m["role"] == "assistant" for m in hist))
    fetched = {"agenda": ""}  # agenda_prefetch's block, read by both model paths

    async def via_claude():
        offline_now = offline or local_now()  # also switched to local mid-turn
        if offline_now or stay:  # never Claude in local mode (say so, offer the switch) or for a schedule turn
            await sentences.put(LOCAL_LINE if offline_now else SCHEDULE_FAILED)
            return
        route["value"] = "claude"
        if on_event:
            await on_event(dict(ROUTING))  # before any filler
        task = pick(TASK_WORDS, text) or "chat"
        buf, got_text = "", asyncio.Event()

        async def nudge():  # at most one FILLER, then one FILLER_LONG; none once Claude's text arrives
            for wait, line in ((FILLER_AFTER_S, FILLER), (NUDGE_AFTER_S - FILLER_AFTER_S, FILLER_LONG)):
                try:
                    await asyncio.wait_for(got_text.wait(), wait)
                    return
                except asyncio.TimeoutError:
                    await sentences.put(line)
        nudger = asyncio.create_task(nudge())
        acted, claimed = {"ok": False}, {"n": 0}

        async def watch(ev):  # Claude, the API path and NIM all report their tool calls as activity
            if ev.get("phase") == "tool_start" and (action_tool(ev.get("tool", "")) or LOOKUP_TOOL.search(ev.get("tool", ""))):
                acted["ok"] = True  # a lookup grounds "이미 맡기셨어요" about work that exists
            if on_event:
                await on_event(ev)

        async def speak(s):  # the action-claim gate, for every model's final text (only when an action was asked)
            # Claude's own turn: its tool calls are watched, so only the wording is checked (a plain statement of fact
            # such as "그 작업은 이미 진행 중이에요" is not a claim). The any-statement rule is for the local model.
            if ACTION_CLAIM.search(s) and not acted["ok"] and asks_action(text):
                timings["unconfirmed"] = s[:80]
                claimed["n"] += 1
                if claimed["n"] == 1 and not claimed.get("said"):  # after a real answer the claim is just dropped
                    await sentences.put(CLAIM_FALLBACK)
                return
            claimed["said"] = True
            await sentences.put(s)
        try:
            personal = task == "schedule" or bool(PERSONAL.search(text))  # this utterance only, not history
            thinking.append(Unthink())  # Claude/NIM output gets the same filter as Qwen's
            async for delta in unthink(claude_stream(text, hkey, model, task, personal, recent_context(prior) + fetched["agenda"],
                                                     on_event=watch), thinking[-1]):
                got_text.set()
                buf += delta
                done, buf = split_ready(buf, final=False)
                for s in done:
                    await speak(s)
        except RuntimeError as e:
            buf = str(e)
        except httpx.HTTPError:
            buf = NO_GATEWAY
        finally:
            nudger.cancel()
        for s in split_ready(buf, final=True)[0]:
            await speak(s)

    async def produce():
        try:
            if confirm:  # "응" to the read-back: the same call with its confirm id, and the gateway's own words
                try:
                    msg = await gateway_tool_msg(confirm[1], confirm[2])
                    done = str(msg.get("spoken") or msg.get("result") or msg.get("message") or "")
                except (RuntimeError, asyncio.TimeoutError):
                    done = "지금은 그 도구를 쓸 수 없어요."
                for s in split_ready(done[:300] or "지금은 그 도구를 쓸 수 없어요.", final=True)[0]:
                    await sentences.put(s)
                return
            if sched:  # handled here and by a full gateway; neither Qwen's answer nor Claude
                for s in split_ready(await schedule_reply(bare, sched, session), final=True)[0]:
                    await sentences.put(s)
                return
            if auto:  # "알아서 해": read back once, then the same grant as the app's toggle
                for s in split_ready(await auto_reply(bare, auto, hkey), final=True)[0]:
                    await sentences.put(s)
                return
            if job:  # a background task: confirm it and let the caller go
                for s in split_ready(await task_reply(bare, job), final=True)[0]:
                    await sentences.put(s)
                return
            if not play:
                fetched["agenda"] = await agenda_prefetch(bare, gw_names, timings)
            if to_claude:
                return await via_claude()
            length = reply_length(settings)
            gw_defs = [*await gateway_tools(), GAME_TOOL]  # devices, call_user, create_task through the gateway; game here
            pend = pending_lookup.get(hkey)
            request_text = bare
            if pend and time.time() - pend[0] < 600 and (PUSH.search(bare) or not matched_tool(bare, gw_defs)) \
                    and (PUSH.search(bare) or REQUEST.search(bare) or needs_answer(bare)):
                request_text = pend[1]  # "진짜 하라고": still the sleep record asked a turn ago
            wanted = matched_tool(request_text, gw_defs)
            requested = bool(needs_answer(bare) or REQUEST.search(bare) or PUSH.search(bare))
            system = (local_prompt if offline or play else no_handover if stay else str)(system_prompt(
                name, length, ctx, persona_text(persona, settings), user_title(settings, persona))) \
                + (tool_text := gateway_tools_text(gw_defs, request_text)) + now_kst() + night_rule() + (memory_hits(await recall(bare, timings)) if ctx else "") \
                + fetched["agenda"] + play
            messages = [{"role": "system", "content": system}, *prior, {"role": "user", "content": text}]
            # No-tool turns pass through as the model said them (user decision 2026-11-22: "툴콜을 안 할 때의 안전장치는
            # 최대한 덜어내고 할루시네이션만 잡는 쪽으로"). Two checks remain, and each first re-asks Qwen once:
            # an action claimed with no action tool, and the user's own plans stated with no source.
            asked = needs_answer(bare)  # a real question (not "여보세요?", "응"): a bare interjection is asked again once
            rechecked, stubbed, notes = False, False, set()  # notes: our own re-ask texts, never a source
            announced, guessed, looked, tool_nudged, lead = [], [], [], False, []
            base_system = messages[0]["content"]

            def renote(note, said=""):
                """A correction for the next try only, in the system prompt: never a user or assistant turn the model
                could take for words said (it parroted the nudge, live 2026-11-23), and gone after this turn."""
                messages[0] = {"role": "system", "content": base_system + "\n\n[이번 답에만 해당] " + note + (
                    f" 이미 한 말: '{said}'. 반복하지 말고 이어서 말해." if said else "")}  # "찾아볼게요" / a figure before any lookup
            lookup_turn = bool(requested or wanted or LIVE_Q.search(bare) or WEB_LIVE.search(bare))  # anything to look up
            live = asked and bool(LIVE_Q.search(bare)) and any(t["name"] in ("web_search", "weather") for t in gw_defs)
            wants_lookup = bool(LIVE_Q.search(bare) or WEB_LIVE.search(bare) or agenda_range(bare, datetime.now(KST)))
            confirmed = {"ok": False}  # an action tool (call, schedule, task, device) succeeded in this turn
            acked = []  # the ack is said once per turn, however many lookups run

            async def lookups(picks):
                """Several read-only calls at once: (the lines, one result text), or None when every one failed."""
                picks = [p for i, p in enumerate(picks)  # one infra pick reads all three already: no 3 x 3
                         if not (p["name"] in INFRA_READ and any(q["name"] in INFRA_READ for q in picks[:i]))]
                lines = [TOOL_TAG + " " + json.dumps(p, ensure_ascii=False) for p in picks]
                looked.extend(lines)
                pending_lookup.pop(hkey, None)
                outs = await asyncio.gather(*(lookup(x) for x in lines))
                if all(o is None for o in outs):
                    return None
                if len(lines) == 1:
                    return lines, outs[0]
                return lines, "\n\n".join(f"[{json.dumps(p, ensure_ascii=False)}]\n{o or '가져오지 못했어요.'}"
                                            for p, o in zip(picks, outs))

            async def lookup(line, on_say=None):
                """Run a tool line. A read-only lookup gets an ack past LOOKUP_SLOW_S and gives up at LOOKUP_S (None);
                an action tool keeps its own wait (an approval can take minutes)."""
                name = (re.search(r'"name"\s*:\s*"([\w-]+)"', line) or [None, ""])[1]
                infra = [t["name"] for t in gw_defs if t["name"] in INFRA_READ]
                if name in INFRA_READ and len(infra) > 1:  # a server question reads the whole picture, not one part
                    job = asyncio.create_task(infra_read(infra, session))
                else:
                    job = asyncio.create_task(run_tool(line, session, on_say))
                if "__" in name or not read_only(name):  # device tools and actions: their own wait
                    return await job
                if not (await asyncio.wait({job}, timeout=LOOKUP_SLOW_S))[0] and not acked:
                    acked.append(1)
                    await sentences.put(LOOKUP_ACK)  # the lookup is already running: an ack, not a promise
                try:
                    return await asyncio.wait_for(job, LOOKUP_S - LOOKUP_SLOW_S)
                except asyncio.TimeoutError:
                    timings["tool_timeout"] = name
                    return None

            async def say(s):
                sources = ctx + fetched["agenda"] + "\n".join(m["content"] for m in messages if m["role"] == "user" and m["content"] not in notes)
                if not grounded(s, sources):
                    timings["ungrounded"] = s[:80]
                    raise Recheck("personal", s)
                if not confirmed["ok"] and unconfirmed_claim(s, bare):
                    timings["unconfirmed"] = s[:80]
                    raise Recheck("action", s)
                if not looked and live and (FIGURE.search(s) or LISTING.search(s)):
                    guessed.append(s)  # today's figure or a listing claim before any lookup: from memory, never said
                    return
                if TOOL_TALK.search(s):
                    return  # "바로 도구를 불러서 찾아볼게요": our nudge parroted, never said
                promise = is_promise(s, wants_lookup) or (future(s) and bool(LOOKUP_VERB.search(s))) or \
                    (requested and wanted and future(s))
                if promise and (looked or not lookup_turn or fetched["agenda"] and (AGENDA_WORDS.search(s) or not LOOKUP_OBJ.search(s))):
                    return  # after the result, or in chat with nothing to look up ("내일 다시 확인해 드릴게요"): dropped
                if promise:
                    announced.append(s)  # held: a promise is never spoken before its result exists
                    return
                if not looked and lookup_turn and CHECKED.search(s):
                    guessed.append(s)  # "확인해 봤는데 정상" with no tool: never said, the lookup runs instead
                    return
                if not looked and wants_lookup and REFUSED_LOOKUP.search(s):
                    timings["refused_lookup"] = s[:80]
                    guessed.append(s)  # "can't look that up" with the tool right there: never said, the lookup runs
                    return
                if LEAD_IN.search(s):
                    lead.append(s)  # "다음과 같아요." waits for what follows it
                    return
                for x in lead:
                    await sentences.put(x)
                lead.clear()
                await sentences.put(s)

            # tool-first: the decision runs beside Qwen's own first answer, which is held (not spoken) until it is in
            ro_tools = [t for t in gw_defs if "__" not in t["name"] and t.get("read_only") is True]
            first_stream = None
            # every turn but a bare "응"/"고마워": "네모클로 응답 안 한대" is a statement and still needs the data
            if ro_tools and not play and not TRIVIAL.fullmatch(bare.strip()):
                pump_q: asyncio.Queue = asyncio.Queue()

                async def pump(msgs):
                    try:
                        async for d in llm_stream(msgs, LENGTH_TOKENS[length]):
                            await pump_q.put(d)
                    except Exception as e:  # noqa: BLE001  handed to the reader, raised where the stream is read
                        await pump_q.put(e)
                    finally:
                        await pump_q.put(None)
                # Qwen's own answer is held beside the router only when the words may well be chat: beside a likely
                # lookup it only slowed the router (2.0 s instead of 0.8 on the shared GPU, measured 2026-11-23)
                likely = bool(wanted or WEATHER_Q.search(bare) or LIVE_Q.search(bare) or WEB_LIVE.search(bare)
                              or INFRA_Q.search(bare) or MINE.search(request_text) or agenda_range(bare, datetime.now(KST)))
                pump_task = None if likely else asyncio.create_task(pump(list(messages)))
                ro_names = {t["name"] for t in ro_tools}
                # "내 주식": the gateway's portfolio (the holdings setting) answers every holding in one call; memory and a
                # quote per holding only when that tool is not there
                own_stocks = bool(MINE.search(request_text) and HOLDINGS_Q.search(request_text))
                portfolio = [{"name": "portfolio", "args": {}}] if own_stocks and "portfolio" in ro_names else None
                mine = not portfolio and bool(MINE.search(request_text)) and "memory_recall" in ro_names
                try:
                    t_route = time.perf_counter()
                    picks = portfolio or (None if mine else
                                          await asyncio.wait_for(route_lookup(request_text, prior, ro_tools), ROUTE_S))
                    timings["route_ms"] = ms(t_route)
                except Exception:  # noqa: BLE001  the router only advises: any failure means undecided
                    picks = None  # undecided: the turn goes on as before
                if picks or mine:
                    if pump_task:
                        pump_task.cancel()
                    acked.append(1)
                    await sentences.put(LOOKUP_ACK)  # at once: the first sound comes with the decision, not the data
                    t_tool, found = time.perf_counter(), []
                    if mine:  # the user's own things: memory says what they are, then the domain tools look them up
                        got = await lookups([{"name": "memory_recall", "args": {"query": request_text[:200]}}])
                        if got:
                            found.append(got)
                            try:
                                picks = await asyncio.wait_for(route_lookup(
                                    f"{request_text}\n\n[기억에서 찾은 것]\n{got[1][:1500]}\n(기억에 나온 대상마다 하나씩 조회해.)",
                                    prior, ro_tools), ROUTE_S)
                            except Exception:  # noqa: BLE001  advice only
                                picks = None
                            picks = [p for p in picks or [] if p["name"] != "memory_recall"]
                    if picks and (got := await lookups(picks)):
                        found.append(got)
                    if not found:
                        await sentences.put(LOOKUP_SLOW)  # never silence after a lookup started
                        return
                    timings["tool_first"] = ",".join(json.loads(x[len(TOOL_TAG):])["name"] for x in looked)[:80]
                    timings.setdefault("tools", []).append(ms(t_tool))
                    timings["result_at_ms"] = ms(t0)
                    # answering from data in hand needs no tool list: 4,300 characters less to read, the first word
                    # 0.7 s sooner (1.15 -> 0.41 s on the live Qwen, 2026-11-23)
                    base_system = base_system.replace(tool_text, "", 1)
                    messages[0] = {"role": "system", "content": base_system}
                    for lines, result in found:
                        messages += [{"role": "assistant", "content": "\n".join(lines)},
                                     {"role": "user", "content": f"[도구 결과]\n{result}"}]
                    messages[-1]["content"] += ("\n\n이 결과로 방금 말에 답해. 여러 항목이면 하나씩 빠짐없이 말하고, 기억에 있는데 "
                                                "결과가 없는 항목은 찾지 못했다고(비상장일 수 있다고) 말해.")
                elif pump_task:
                    async def from_pump():
                        while (d := await pump_q.get()) is not None:
                            if isinstance(d, Exception):
                                raise d
                            yield d
                    first_stream = from_pump()
            try:
                for rounds in range(4):  # at most two tool calls and one re-ask, then an answer
                    buf, state, said_round = "", "maybe", []
                    thinking.append(Unthink())
                    stream, first_stream = first_stream or llm_stream(messages, LENGTH_TOKENS[length]), None
                    try:
                        async for delta in unthink(stream, thinking[-1]):
                            timings.setdefault("llm_first_token_ms", ms(t0))
                            buf += delta
                            if state not in ("tool", "no"):
                                state = tag_state(buf.lstrip())
                            if state in ("maybe", "tool"):
                                continue  # might be a tag; a tool call is collected whole
                            if state == "tag" or ROUTE_TAG in buf:  # also "수요일이요. [CLAUDE]": hand over, never say it
                                if state != "tag" and (said_round or re.search(r"[가-힣]", buf.split(ROUTE_TAG)[0])):
                                    # already answered: keep what was said, drop the late tag
                                    buf = buf.split(ROUTE_TAG)[0]
                                    break
                                return await via_claude()
                            if TOOL_TAG in buf or NATIVE_TAG in buf:  # "찾아볼게요. [TOOL] {…}": say the words, collect the call
                                tag = TOOL_TAG if TOOL_TAG in buf and (NATIVE_TAG not in buf or buf.index(TOOL_TAG) <
                                                                       buf.index(NATIVE_TAG)) else NATIVE_TAG
                                before, _, after = buf.partition(tag)
                                for x in split_ready(before, final=True)[0]:
                                    said_round.append(x)
                                    await say(x)
                                buf, state = tag + after, "tool"
                                continue
                            speak, held = hold_tag_prefix(buf)  # a trailing "[CLA" might become the tag: keep it back
                            done, rest = split_ready(speak, final=False)
                            buf = rest + held
                            for x in done:
                                timings.setdefault("first_sentence_ms", ms(t0))
                                said_round.append(x)
                                await say(x)
                        if state == "tag":
                            return await via_claude()
                        if state == "tool" and (ctx or gw_defs) and rounds < 2:
                            t_tool = time.perf_counter()

                            async def tool_say(text):  # "앱에서 허용해 주세요" while an approval is pending
                                said_round.append(text)
                                await sentences.put(text)
                            line = (native_call(buf) or "") if NATIVE_TAG in buf else buf.strip().split("\n")[0]
                            announced.clear()  # a promise is never said: the ack covers a slow lookup instead
                            looked.append(line)
                            pending_lookup.pop(hkey, None)  # the request got its tool
                            if (result := await lookup(line, tool_say)) is None:
                                await sentences.put(LOOKUP_SLOW)
                                return
                            tool_name = (re.search(r'"name"\s*:\s*"([\w-]+)"', line) or [None, ""])[1]
                            if action_tool(tool_name) and not TOOL_REFUSED.search(result.strip()):  # side effects only
                                confirmed["ok"] = True
                            if tool_name == "end_call" and not TOOL_REFUSED.search(result.strip()):
                                return  # the goodbye was said before the call: another round said it twice
                            timings.setdefault("tools", []).append(ms(t_tool))
                            messages += [{"role": "assistant", "content": buf.strip()},
                                         {"role": "user", "content": f"[도구 결과]\n{result}\n\n이 결과로 방금 질문에 답해."}]
                            continue
                        if state == "tool" and (offline or stay) and rounds < 3:  # local: no Claude to hand to; answer with what came
                            renote("도구는 이제 그만 부르고, 지금까지 받은 결과로 바로 답해. 결과가 없는 항목은 없다고 말해.")
                            continue
                        if state == "tool" or ROUTE_TAG in buf:
                            return await via_claude()  # tools looping, or no brain, or a late tag: let Claude take it
                        for x in split_ready(hold_tag_prefix(buf)[0], final=True)[0]:
                            said_round.append(x)
                            await say(x)
                    except Recheck as e:  # one re-ask with a short correction; never cut the reply down to a filler
                        if rechecked:
                            raise Ungrounded if e.kind == "personal" else Unconfirmed
                        rechecked = True
                        renote(RECHECK[e.kind].format(s=e.sentence[:80]), " ".join(said_round[:-1]))
                        continue
                    if (announced or guessed) and not looked and rounds < 3:  # promised or guessed, looked nothing up
                        timings["announce_only"] = timings.get("announce_only", 0) + 1
                        timings.setdefault("raw_announce", buf[:200])  # the raw output, to see a mangled tool call
                        announced.clear(), guessed.clear()  # never said: no result exists yet
                        if not tool_nudged:
                            tool_nudged = True
                            hint = f" 이 요청에는 {wanted['name']}{arg_hint(wanted)}가 맞아." if wanted else ""
                            renote(TOOL_NUDGE + hint)
                            continue
                        names = {t["name"] for t in gw_defs}  # twice: look it up for Qwen, then let it answer
                        try:  # the same decision as tool-first, now with Qwen's own words as the hint it needs data
                            routed = await asyncio.wait_for(route_lookup(request_text, prior, ro_tools), ROUTE_S) \
                                if ro_tools else None
                        except Exception:  # noqa: BLE001  advice only
                            routed = None
                        if routed:
                            if (got := await lookups(routed)) is None:
                                await sentences.put(LOOKUP_SLOW)
                                return
                            messages += [{"role": "assistant", "content": "\n".join(got[0])},
                                         {"role": "user", "content": f"[도구 결과]\n{got[1]}\n\n이 결과로 방금 질문에 답해."}]
                            continue
                        span = agenda_range(bare, datetime.now(KST)) if "agenda" in names else None
                        pick_ = "agenda" if span else "weather" if WEATHER_Q.search(bare) and "weather" in names else \
                            "stock_quote" if re.search(r"주가|주식|종가|환율", bare) and "stock_quote" in names else \
                            wanted["name"] if wanted else "web_search" if "web_search" in names else None
                        if pick_:
                            args = {"from": span[0].isoformat(), "to": span[1].isoformat()} if pick_ == "agenda" else \
                                {} if pick_ == "weather" else infer_args(wanted, request_text) \
                                if wanted and pick_ == wanted["name"] else {"query": bare[:60 if pick_ == "stock_quote" else 200]}
                            line = TOOL_TAG + " " + json.dumps({"name": pick_, "args": args}, ensure_ascii=False)
                            looked.append(line)
                            pending_lookup.pop(hkey, None)
                            if (result := await lookup(line)) is None:
                                await sentences.put(LOOKUP_SLOW)
                                return
                            timings.setdefault("tools", []).append(0)
                            messages += [{"role": "assistant", "content": line},
                                         {"role": "user", "content": f"[도구 결과]\n{result}\n\n이 결과로 방금 질문에 답해."}]
                            continue
                        pending_lookup[hkey] = (time.time(), request_text)  # the user may push: "진짜 하라고"
                        await sentences.put(LOOKUP_FAILED)  # nothing to look it up with: say so, never just go quiet
                        break
                    if announced:  # still only a promise (no tool call, native or [TOOL]) after the tries above
                        pending_lookup[hkey] = (time.time(), request_text)
                        await sentences.put(LOOKUP_FAILED)
                        break
                    if requested and not stubbed and not confirmed["ok"] and said_round and all(map(future, said_round)):
                        stubbed = True  # "정리해 드릴게요." and nothing after it (live 2026-11-22): the content, now
                        timings["promise_only"] = " ".join(said_round)[:40]
                        renote(PROMISE_NOTE, " ".join(said_round))
                        continue
                    if asked and not stubbed and stub(" ".join(said_round), user_title(settings, persona)):
                        stubbed = True  # "아, 그거!" to a question: ask for the rest, once (live 2026-11-22, 서아)
                        timings["stub"] = " ".join(said_round)[:40]
                        renote(STUB_NOTE, " ".join(said_round))
                        continue
                    if lead and not stubbed:  # "다음과 같아요." was the end of it: ask for the list, once
                        stubbed = True
                        timings["lead_only"] = lead[0][:40]
                        renote(LEAD_NOTE, " ".join(x for x in said_round if x not in lead))
                        lead.clear()
                        continue
                    lead.clear()  # a lead-in with nothing after it twice: dropped, never said
                    break
            except Ungrounded:
                return await via_claude()  # twice unsourced: Claude checks the real data (local: says it can't)
            except Unconfirmed:  # never say an action happened when nothing did it
                if SCHED_CALL.search(bare) or SCHED_WAKE.search(bare):
                    pending_schedule[session] = (time.time(), {"kind": "wake" if SCHED_WAKE.search(bare) else "reminder"})
                    await sentences.put(ASK_WHEN)
                    return
                return await via_claude()
        finally:
            await sentences.put(None)

    producer = asyncio.create_task(produce())
    spoken, back, play_end = [], [], 0.0
    held, held_since, opened = [], [0.0], [False]  # synthesized audio not sent yet, and since when

    def buffered():
        """Seconds of audio ahead of the listener: still to play on the phone, or held here before playback."""
        return max(0.0, play_end - time.monotonic()) if opened[0] else sum(len(p) / 2 / r for _, p, r in held)
    hist.append({"role": "user", "content": text})

    uttered: set[str] = set()

    def repeat(x):
        """A sentence this reply already said (a re-asked or restarted model began again: "네, 수능! … 네, 수능!"),
        or a second announcement ("일정 알려드릴게요" again after a re-ask, live 2026-11-23)."""
        if x == LOOKUP_ACK:
            address.acked = True  # the answer after it does not say "네," again
        if x is None or x in FILLERS:
            return False
        if INTRO_ANNOUNCE.search(x):
            if uttered:  # "오늘 일정 안내해 드릴게요." after content: the reply started over
                return True
            uttered.add("\0announce")
        k = re.sub(r"[\W_]+", "", x)
        if len(k) >= 2 and k in uttered:
            return True
        uttered.add(k)
        return False

    async def next_chunk():
        """The next text to synthesize. Sentences that queued up while the last one was synthesized go out
        together (fewer, longer TTS calls), but only as many as the audio already sent covers: TTS is not
        streamed and runs at about 0.7x real time, so a chunk longer than that leaves the phone silent."""
        if back:
            s = back.pop()  # already addressed when it was set aside
        else:
            s = await sentences.get()
            while repeat(s):
                s = await sentences.get()
            if s is None or s in FILLERS:
                return s
            if not (s := ko_read.strip_foreign(s)) or (ko_read.english_only(s) and not ASKS_ENGLISH.search(text)):
                return await next_chunk()  # nothing but stray Chinese/Japanese, or a model's English working note
            if not (s := address(s)):
                return await next_chunk()  # only the title again: nothing to say
        if s is None or s in FILLERS:
            return s
        # as much as the audio ahead covers (the held audio before playback starts), never more than MAX_CHUNK_SYL.
        # A short first sentence stays its own chunk (its own caption): the start gate holds it until what
        # follows is ready, so no hole opens after it.
        room = min(MAX_CHUNK_SYL, buffered() / (SYNTH_MARGIN * max(SYNTH_S_PER_SYL, synth_rate[0])))
        while not back and not sentences.empty():
            nxt = sentences.get_nowait()
            if repeat(nxt):
                continue
            if nxt not in (None, FILLER, FILLER_LONG) and (
                    not (nxt := ko_read.strip_foreign(nxt)) or (ko_read.english_only(nxt) and not ASKS_ENGLISH.search(text))):
                continue
            nxt = nxt if nxt is None or nxt in FILLERS else address(nxt)
            if nxt == "":
                continue
            if nxt is None or nxt in FILLERS or syllables(s) + syllables(nxt) > room:
                back.append(nxt)
                break
            s = f"{s} {nxt}"
        return s

    def pending_syl():
        """Syllables of the sentences known but not synthesized yet, plus one assumed sentence while the LLM writes."""
        known = [x for x in [*back, *list(sentences._queue)] if x is not None and x not in FILLERS]
        more = not producer.done() and None not in back and None not in sentences._queue
        return [syllables(x) for x in known] + ([FUTURE_SYL] if more else [])

    def ready_to_play():
        """Start playback only once the held audio covers the synthesis of what follows: no gap mid-reply."""
        if not speak or time.monotonic() - held_since[0] >= HOLD_MAX_S or buffered() == 0:
            return True  # muted, held long enough, or nothing audible to protect
        return gap_free(time.monotonic(), buffered(), pending_syl())

    def take(s, pcm, sr):
        nonlocal play_end
        play_end = max(time.monotonic(), play_end) + len(pcm) / 2 / sr  # when the phone finishes this chunk
        if s not in FILLERS:
            spoken.append(s)
        return s, route["value"], pcm, sr

    try:
        while True:
            if held and ready_to_play():
                timings.setdefault("first_audio_ms", ms(t0))
                # held_since[0] is -1e9 once the gate gave up: that is "release now", not a time (held_ms was 10^12)
                timings.setdefault("held_ms", round((time.monotonic() - held_since[0]) * 1000) if held_since[0] > 0 else 0)
                while held:
                    yield take(*held.pop(0))
                opened[0] = True
            try:  # while audio is held, re-check the gate now and then: the LLM may be slow to send the rest
                s = await (asyncio.wait_for(next_chunk(), max(0.05, HOLD_MAX_S - (time.monotonic() - held_since[0])))
                           if held else next_chunk())
            except asyncio.TimeoutError:
                held_since[0] = -1e9  # held long enough: play what there is
                continue
            if s is None:
                break
            if not speak and s in FILLERS:
                continue  # a muted (typed) turn: captions only, and "잠시만요" is not a caption
            if held and not opened[0] and s not in FILLERS and \
                    time.monotonic() - held_since[0] + synth_est(syllables(s)) > HOLD_MAX_S:
                back.append(s)  # synthesizing this first would hold the ready audio too long (live: 13 s of silence)
                held_since[0] = -1e9
                continue
            pcm, sr = await tts(s) if speak else (b"", 24000)
            if opened[0] or s in FILLERS:
                timings.setdefault("first_audio_ms", ms(t0))
                yield take(s, pcm, sr)  # playing already (its buffer is on the phone), or a filler: as it comes
            else:
                if not held:
                    held_since[0] = time.monotonic()
                held.append((s, pcm, sr))
        while held:  # the reply is complete: everything synthesized plays back to back
            timings.setdefault("first_audio_ms", ms(t0))
            yield take(*held.pop(0))
        await producer  # surfaces LLM errors
        if not spoken and any(f.thought for f in thinking):  # the model only reasoned: a short line, not silence
            pcm, sr = await tts(THINK_FALLBACK) if speak else (b"", 24000)
            spoken.append(THINK_FALLBACK)
            yield THINK_FALLBACK, route["value"], pcm, sr
    finally:
        producer.cancel()
        # Only what was actually spoken goes into history (a barge-in cuts it short). Nothing spoken: no assistant
        # turn at all; a placeholder text would leak into captions and Qwen would parrot it.
        if spoken:
            hist.append({"role": "assistant", "content": " ".join(spoken)})
            address.done()
            if kept is not None:
                kept.update(hist=hist, entry=hist[-1])


app = FastAPI(title="assistant-voice")


@app.get("/health")
def health():
    return {"ok": True, "stt": STT_MODEL, "llm": LLM_MODEL, "voice": TTS_VOICE,
            "gateways": [{"device": lk.device, "mode": lk.mode, "priority": lk.priority, "fails": lk.fails}
                         for lk in links.links]}


@app.post("/v1/audio/transcriptions", dependencies=[Depends(auth)])
async def transcriptions(file: UploadFile):
    return {"text": await transcribe(decode_wav(await file.read()))}


@app.post("/v1/turn", dependencies=[Depends(auth)])
async def turn(request: Request, session: str = "default"):
    t0 = time.perf_counter()
    wav = decode_wav(await request.body())

    async def events():
        timings = {"audio_s": round(len(wav) / 16000, 2)}
        text = await transcribe(wav)
        timings["stt_ms"] = ms(t0)
        yield json.dumps({"type": "transcript", "text": text, "stt_ms": timings["stt_ms"]}, ensure_ascii=False) + "\n"
        reply, seq, acts = [], 0, []

        async def on_event(ev):
            acts.append(ev)
        if text:
            async for s, route, pcm, sr in respond(text, session, t0, timings, on_event):
                while acts:
                    yield json.dumps(acts.pop(0), ensure_ascii=False) + "\n"
                reply.append(s)
                yield json.dumps({"type": "audio", "seq": seq, "text": s, "route": route, "sr": sr,
                                  "pcm16": base64.b64encode(pcm).decode()}, ensure_ascii=False) + "\n"
                seq += 1
        for ev in acts:
            yield json.dumps(ev, ensure_ascii=False) + "\n"
        timings["total_ms"] = ms(t0)
        yield json.dumps({"type": "done", "reply": " ".join(reply), "timings": timings}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


# Voice activity: Silero's probability AND a margin over the call's own noise floor (background noise, other
# people's voices far away). Tunable; `noise_mode: noisy` (shared setting) raises the bar and turns voice
# barge-in off during playback (tap-to-interrupt still works).
VAD_MODES = {
    "normal": {"threshold": float(os.environ.get("VAD_THRESHOLD", "0.5")),
               "margin_db": float(os.environ.get("NOISE_MARGIN_DB", "10")),
               "min_voiced_ms": int(os.environ.get("MIN_VOICED_MS", "250")),
               "barge_voiced_ms": int(os.environ.get("BARGE_MIN_VOICED_MS", "500")), "barge": True},
    "noisy": {"threshold": float(os.environ.get("NOISY_VAD_THRESHOLD", "0.7")),
              "margin_db": float(os.environ.get("NOISY_MARGIN_DB", "15")),
              "min_voiced_ms": int(os.environ.get("NOISY_MIN_VOICED_MS", "400")),
              "barge_voiced_ms": 10**9, "barge": False},
}


class Turn:
    """Server-side end-of-speech detection over a stream of 16 kHz PCM16 bytes, gated by an adaptive noise floor."""
    FRAME = 512
    FLOOR_MIN = 30.0  # int16 RMS; digital silence must not make every breath "10 dB above the floor"
    FLOOR_START_MAX = 300.0  # a quiet room; speech is roughly 2000-8000

    def __init__(self, vad, end_silence_ms=END_SILENCE_MS, threshold=None, mode="normal"):
        self.vad = vad
        self.set_mode(mode)
        if threshold is not None:
            self.threshold = threshold
        self.end_frames = max(1, int(end_silence_ms / 1000 * 16000 / self.FRAME))
        self.pre = int(0.3 * 16000 / self.FRAME)
        self.pending = b""
        self.ring, self.speech, self.silent, self.voiced = [], [], 0, 0
        self.floor, self.last_voiced_s = None, 0.0
        self.hold = False  # inside a held recording: its own pauses never end the turn (set per message by the caller)

    def set_mode(self, mode):
        m = VAD_MODES.get(mode, VAD_MODES["normal"])
        self.mode, self.threshold, self.margin = mode if mode in VAD_MODES else "normal", m["threshold"], \
            10 ** (m["margin_db"] / 20)
        self.min_voiced_s, self.barge_voiced_s, self.barge = m["min_voiced_ms"] / 1000, m["barge_voiced_ms"] / 1000, m["barge"]

    def loud_enough(self, frame):
        """Track the noise floor (down fast, up slowly, so steady babble becomes the floor within seconds while a
        sentence barely moves it) and say whether this frame stands out from it by the margin."""
        x = np.frombuffer(frame, np.int16).astype(np.float32)
        rms = max(float(np.sqrt(np.mean(x * x))), 1.0)
        if self.floor is None:  # capped: if the user talks the moment the call connects, the floor is not their voice
            self.floor = min(max(rms, self.FLOOR_MIN), self.FLOOR_START_MAX)
        loud = rms >= self.floor * self.margin
        # down fast (pauses between words), up 1 % per 32 ms frame: steady babble becomes the floor in ~4 s
        self.floor = max(self.FLOOR_MIN, 0.7 * self.floor + 0.3 * rms if rms < self.floor else self.floor * 1.01)
        return loud

    def feed(self, data: bytes):
        """Yield ("start", None) when speech begins, ("end", float32 audio) when it ends (self.last_voiced_s is its
        voiced time), and ("drop", None) when what started was too short to be a turn (a click, a cough)."""
        self.pending += data
        step = self.FRAME * 2
        while len(self.pending) >= step:
            frame, self.pending = self.pending[:step], self.pending[step:]
            voiced = self.loud_enough(frame) and self.vad(frame) >= self.threshold
            if not self.speech:
                self.ring = (self.ring + [frame])[-self.pre:]
                if voiced:
                    self.speech, self.silent, self.voiced = self.ring[:], 0, 1
                    yield "start", None
                continue
            self.speech.append(frame)
            self.silent = 0 if voiced else self.silent + 1
            self.voiced += voiced
            if self.silent >= self.end_frames and not self.hold:
                audio = np.frombuffer(b"".join(self.speech), dtype=np.int16).astype(np.float32) / 32768
                self.speech, self.ring = [], []
                self.last_voiced_s = self.voiced * self.FRAME / 16000
                yield ("end", audio) if self.last_voiced_s >= self.min_voiced_s else ("drop", None)

    def flush(self):
        """End the speech under way now, as if its closing quiet had come: [("end", audio)] or [("drop", None)], or []
        with nothing under way. For a client that stopped sending mid-turn."""
        if not self.speech:
            return []
        audio = np.frombuffer(b"".join(self.speech), dtype=np.int16).astype(np.float32) / 32768
        self.speech, self.ring, self.pending = [], [], b""
        self.last_voiced_s = self.voiced * self.FRAME / 16000
        return [("end", audio) if self.last_voiced_s >= self.min_voiced_s else ("drop", None)]


class TurnDiag:
    """Per turn, from speech onset to its end: inbound audio seconds, wall seconds, the longest gap between inbound audio
    messages (and when it began, seconds into the call) and what ended it. A turn that closes late with little
    audio and one long gap means the client stopped sending (live 2026-11-23: Mac turns closed ~17 s late)."""

    def __init__(self, t0=None):
        self.t0, self.last, self.started = t0 if t0 is not None else time.monotonic(), None, None

    def start(self, now):
        self.started, self.audio_s, self.gap_s, self.gap_at = now, 0.0, 0.0, None

    def frame(self, now, nbytes):
        """Every inbound audio message (16 kHz PCM16), after the VAD saw it."""
        if self.started is not None:
            if self.last is not None and now - self.last > self.gap_s and self.last >= self.started:
                self.gap_s, self.gap_at = now - self.last, self.last - self.t0
            self.audio_s += nbytes / 32000
        self.last = now

    def done(self, now, by, voiced_s=None):
        """The turn's numbers (None when no speech was under way), and the turn closes."""
        if self.started is None:
            return None
        d = {"by": by, "audio_s": round(self.audio_s, 2), "wall_s": round(now - self.started, 2),
             "max_gap_s": round(self.gap_s, 2), "gap_at_s": None if self.gap_at is None else round(self.gap_at, 2),
             **({"voiced_s": round(voiced_s, 2)} if voiced_s is not None else {})}
        self.started = None
        return d


FILLER_SOUNDS = re.compile(r"(음+|어+|아+|으+|흠+|에+|그+|저기|그러니까)[\s.,!?~]*")
STOP_WORDS = re.compile(r"아니|그만|잠깐|취소|됐어|멈춰|스톱|stop|그거 말고", re.I)
# "잠깐만", "음…", "생각 좀 해볼게": the user asks for time, not an answer. Nothing is said back; the next speech is the turn
# (claude.ai's voice mode has pause_endpointing for the same thing)
HOLD = re.compile(r"^(아+[,.\s]*|어+[,.\s]*|음+[,.\s]*)*(잠깐만?(요|만요)?|잠시만?(요|만요)?|기다려\s*(줘|봐|주세요|봐요)|"
                  r"생각\s*(좀)?\s*(해\s*볼게(요)?|할게(요)?|해\s*보자|해\s*볼까)|음+|어+|그러니까|있잖아|그게|아+)[\s.,!~…]*$")
QUESTION = re.compile(r"\?|뭐|언제|어디|누구|왜|어떻게|몇|얼마|무슨|어때|알려|해\s*줘|줄래|할까|인가|나요|까요|니\b")


def real_content(text):
    """A transcript that is someone talking to the assistant, not noise: two or more Hangul syllables outside
    filler sounds (음, 어…), and not one of Whisper's silence hallucinations."""
    text = (text or "").strip()
    if not text or text.rstrip(".!") in HALLUCINATIONS:
        return False
    words = [w for w in re.split(r"\s+", text) if not FILLER_SOUNDS.fullmatch(w)]
    return len(re.findall(r"[가-힣]", "".join(words))) >= 2 or bool(re.search(r"[A-Za-z]{3,}", text))


CALL_CAPS = ("text", "played", "mode", "secret", "auto_approve")
SECRET_PREFIX = "secret-"  # a secret call's throwaway session key (the gateway keys its own rules off this)  # client messages /v1/call understands beyond audio, hangup and interrupt
TEXT_MAX, TEXT_GAP_S = 1000, 1.0  # a typed message: at most this long, and one per second
WAKE_GUARD_S = 2.0  # /v1/call?intent=wake: the first seconds use the noisy-mode VAD thresholds
TAIL_GUARD_S = 1.0  # the phone may still be playing this long after the server's estimate (network, buffering)
PLAYED_WAIT_S = 2.0  # an app that reports "played": wait this long past the estimate for it, then go on anyway


def _bigrams(s):
    s = re.sub(r"[^가-힣a-z0-9]", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def is_echo(text, playing_text):
    """The assistant's own voice coming back through the phone's speaker: most of the transcript is in what it
    was just saying."""
    heard = _bigrams(text)
    return bool(heard) and len(heard & _bigrams(playing_text)) >= 0.6 * len(heard)


# Whisper turns speaker echo and room noise into a polite greeting; nobody greets in the middle of the assistant's reply
# (live 2026-11-23: "안녕하세요." the user never said cut the schedule answer and was answered with "안녕하세요, 대표님.")
GREETING_ONLY = re.compile(r"^\s*(안녕하세요|안녕하십니까|안녕|여보세요|하이|헬로|hello|hi)\s*[.!?~]*\s*$", re.I)


def barge_verdict(text, voiced_s, vad_turn, playing_text=""):
    """While the assistant was speaking: 'interrupt' only for long enough speech that is plainly meant for it (a stop
    word, a question, or four syllables beyond fillers) and is not its own echo; else 'resume'. Whisper turns noise
    into short made-up words ("오바자."), and the speaker's sound into the reply's own words."""
    if not (vad_turn.barge and voiced_s >= vad_turn.barge_voiced_s and real_content(text)) or GREETING_ONLY.match(text):
        return "resume"
    if stop := STOP_WORDS.search(text):  # "아니요…" in the reply itself can come back through the speaker
        echoed = stop.group(0).lower() in playing_text.lower() and is_echo(text, playing_text)
        return "resume" if echoed else "interrupt"
    words = [w for w in text.split() if not FILLER_SOUNDS.fullmatch(w)]
    meant = QUESTION.search(text) or len(re.findall(r"[가-힣]", "".join(words))) >= 4 \
        or len(re.findall(r"[A-Za-z]{3,}", text)) >= 2
    return "interrupt" if meant and not is_echo(text, playing_text) else "resume"


def followup_action(text):
    """While the assistant is thinking: 'ignore' noise, 'replace' the question being worked on (stop words or a
    new question), or 'queue' it to answer afterwards."""
    if not real_content(text) or GREETING_ONLY.match(text):
        return "ignore"
    return "replace" if STOP_WORDS.search(text) or QUESTION.search(text) else "queue"


@app.websocket("/v1/gateway-link")
async def gateway_link(ws: WebSocket):
    if not link_peer_ok(ws.client.host if ws.client else "", ws.headers):
        await ws.close(code=4403)
        return
    try:
        label = check_token(bearer(ws.headers), ("link", "link_limited"))
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    lk = links.add(ws, limited=label == "link_limited")  # every gateway stays connected; routing picks one per ask
    try:
        while True:
            links.receive(lk, json.loads(await ws.receive_text()))
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        links.remove(lk)


@app.websocket("/v1/call")
async def call(ws: WebSocket, token: str = "", session: str = "", call_id: str = "", client: str = "",
               intent: str = "", persona: str = "", mode: str = "", secret: str = "", resume: str = "", duck: str = ""):
    try:
        check_token(call_token(ws.headers, token))  # header preferred; ?token= only for old clients
        if token and not bearer(ws.headers):  # remove ?token= once every shipped app sends the header
            print("deprecated: /v1/call got its token in the query string; clients must send Authorization",
                  flush=True)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    resume_from = resume  # the handler defines its own resume() (un-pause) below, which rebinds the name
    session = session or f"call-{int(time.time())}"
    # Secret mode: nothing about this call is stored. Its turns live under a throwaway session key (the gateway sees
    # "secret-" and keeps no Claude session, refuses storing tools and forces approvals to once), no call log or brain
    # event is sent, and the key's history is dropped at hangup, never merged into the persona+session history.
    secret_on = secret in ("1", "true")
    if secret_on:
        session = f"{SECRET_PREFIX}{uuid.uuid4().hex[:12]}"
    log_id, started = uuid.uuid4().hex, time.time()  # one call-history entry per connection
    start_log = {"event": "start", "log_id": log_id}
    vad_turn = Turn(SileroVoiceActivityDetector(), mode=links.settings.get("noise_mode", "normal"))
    diag = TurnDiag()

    def diag_done(by, voiced_s=None):
        d = diag.done(time.monotonic(), by, voiced_s)
        if d:  # numbers only: nothing said is printed, and a secret call is not named
            print(f"turn-end {'secret' if current.get('secret') else log_id[:8]} " + " ".join(f"{k}={v}" for k, v in d.items()),
                  flush=True)
        return d
    # a wake-started call opens on room tone and the wake word's tail: its first WAKE_GUARD_S hear only louder,
    # longer speech (the noisy-mode thresholds), so Whisper is not handed silence to hallucinate on
    wake_guard_until = time.monotonic() + WAKE_GUARD_S if intent == "wake" else 0.0
    send_lock = asyncio.Lock()
    # play_end: when the client will finish playing what we already sent. Audio goes out
    # faster than real time, so "still talking" is judged by this, not by the task.
    # gen: the reply generation. Every reply claims a new one and an interrupt bumps it, so audio (and captions) a
    # superseded reply produces afterwards is dropped here: binary frames carry no id the app could check.
    current: dict = {"task": None, "play_end": 0.0, "paused_at": None, "gen": 0, "replies": set(), "side": set(),
                     "said": deque(maxlen=8),  # (sent at, text) of recent reply sentences, for the echo check
                     # the reply whose audio went out last, and the last one the app reported fully played (an app
                     # that sends {"type":"played"} at all: "reports")
                     "audio_gen": 0, "played": None, "reports": False, "text_at": -1e9,
                     "secret": secret_on, "logged": False}  # logged: the call's start went to the call log
    # this call's brain mode: ?mode=, else the shared setting; replies spawned later copy the current value
    current["mode"] = mode if mode in BRAIN_MODES else (links.settings.get("brain_mode") or "auto")
    CALL_MODE.set(current["mode"])
    CALL_LIVE.set(current)

    async def log(ev):
        """The call log, unless secret. A call made secret midway keeps only its (text-free) end."""
        if current["secret"] and not (ev.get("event") == "end" and current["logged"]):
            return
        current["logged"] = current["logged"] or ev.get("event") == "start"
        await links.log(ev)

    async def brain(ev):
        """Call events for the brain, unless secret (it may remember what a call was about). A call made secret
        midway still ends, under the session the brain knows."""
        end, known = ev.get("event") == "end", current.get("brain_session")
        if current["secret"] and not (end and known):
            return
        if ev.get("event") == "start":
            current["brain_session"] = ev.get("session")
        elif end and known:
            ev = {**ev, "session": known}
        await links.brain_event(ev)

    def spawn(coro):
        """A task that may speak: tracked so an interrupt can cancel all of them, not only the latest."""
        t = asyncio.create_task(coro)
        current["replies"].add(t)
        t.add_done_callback(current["replies"].discard)
        t.add_done_callback(_settled)
        return t

    def _settled(t):  # a speaking task that failed (TTS down) is logged, never an unretrieved-exception warning
        if not t.cancelled() and t.exception():
            print("call task failed:", type(t.exception()).__name__, flush=True)

    def side(coro):
        """A helper task that must not block the receive loop (an interrupt, a tap): cancelled on hangup."""
        t = asyncio.create_task(coro)
        current["side"].add(t)
        t.add_done_callback(current["side"].discard)
        return t

    def new_reply():
        current["gen"] += 1
        return current["gen"]

    async def send(obj=None, audio=None, gen=None):
        async with send_lock:
            if gen is not None and gen != current["gen"]:
                return  # from a reply that was interrupted or superseded: never reaches the phone
            if audio is not None:
                now = time.monotonic()
                current["play_end"] = max(now, current["play_end"]) + len(audio) / 2 / 24000
                current["audio_gen"] = gen or current["audio_gen"]
                for i in range(0, len(audio), 9600):  # 200 ms chunks at 24 kHz
                    await ws.send_bytes(audio[i:i + 9600])
            else:
                if obj.get("type") == "reply":
                    current["said"].append((time.monotonic(), str(obj.get("text", ""))))
                if obj.get("type") == "state":  # every state frame says which brain answers, secret, auto-approve
                    obj = {**obj, "mode": current["mode"], "secret": current["secret"], "auto": current.get("auto", False)}
                await ws.send_text(json.dumps(obj, ensure_ascii=False))

    guard = OpeningGuard()

    async def speak_opening(op):
        done = False
        try:
            gen = new_reply()
            await send({"type": "state", "value": "speaking"})
            await send({"type": "reply_start", "reply_id": gen}, gen=gen)
            i = -1
            async for s, pcm in opening_audio(op):  # streams: the first chunks play while the rest synthesize
                i += 1
                await send({"type": "reply", "text": s, "route": "opening"}, gen=gen)
                await send(audio=pcm, gen=gen)
                if i == 0:
                    guard.first_end = current["play_end"]
                guard.play_end = current["play_end"]
            done = True
        finally:
            if not done:  # TTS failed or cancelled: drop the protection, or the call stays deaf for an hour
                guard.first_end = guard.play_end = current["play_end"]
                if op.get("audio"):
                    op["audio"].cancel()  # cut in or hung up: the rest of the opening is never synthesized

    ducks = duck == "1"  # the app lowers the volume on a tentative barge-in instead of pausing (claude.ai's server_duck)

    def cut_at():
        """Now in phone playback time: playback stands still while paused (a ducked reply keeps playing)."""
        return current["paused_at"] if current["paused_at"] is not None and not ducks else time.monotonic()

    def queue_audio(sent, s, pcm, sr):
        """Note when this sentence will play on the phone (before it is sent: send() moves play_end)."""
        start = max(time.monotonic(), current["play_end"])
        sent.append((s, start, start + len(pcm) / 2 / sr))

    async def until_played():
        """Stay 'speaking' until the phone has played everything sent, plus a short tail: only then is it the
        user's turn. Speech meanwhile is a barge-in (pause/check), never a new turn. A pause stretches the wait."""
        while current["paused_at"] is not None or (left := current["play_end"] + PLAY_TAIL_S - time.monotonic()) > 0 \
                or (current["reports"] and current["played"] != current["audio_gen"]
                    and time.monotonic() < current["play_end"] + PLAYED_WAIT_S):  # the estimate ended before the phone
            await asyncio.sleep(0.05 if current["paused_at"] is not None or left <= 0 else min(left, 0.2))

    def played_out():
        """The app said the last reply's audio has finished playing on the phone (no estimate involved)."""
        return current["reports"] and current["played"] == current["audio_gen"]

    async def pause():
        """First stage of a voice barge-in: the app pauses its playback queue; nothing is cancelled yet."""
        current["paused_at"] = time.monotonic()
        await send({"type": "duck"} if ducks else {"type": "pause"})

    async def resume():
        if current["paused_at"] is not None:
            if not ducks:
                current["play_end"] += time.monotonic() - current["paused_at"]  # playback stood still meanwhile
            current["paused_at"] = None
            await send({"type": "unduck"} if ducks else {"type": "resume"})

    async def barge_check(audio, voiced_s):
        """Second stage: interrupt only for real speech, else let the reply play on. Whatever fails (STT down),
        the app never stays paused."""
        t = time.perf_counter()
        verdict, text = "resume", ""
        try:
            text = await transcribe(audio)
            playing = " ".join(t for at, t in current["said"] if time.monotonic() - at < 60)
            verdict = barge_verdict(text, voiced_s, vad_turn, playing)
            print("barge-in:", verdict, f"{voiced_s:.1f}s", "echo" if is_echo(text, playing) else "", flush=True)
        except Exception as e:  # STT down: treat it as noise and play on
            print("barge-in check failed:", type(e).__name__, flush=True)
        finally:
            if verdict != "interrupt":
                await resume()
        if verdict != "interrupt":
            return
        await interrupt()
        current["task"] = asyncio.current_task()
        await answer(audio, text, timings={"barge_stt_ms": ms(t)})

    async def followup(audio, prev):
        """Speech while the assistant is thinking: a replacement cancels the current answer, a follow-up waits."""
        text = await transcribe(audio)
        act = followup_action(text)
        if act == "ignore":
            return
        asked = current.get("asked")
        if asked and not asked["spoke"] and not STOP_WORDS.search(text):
            # nothing said yet: the user was still talking ("…보고서 만들어 줘" + "그러고 끝나고 전화해줘"), so it is one
            # request, whether or not the second half looks like a new question. Answered apart, the first half got
            # its own answer and the second another (live 2026-11-23: "…SK하이닉스 주식" / "오늘 알려줘")
            text = f"{asked['text']} {text}"
            act = "replace"
        if act == "replace":
            await interrupt()  # cancels the answer being worked on (and anything else speaking)
        else:
            await asyncio.gather(prev, return_exceptions=True) if prev else None
        await answer(audio, text)

    async def interrupt(protected=False):
        # Let the opening's first sentence finish playing; re-read, since its end is known only once sent.
        while protected and (wait := guard.wait(time.monotonic())) > 0:
            await asyncio.sleep(min(wait, 0.05))
        current["gen"] += 1  # first: nothing the old reply still produces reaches the phone
        g = current["gen"]  # if a newer reply starts while we wait, our interrupt must not cut it on the phone
        me = asyncio.current_task()
        olds = [t for t in {*current["replies"], current["task"]} if t and not t.done() and t is not me]
        for t in olds:
            t.cancel()
        if olds:  # its TTS, Qwen and Claude work stop before the app is told (a Claude run gets a link cancel)
            await asyncio.wait(olds, timeout=3)
            for t in olds:  # a reply that had already failed (TTS down): its error is consumed here, not lost
                if t.done() and not t.cancelled():
                    t.exception()
        if g != current["gen"]:
            return  # a newer interrupt or reply took over meanwhile: it does the telling
        current["play_end"] = guard.play_end = 0.0
        await send({"type": "interrupt"}, gen=g)  # barge-in: the client drops its queue
        await send({"type": "state", "value": "listening"}, gen=g)  # the cancelled reply's own "listening" is dropped
        if current["paused_at"] is not None:  # it was paused: un-pause the (now empty) queue, or it stays stuck
            current["paused_at"] = None
            await send({"type": "resume"}, gen=g)

    async def speak_briefing():
        spoken, cut, gen, sent = [], False, new_reply(), []
        try:
            await send({"type": "state", "value": "speaking"})
            await send({"type": "reply_start", "reply_id": gen}, gen=gen)
            st = await gateway_settings()
            p = current_persona(st)
            title = user_title(st, p)
            async for s in briefing_sentences(f"{p['id']}:{session}", p["name"], send, persona_text(p, st), title):
                if s not in FILLERS and not spoken:
                    s = with_title(s, title)  # the briefing's first words always use the title
                pcm, sr = await tts(s)
                await send({"type": "reply", "text": s, "route": "briefing"}, gen=gen)
                if s not in FILLERS:
                    queue_audio(sent, s, pcm, sr)
                await send(audio=pcm, gen=gen)
                if s not in FILLERS:
                    spoken.append(s)
        except asyncio.CancelledError:
            cut = True
            spoken, short = heard_part(sent, cut_at(), False)
            raise
        finally:  # what was actually heard becomes the call's opening in history (the start event is an upsert)
            if spoken:
                history[f"{current_persona(await gateway_settings())['id']}:{session}"].append(
                    {"role": "assistant", "content": " ".join(spoken) + (CUT_MARK if cut and short else "")})
                await log({**start_log, "opening": " ".join(spoken)})
            if not cut:
                await until_played()
            await send({"type": "state", "value": "listening"}, gen=gen)  # not after it was interrupted

    async def tap_interrupt():
        guard.speech_at = None
        await interrupt(protected=True)  # cancels the reply task; its Claude run gets a link cancel on the way out

    async def interrupt_then_answer(audio, text=None, **kw):
        await interrupt(protected=True)
        current["task"] = asyncio.current_task()
        await answer(audio, text, **kw)

    async def typed_turn(m):
        """{"type":"text","text","speak"?}: trimmed, ≤ TEXT_MAX chars, at most one per TEXT_GAP_S. While the assistant
        speaks or thinks it interrupts like a tap, then answers; speak=false answers with captions only."""
        t = m.get("text").strip() if isinstance(m.get("text"), str) else ""
        now = time.monotonic()
        if not t:
            return
        if len(t) > TEXT_MAX or now - current["text_at"] < TEXT_GAP_S:
            await send({"type": "text_rejected", "reason": "too_long" if len(t) > TEXT_MAX else "too_fast"})
            return
        current["text_at"] = now
        task = current["task"]
        busy = (task and not task.done()) or now < current["play_end"] or current["paused_at"] is not None
        kw = {"typed": True, "speak": m.get("speak") is not False}
        current["task"] = spawn(interrupt_then_answer(None, t, **kw) if busy else answer(None, t, **kw))

    async def announcer(q):
        """Speak each finished task's result as its own turn, once the call is quiet (nothing playing or thinking)."""
        while True:
            m = await q.get()
            while (current["task"] and not current["task"].done()) or current["paused_at"] is not None \
                    or time.monotonic() < current["play_end"]:
                await asyncio.sleep(0.2)
            current["task"] = spawn(speak_announcement(m))
            await asyncio.gather(current["task"], return_exceptions=True)

    async def speak_announcement(m):
        hkey = f"{current_persona(await gateway_settings())['id']}:{session}"
        if isinstance(m.get("detail"), str) and m["detail"]:
            history[hkey].append({"role": "assistant", "content": f"{DETAIL_NOTE} {m['detail'][:4000]}"})
        ctx = m.get("context")
        if isinstance(ctx, dict) and ctx.get("kind") == "task":
            await send({"type": "context", "context": {k: str(ctx.get(k) or "")[:60] for k in ("kind", "label", "id")}})
        gen, said, spoken = new_reply(), [], False
        try:
            await send({"type": "state", "value": "speaking"})
            await send({"type": "reply_start", "reply_id": gen}, gen=gen)
            async for s, pcm in opening_audio(prepare_opening(m["opening"])):  # gap-free, like an opening
                await send({"type": "reply", "text": s, "route": "task"}, gen=gen)
                await send(audio=pcm, gen=gen)
                said.append(s)
            await until_played()
            spoken = True
        finally:
            if said:
                history[hkey].append({"role": "assistant", "content": " ".join(said)})
            # the gateway counts it delivered only now; cut off or hung up first: it rings instead
            await links.notify({"type": "announced", "id": m.get("id"), "ok": spoken})
            await send({"type": "state", "value": "listening"}, gen=gen)

    async def opening_heard(audio):
        """The callee spoke while a placed call's opening was about to play or playing. A greeting or ack lets it
        play on (once, in full); a stop word or a real question interrupts; anything else is answered after it."""
        text = await transcribe(audio)
        if not text or not real_content(text) or is_ack(text, (CALL_PERSONA.get() or {}).get("name", "")):
            return
        if STOP_WORDS.search(text) or QUESTION.search(text):
            await interrupt_then_answer(audio, text)
            return
        if (op_task := current.get("opening")) and not op_task.done():
            await asyncio.gather(op_task, return_exceptions=True)
        await answer(audio, text)

    async def answer(audio, text=None, timings=None, typed=False, speak=True):
        """One user turn: spoken (audio, transcribed here) or typed (text; speak=False: captions without audio)."""
        t0, timings, gen = time.perf_counter(), timings or {}, new_reply()
        turn = {"assistant": [], "activity": [], "route": "", "interrupted": False, **({"input": "text"} if typed else {})}
        sent, kept, finished = [], {}, False

        async def on_event(ev):
            if ev.get("phase") in ("tool_start", "thinking"):
                turn["activity"].append({"label": ev.get("label", ""), "kind": ev.get("kind", "generic"),
                                         "at": time.time()})
            if ev.get("type") == "card":
                ev = {**ev, "reply_id": gen}  # the app shows the card under this reply (reply_start's id)
            await send(ev)
        try:
            await send({"type": "state", "value": "thinking"})
            text = text if text is not None else await transcribe(audio)
            timings["stt_ms"] = ms(t0)
            if not text:
                return
            if not typed and HOLD.fullmatch(text.strip()):
                text = ""  # only asked for time: no reply, no turn in the history, back to listening below
                return
            if not typed:  # the app already shows what the user typed
                await send({"type": "transcript", "text": text})
            current["asked"] = asked = {"text": text, "spoke": False}  # followup() joins a cut-in to an unanswered turn
            if not typed and DANGLING.search(text):
                # "…엔트로픽이랑 그리고": the user paused mid-sentence. Wait a moment: more speech arrives as a follow-up
                # that cancels this task and joins both halves, instead of two answers (live 2026-11-23)
                await asyncio.sleep(DANGLING_WAIT_S)
            started = False
            # aclosing: the reply's history turn exists before a cut below rewrites it (no GC-time finally)
            async with contextlib.aclosing(respond(text, session, t0, timings, on_event=on_event, kept=kept,
                                                       speak=speak)) as replies:
                async for s, route, pcm, sr in replies:
                    if gen != current["gen"]:
                        raise asyncio.CancelledError  # superseded while synthesizing: stop, say nothing more
                    if not started:
                        await send({"type": "state", "value": "speaking"})
                        await send({"type": "reply_start", "reply_id": gen}, gen=gen)  # new audio starts here
                        started = asked["spoke"] = True
                    turn["route"] = route
                    if s not in FILLERS:
                        turn["assistant"].append(s)
                        queue_audio(sent, s, pcm, sr)
                    await send({"type": "reply", "text": s, "route": route}, gen=gen)
                    if pcm:
                        await send(audio=pcm, gen=gen)
            finished = True
            timings["total_ms"] = ms(t0)
            queued = max(0, round((current["play_end"] - time.monotonic()) * 1000))  # still to play when generation ended
            await until_played()
            await send({"type": "turn_done", "timings": timings, "audio_ends_in_ms": queued,
                        "brain": "claude" if turn["route"] == "claude" else "local"})  # who actually answered
        except asyncio.CancelledError:
            turn["interrupted"] = True
            # Hung up or cut in: history and the call log keep only what the phone played, marked as cut off, so
            # the next turn (or the next call on this session) never treats the unheard rest as said.
            heard, short = heard_part(sent, cut_at(), finished)
            turn["assistant"] = heard
            if kept:
                keep_heard(kept["hist"], kept["entry"], heard, short)
            raise
        except Exception as e:  # keep the call alive on a failed turn; the user never sees a Python error
            print("turn failed:", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)), flush=True)
            await send({"type": "error", "message": TURN_FAILED})
        finally:
            if text:  # turns with an empty transcript are not history
                await log({"event": "turn", "log_id": log_id, "user": text, **turn,
                                 "timings": timings, "at": time.time()})
            if not turn["interrupted"]:  # a failed turn's partial reply still plays out first
                await until_played()
            await send({"type": "state", "value": "listening"}, gen=gen)  # an interrupted or superseded turn: none

    try:
        op = links.openings.pop(call_id, None) if call_id else None
        await send({"type": "hello", "caps": list(CALL_CAPS)})  # what this server takes on the socket (the app's UI)
        if op and op.get("context"):  # first message: what this call is about (the app's "맡긴 작업: …" chip)
            await send({"type": "context", "context": op["context"]})
        # The persona: a placed call's own (it rang as that persona), else ?persona=, else the primary. Every task
        # this call starts inherits it (name, character, TTS voice, history key, Claude session, tags).
        CALL_PERSONA.set(pick_persona(links.settings, (op or {}).get("persona") or persona))
        pid = CALL_PERSONA.get()["id"]
        briefing = intent == "briefing" and not call_id
        if call_id:  # the callee picked up: stops a scheduled call's retries
            await links.notify({"type": "call_answered", "call_id": call_id})
        await brain({"type": "call_event", "event": "start", "session": session, "persona": pid,
                                 "reason": (op or {}).get("reason", ""), "opening": (op or {}).get("text", "")})
        start_log.update(session=session, call_id=call_id, client=client[:20], started_at=started, persona_id=pid,
                         # the user's point of view, like a phone's call log: the assistant calling = incoming
                         direction="incoming" if call_id else "outgoing",
                         reason="briefing" if briefing else (op or {}).get("reason", ""), opening=(op or {}).get("text", ""))
        await log(start_log)
        if resume_from and not current["secret"] and not history[f"{pid}:{session}"]:
            await seed_from_call(f"{pid}:{session}", resume_from)  # "이어서 통화" from the call list
        if briefing:  # the app's "오늘 브리핑": speak a briefing first, interruptible like any reply
            current["task"] = spawn(speak_briefing())
        if call_id:  # the assistant placed this call: say why first (sent ahead over the link)
            if not (op and op["parts"]):  # its opening never arrived: never a silent call
                op = {**(op or {}), **prepare_opening(FALLBACK_OPENING)}
            if op.get("origin") and op["origin"] != f"{pid}:{session}" and not current["secret"]:  # a task's or reminder's call: continue there
                history[f"{pid}:{session}"].extend(list(history.get(op["origin"], ()))[-ORIGIN_TURNS:])
            if op.get("detail"):  # a task's whole result: follow-up questions are answered from it
                history[f"{pid}:{session}"].append({"role": "assistant", "content": f"{DETAIL_NOTE} {op['detail']}"})
            history[f"{pid}:{session}"].append({"role": "assistant", "content": op["text"]})
            guard.protect(time.monotonic())  # before any frame is read, so an early tap cannot cut it
            current["task"] = current["opening"] = spawn(speak_opening(op))
        call_key = f"{pid}:{session}"
        active_calls[call_key] = announce_q = asyncio.Queue()

        async def on_auto_state(m):
            current["auto"] = m.get("value") is True
            await send({"type": "auto_approve", "value": current["auto"], "expires_at": m.get("expires_at")})
        call_hooks[call_key] = on_auto_state

        async def on_end_call(m):
            """Let the reply in progress (the goodbye) finish playing, then tell the app and close the socket."""
            await asyncio.sleep(0.3)  # the goodbye's reply task starts right after the tool answered
            # every reply, not only current["task"]: a noise "follow-up" that returned at once had replaced it and the
            # call hung up before the handoff was confirmed (2026-11-22 21:36)
            while any(t and not t.done() for t in {current["task"], *current["replies"]}) \
                    or current["paused_at"] is not None or time.monotonic() < current["play_end"]:
                await asyncio.sleep(0.2)
            await send({"type": "hangup", "reason": str(m.get("reason") or "")[:40]})
            await ws.close()
        end_hooks[call_key] = on_end_call
        side(announcer(announce_q))
        await send({"type": "mode", "value": current["mode"]})  # the app shows which brain answers
        async def on_vad(kind, audio, by):
            """One VAD event (start / end / drop) of this call's speech; `by` says what ended it (quiet, stall)."""
            now = time.monotonic()
            d = diag_done(by if kind == "end" else "drop", vad_turn.last_voiced_s) if kind in ("end", "drop") else None
            task = current["task"]
            busy = task and not task.done()
            # The tail guard: speech then still gets the barge-in check (echo, noise), never a free new turn
            # whose reply_start would make the app drop the rest of a reply it is still playing.
            playing = now < current["play_end"] + TAIL_GUARD_S * (current["play_end"] > 0 and not played_out()) \
                or current["paused_at"] is not None
            if kind == "start" and guard.active(now):
                guard.start(now)  # a placed call's opening: its own rules (first sentence, greetings)
            elif kind == "start" and playing and current["paused_at"] is None and vad_turn.barge:
                await pause()
            elif kind == "drop":
                guard.end(now, 0.0)
                await resume()  # a click or cough: the reply plays on
            elif kind == "end":
                if guard.speech_at is not None:  # during a placed call's opening: decided on the words
                    guard.end(now, len(audio) / 16000)
                    spawn(opening_heard(audio))
                elif current["paused_at"] is not None:
                    spawn(barge_check(audio, vad_turn.last_voiced_s))
                elif playing and not vad_turn.barge:
                    pass  # noisy mode: speech over the reply is not a turn
                elif busy:  # thinking: never cancel on raw voice, decide on the transcript
                    current["task"] = spawn(followup(audio, task))
                else:
                    current["task"] = spawn(answer(audio, timings={"turn": d} if d else None))

        await send({"type": "state", "value": "listening"})
        last_audio, last_len = time.monotonic(), 0.0
        while True:
            try:  # mid-speech, no audio from the client for END_STALL_S ends the turn (control messages don't count)
                left = END_STALL_S - (time.monotonic() - last_audio)
                msg = await (asyncio.wait_for(ws.receive(), max(0.01, left)) if vad_turn.speech else ws.receive())
            except asyncio.TimeoutError:
                for kind, audio in vad_turn.flush():
                    await on_vad(kind, audio, "stall")
                continue
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                try:
                    frame = json.loads(msg["text"])
                except ValueError:
                    frame = None
                if not isinstance(frame, dict):  # not a JSON object: ignored, never a crash of the call
                    continue
                kind = frame.get("type")
                if kind == "hangup":
                    break
                if kind == "interrupt":  # tap-to-interrupt: same as a barge-in, after a protected first sentence
                    diag_done("interrupt")
                    side(tap_interrupt())
                if kind == "played":  # the phone finished reply N: playback really ends now, not at the estimate
                    current["reports"] = True
                    if frame.get("reply_id") == current["audio_gen"]:
                        current["played"] = current["audio_gen"]
                        if current["paused_at"] is None:
                            current["play_end"] = time.monotonic()
                if kind == "secret":  # on midway: from here nothing is stored; off is refused (a secret call stays one)
                    if frame.get("value") is True and not current["secret"]:
                        old, session = session, f"{SECRET_PREFIX}{uuid.uuid4().hex[:12]}"
                        for k in [k for k in history if k.endswith(f":{old}")]:  # the context so far, as a copy
                            history[k.rsplit(":", 1)[0] + f":{session}"] = deque(history[k], maxlen=MAX_TURNS * 2)
                        current["secret"] = True
                    await send({"type": "secret", "value": current["secret"]})
                if kind == "auto_approve":  # the app's toggle: the gateway makes or ends the grant, auto_state answers
                    await links.notify({"type": "auto_grant", "session": call_key, "value": frame.get("value") is True,
                                           **({"minutes": frame["minutes"]} if isinstance(frame.get("minutes"), (int, float)) else {}),
                                           **({"devices": frame["devices"]} if isinstance(frame.get("devices"), list) else {})})
                if kind == "text":  # typed during the call: a user turn like a transcript, no STT, no VAD
                    await typed_turn(frame)
                if kind == "phone_hello":  # the categories the phone answers in this call
                    ks = frame.get("kinds")
                    phones[:] = [p for p in phones if p["send"] is not send]
                    phones.append({"send": send, "kinds": PHONE_KINDS & set(ks if isinstance(ks, list) else [])})
                if kind == "phone_result":  # relayed, never stored or logged (secret calls included)
                    side(phone_result(frame))
                if kind == "mode" and (v := frame.get("value")) in BRAIN_MODES:
                    current["mode"] = v
                    CALL_MODE.set(v)  # from the next reply on
                    await send({"type": "mode", "value": v})
                continue
            data, now = msg.get("bytes") or b"", time.monotonic()
            vad_turn.hold = len(data) / 32000 > BURST_S or now - last_audio < BURST_GAP * last_len
            last_audio, last_len = now, len(data) / 32000
            vad_turn.set_mode("noisy" if time.monotonic() < wake_guard_until else links.settings.get("noise_mode", "normal"))
            diag.frame(time.monotonic(), len(msg.get("bytes") or b""))  # before the VAD: a stall then the closing frames
            for kind, audio in vad_turn.feed(msg.get("bytes") or b""):
                if kind == "start":
                    diag.start(time.monotonic())
                    diag.audio_s += len(msg.get("bytes") or b"") / 32000  # the message speech began in (a one-shot send: all of it)
                await on_vad(kind, audio, "quiet")
    except WebSocketDisconnect:
        pass
    finally:  # hung up: stop everything this call started (a Claude run gets a link cancel as its task unwinds)
        diag_done("hangup")  # speech still under way when the call ended
        async def cleanup():
            phones[:] = [p for p in phones if p["send"] is not send]
            left = [t for t in {*current["replies"], *current["side"], current["task"]} if t and not t.done()]
            for t in left:
                t.cancel()
            await asyncio.gather(*left, return_exceptions=True)  # its turn is logged before the end
            ended = time.time()
            await log({"event": "end", "log_id": log_id, "ended_at": ended, "duration_s": round(ended - started, 1)})
            await brain({"type": "call_event", "event": "end", "session": session})
            links.session_ctx.pop(session, None)
            for k in [k for k, q in active_calls.items() if k.endswith(f":{session}")]:
                q = active_calls.pop(k, None)
                while q is not None and not q.empty():  # taken but never spoken before the hangup: the gateway rings
                    await links.notify({"type": "announced", "id": q.get_nowait().get("id"), "ok": False})
                call_hooks.pop(k, None)
                end_hooks.pop(k, None)
                pending_auto.pop(k, None)  # a read-back never carries into the next call
                if current.get("auto"):  # its grant ends with the call (a secret call has no call log to end it)
                    await links.notify({"type": "auto_grant", "session": k, "value": False})
            for k in [k for k in games if k.endswith(f":{session}")]:  # a game left running never carries into the next call
                games.pop(k, None)
            if current["secret"]:  # forget the secret turns: history, title rhythm, a half-asked schedule
                for store in (history, history_sig, title_gap):
                    for k in [k for k in store if k.endswith(f":{session}")]:
                        store.pop(k, None)
                pending_schedule.pop(session, None)

        # shielded: a handler cancelled on the way out (the server closed the socket, an abrupt drop) must still end
        # the call log, its grant and its announcements
        done = asyncio.ensure_future(cleanup())
        try:
            await asyncio.shield(done)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    # Self-checks without GPU or network (run: python voice_server.py --check).
    if CHECK_ONLY:
        real_route_lookup = route_lookup

        async def no_route(*a, **k):  # no network in the checks: the tool-first decision is stubbed where it is tested
            return None
        route_lookup = no_route
        # live 2026-11-23: a greeting Whisper made up over the reply is noise, and a second announcement is dropped
        assert all(GREETING_ONLY.match(x) for x in ("안녕하세요.", "안녕하세요", "여보세요?", "Hello"))
        assert not GREETING_ONLY.match("안녕하세요 오늘 일정 알려줘") and followup_action("안녕하세요.") == "ignore"
        assert INTRO_ANNOUNCE.search("오늘 일정 브리핑해 드릴게요.") and INTRO_ANNOUNCE.search("알려드릴게요.")
        assert not INTRO_ANNOUNCE.search("오늘은 수업이 두 개예요.")
        class _VT:
            barge, barge_voiced_s = True, 0.3
        assert barge_verdict("안녕하세요.", 1.0, _VT(), "오늘 일정은 두 건이에요.") == "resume"
        assert barge_verdict("아니 잠깐만 그거 말고", 1.0, _VT(), "오늘 일정은 두 건이에요.") == "interrupt"
        assert all(HOLD.fullmatch(x) for x in ("잠깐만", "잠깐만요.", "음…", "아, 잠시만", "생각 좀 해볼게", "기다려 줘", "그러니까"))
        assert not any(HOLD.fullmatch(x) for x in ("잠깐만 일정 알려줘", "음 그래서 뭐 했지", "기다려 주셔서 감사해요", "아니 그거 말고"))
        # local first: only a model or Claude asked for by name, or a default model, goes straight to Claude
        for ask in ("오늘 일정 뭐야?", "광주 날씨 어때?", "삼성전자 주가 알려줘", "서버 괜찮아?", "스파크 재시작해 줘",
                    "맥북 다운로드 폴더에 파일 넣어 줘", "오늘 해야 될 일정 브리핑 해줘", "내가 가진 주식 오늘 알려줘",
                    "비트코인 가격 알려줘", "레포 빌드 에러 고쳐 줘", "맥북 켜져 있어?"):
            assert pre_route(ask, {}) == (False, None), ask  # local first: Qwen has its tools, and hands over itself
        assert pre_route("클로드로 해 줘", {})[0] and pre_route("오퍼스로 풀어 줘", {}) == (True, "opus")  # asked by name
        assert pre_route("안녕", {"default_model": "sonnet"}) == (True, "sonnet")  # a default set in the settings
        assert not pre_route("스파크 재시동이죠 클로드는 쓰지마", {})[0] and CLAUDE_NO.search("클로드 말고 네가 해")
        assert not CLAUDE_NO.search("클로드로 해 줘") and not CLAUDE_NO.search("오늘 일정 알려줘")
        def nomode(f):  # state frames carry the call's brain mode and secret flag; most checks don't care
            return {k: v for k, v in f.items() if k not in ("mode", "secret", "auto")} if isinstance(f, dict) else f
        assert split_ready("안녕하세요. 오늘", False) == (["안녕하세요."], "오늘")
        assert korean_only("회의(會議)가 있어요.") == "회의가 있어요." and korean_only("好的。") == ""
        for noise in ["MBC 뉴스 김철수입니다.", "MBC 뉴스 이덕영입니다", "시청해 주셔서 감사합니다", "시청해주셔서 감사합니다!",
                      "구독과 좋아요 부탁드립니다", "구독, 좋아요 알림설정", "자막 제공 배달의민족", "감사합니다.",
                      "아니 아니 아니 아니 아니 아니 아니 아니", "네 네 네 네 네 네 네", "지금까지 뉴스 스토리였습니다.",
                      "지금까지 MBC 뉴스였습니다", "뉴스 스토리", "다음 시간에 만나요.", "MBC 뉴스", "KBS 뉴스.", "YTN",
                      "감사합니다"]:
            assert clean_transcript(noise) == "", noise  # Whisper's noise/subtitle outputs are not the user
        for real in ["아니", "아니 그게 아니라", "매우 좋아", "네 네", "뉴스 좀 틀어줘", "정말 감사합니다 덕분에 살았어요",
                     "지금까지 한 거 정리해 줘", "MBC 뉴스 틀어 줘", "KBS 뉴스 보고 싶어", "오늘 뉴스 스토리 뭐 있어?"]:
            assert clean_transcript(real) == real, real  # short real speech passes untouched
        assert clean_transcript("매우 매우 매우 좋아") == "매우 좋아" and clean_transcript("아니아니아니아니") == "아니"
        assert clean_transcript("그게 아니라 그게 아니라 그게 아니라 진짜로") == "그게 아니라 진짜로"
        assert not STT_QUALITY["condition_on_prev_tokens"] and STT_QUALITY["compression_ratio_threshold"] == 2.4

        def unthought(chunks):
            f = Unthink()
            out = [f.feed(c) for c in chunks]
            return out, f.close(), f.thought
        out, rest, th = unthought(["<thi", "nk>Okay, the user asks about wa", "ter.</th", "ink>네, 물은 백 도", "에서 끓어요."])
        assert "".join(out) + rest == "네, 물은 백 도에서 끓어요." and th  # split tags across chunks
        assert out[3] == "네, 물은 백 도"  # once past the reasoning, Korean streams out at once
        out, rest, th = unthought(["<think>Let me consider", " the schedule first"])
        assert "".join(out) + rest == "" and th  # unclosed: nothing at all
        out, rest, th = unthought(["Okay so the user wants", " a time.\n</think>\n\n지금은 오후 세 시예요."])
        assert "".join(out) + rest == "\n\n지금은 오후 세 시예요." and th  # stray </think>: all before it dropped
        out, rest, th = unthought(["안녕하세요, ", "무엇을 도와", "드릴까요?"])
        assert out == ["안녕하세요, ", "무엇을 도와", "드릴까요?"] and rest == "" and not th  # normal text untouched
        out, rest, th = unthought(["Sure", ", 3 PM."])
        assert out == ["", ""] and rest == "Sure, 3 PM." and not th  # English with no tags: held, then said whole
        out, rest, th = unthought(["답이에요 <thi", "nk>속으로</think> 끝."])
        assert "".join(out) + rest == "답이에요  끝." and th  # a think block mid-answer
        assert split_ready("好的. 내일 봬요.", True)[0] == ["내일 봬요."] and korean_only("API 서버") == "API 서버"
        assert split_ready("네! 알겠어요? 그럼", False) == (["네! 알겠어요?"], "그럼")  # "네!" alone is too short
        assert split_ready("네. 말씀하세요", False) == ([], "네. 말씀하세요") and split_ready("네. 그럼요.", True)[0] == ["네. 그럼요."]
        assert split_ready("오늘은 맑아요. 네.", True)[0] == ["오늘은 맑아요. 네."]
        assert split_ready("모두 " + "3,000원이고 " * 12, False)[0] == []  # never cut inside a number
        assert split_ready("마지막 문장", True) == (["마지막 문장"], "")
        buf, out = "", []  # streamed two characters at a time, words keep their spaces
        for i in range(0, len(text := "광합성은 식물이 빛 에너지를 이용해요. 그래서 산소가 나와요."), 2):
            done, buf = split_ready(buf + text[i:i + 2], False)
            out += done
        assert out + split_ready(buf, True)[0] == ["광합성은 식물이 빛 에너지를 이용해요.", "그래서 산소가 나와요."], out
        assert split_ready("네, 알겠습니다. 오늘", False, first=True) == (["네, 알겠습니다."], "오늘")
        assert split_ready("오늘 날씨는요, 맑아요", False, first=True) == (["오늘 날씨는요,"], "맑아요")
        assert split_ready("네,", False, first=True) == ([], "네,")
        long = "가" * 70 + ", " + "나" * 20
        assert split_ready(long, False) == (["가" * 70 + ","], "나" * 20)
        assert now_kst().startswith(" 현재 시각: 20") and "요일" in now_kst()
        assert pick(MODEL_WORDS, "이번엔 오퍼스로 해 줘") == "opus" and pick(MODEL_WORDS, "안녕") is None
        assert pick(TASK_WORDS, "블루리프 레포 빌드 에러 고쳐 줘") == "code"
        assert pick(TASK_WORDS, "내일 일정 알려줘") == "schedule"

        class FakeVad:  # loud frames = speech
            def __call__(self, frame):
                return float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)
        t = Turn(FakeVad(), end_silence_ms=200)
        loud = (np.ones(16000, np.int16) * 3000).tobytes()  # 1 s speech
        quiet = np.zeros(8000, np.int16).tobytes()           # 0.5 s silence
        ev = list(t.feed(quiet + loud[:5000])) + list(t.feed(loud[5000:] + quiet))
        assert [k for k, _ in ev] == ["start", "end"] and 0.9 < len(ev[1][1]) / 16000 < 1.6, ev
        assert list(t.feed(quiet)) == []
        assert t.flush() == []  # nothing under way
        assert [k for k, _ in t.feed(quiet + loud)] == ["start"]  # a one-breath block that ends on its last word
        got = t.flush()  # the client sends nothing more: the turn ends with what came
        assert [k for k, _ in got] == ["end"] and 0.9 < len(got[0][1]) / 16000 < 1.4 and t.speech == [], got
        short = (np.ones(3000, np.int16) * 3000).tobytes()   # 0.19 s blip: no turn
        assert [k for k, _ in t.feed(short + quiet)] == ["start", "drop"]  # a blip: no turn, and playback resumes

        # noise: Silero says "speech" for everything here (its worst case); only the noise floor stands between
        def tone(seconds, amp, seed=0):
            rng = np.random.default_rng(seed)
            return (rng.standard_normal(int(16000 * seconds)) * amp).clip(-32000, 32000).astype(np.int16).tobytes()
        always = lambda frame: 0.95  # noqa: E731
        silence = np.zeros(16000, np.int16).tobytes()
        t = Turn(always)
        babble = tone(8, 1000, 1)  # continuous babble; speech below is 15 dB louder (1000 * 5.6)
        assert [k for k, _ in t.feed(babble)] in ([], ["start", "drop"])  # learns it as the floor, no turn
        assert [k for k, _ in t.feed(tone(4, 1000, 2))] == []  # and stays quiet on it
        ev = list(t.feed(tone(0.8, 5600, 3) + tone(1, 1000, 4)))  # "잠깐만" over the babble
        assert [k for k, _ in ev] == ["start", "end"] and t.last_voiced_s >= 0.5, (ev, t.last_voiced_s)
        t = Turn(always)
        t.feed(silence) and None
        list(t.feed(silence))
        cough = tone(0.2, 8000, 5)
        assert [k for k, _ in t.feed(cough + silence)] == ["start", "drop"]  # a 200 ms cough: never a turn
        dg = TurnDiag(t0=100.0)
        dg.frame(100.5, 3200)  # before any speech: not counted
        dg.start(101.0)
        for at in (101.1, 101.2, 101.3):
            dg.frame(at, 3200)  # 0.1 s chunks arriving in time
        dg.frame(118.3, 3200)  # then a 17 s stall before the closing frames
        got = dg.done(118.6, "quiet", 1.4)
        assert got == {"by": "quiet", "audio_s": 0.4, "wall_s": 17.6, "max_gap_s": 17.0, "gap_at_s": 1.3, "voiced_s": 1.4}, got
        assert dg.done(119.0, "hangup") is None  # nothing under way: nothing to say
        bursts = b"".join(tone(0.1, 9000, 6 + i) + silence[:9600] for i in range(5))  # door slams, clatter
        kinds = [k for k, _ in t.feed(bursts + silence)]
        # bursts 300 ms apart merge into one utterance (end-of-speech needs 500 ms of quiet); it reaches STT, and
        # noise transcribes to nothing or a hallucination, which the transcript gate rejects
        assert kinds[0] == "start" and (kinds[-1] == "drop" or barge_verdict("", t.last_voiced_s, t) == "resume")
        noisy = Turn(always, mode="noisy")
        list(noisy.feed(silence))
        assert not noisy.barge and noisy.threshold > t.threshold and noisy.margin > t.margin
        for text, voiced, want in [("잠깐만", 0.8, "interrupt"), ("잠깐만", 0.3, "resume"), ("음", 0.9, "resume"),
                                   ("어 어", 0.9, "resume"), ("시청해 주셔서 감사합니다", 1.0, "resume"), ("", 1.0, "resume"),
                                   ("그거 말고 내일 거", 1.2, "interrupt")]:
            assert barge_verdict(text, voiced, t) == want, (text, voiced)
        assert barge_verdict("잠깐만", 2.0, noisy) == "resume"  # noisy: voice never interrupts playback
        playing = "대표님, 제가 쓸 수 있는 도구는 크게 여섯 묶음이에요. 먼저 기본 도구로는 파일 읽기가 있어요."
        for text, want in [("오바자.", "resume"), ("여섯 점 해줘.", "interrupt"), ("쓸 수 있는 도구는 크게", "resume"),
                           ("아니 그런 거 아니고", "interrupt"), ("어 확인했어", "interrupt"),
                           ("그만 그만", "interrupt"), ("알았어", "resume"), ("뭐라고?", "interrupt"),
                           ("양치해가지고 더 먹기가 좀 그래", "interrupt")]:  # from live calls: noise, echo, real
            assert barge_verdict(text, 1.0, t, playing) == want, (text, want)
        assert barge_verdict("you know what I mean", 1.5, t, "Well, you know what I mean, right?") == "resume"
        assert barge_verdict("you know what I mean", 1.5, t, "다른 얘기") == "interrupt"
        assert barge_verdict("You know...", 1.0, t, "Spicy stops, huh? Well, you know, since you're in the mood.") == "resume"
        hist = [{"role": "user", "content": "도구 뭐 있어?"}, {"role": "assistant", "content": "도구는 여섯 묶음이에요." + CUT_MARK}]
        assert CUT_NOTE in recent_context(hist) and CUT_NOTE not in recent_context(hist[:1] + [{"role": "assistant", "content": "네."}])
        no = "아니요, 그건 제가 못 해요. 다른 걸 해 볼까요?"
        assert barge_verdict("아니요 그건 제가 못 해요", 1.0, t, no) == "resume"  # its own stop word, echoed back
        assert barge_verdict("아니 그거 말고", 1.0, t, no) == "interrupt" and barge_verdict("아니 잠깐", 1.0, t, playing) == "interrupt"
        assert barge_verdict("파일 읽기", 1.0, t, playing) == "resume" and barge_verdict("그만 읽어", 1.0, t, playing) == "interrupt"
        for text, want in [("음", "ignore"), ("", "ignore"), ("아니 그거 말고", "replace"), ("잠깐", "replace"),
                           ("내일 날씨는 어때?", "replace"), ("그리고 메일도 확인해 줘", "replace"),
                           ("고마워요", "queue"), ("그리고 회의는 세 시예요", "queue")]:
            assert followup_action(text) == want, (text, followup_action(text))
        # live 2026-11-23: a half-said sentence waits for its rest; "기억했어요" with no remember tool is a false claim
        assert all(DANGLING.search(x) for x in ("엔트로픽이랑 그리고", "애플이랑", "주식 갖고 있는데.", "그리고…"))
        assert not any(DANGLING.search(x) for x in ("주식 오늘 알려줘", "고마워요", "일정 있어?"))
        assert ACTION_CLAIM.search("네, 엔트로픽도 추가해서 기억했어요.") and ACTION_CLAIM.search("스파크를 재시작했어요.")
        assert asks_action("애플 주식 기억해줘") and asks_action("스파크 재시작해줘") and not asks_action("오늘 기분 어때")
        assert action_tool("remember") and not ACTION_CLAIM.search("기억나요")
        # live 2026-11-23: "재시동을 시도해 볼게요. 잠시만 기다려 주세요." and nothing ran; Claude's English notes were spoken
        assert ACTION_CLAIM.search("스파크에 접속해서 네모클로즈의 재시동을 시도해 볼게요.") and asks_action("스파크 재시동해줘")
        # by the request, not the wording: any statement of doing, when an action was asked and nothing ran
        assert unconfirmed_claim("스파크에 들어가서 바로 처리해 둘게요.", "스파크 네모클로 재시동해줘")
        assert unconfirmed_claim("네, 그렇게 해 놨습니다.", "저기 서버 껐다 켜 줘")
        assert not unconfirmed_claim("어느 서버를 재시작할까요?", "스파크 재시동해줘")
        assert not unconfirmed_claim("그건 제가 직접 처리하지 못해요.", "스파크 재시동해줘")
        assert not unconfirmed_claim("내일은 비가 올 것 같아요.", "내일 날씨 알려줘")
        assert not unconfirmed_claim("일정은 두 건이에요 오늘은요.", "오늘 일정 확인해줘")
        assert not unconfirmed_claim("그건 좋은 생각이에요 정말로요.", "나 오늘 피곤해")
        assert ko_read.english_only("I'll restart its agent gateway.") and not ko_read.english_only("GPU 서버예요.")
        assert not ko_read.english_only("iPhone 15") and not ko_read.english_only("네, OK.")
        class FakeWS:
            def __init__(self):
                self.sent = []

            async def send_text(self, raw):
                self.sent.append(json.loads(raw))

        gl = GatewayLinks()
        mac, alb, spark = (gl.add(FakeWS()) for _ in range(3))
        gl.receive(mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
        gl.receive(alb, {"type": "hello", "device": "albireo", "mode": "full", "priority": 20})
        gl.receive(spark, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
        gl.receive(alb, {"type": "settings", "value": {"default_model": "opus"}})  # settings from any link
        gl.receive(alb, {"type": "call", "call_id": "c1", "opening": "좋은 아침이에요."})  # openings too
        gl.receive(spark, {"type": "call", "call_id": "c2", "opening": "x"})  # but never from a limited link
        assert "c2" not in gl.openings
        assert gl.settings["default_model"] == "opus" and gl.openings["c1"]["text"] == "좋은 아침이에요."
        assert gl.pick(True) is mac and gl.pick(False) is mac
        mac.fails = 3  # unhealthy: the next full link by priority takes over
        assert gl.pick(True) is alb
        mac.fails = 0
        q = asyncio.Queue()
        gl.queues["r1"] = (q, mac)
        gl.receive(mac, {"id": "r1", "type": "text", "delta": "네"})
        gl.receive(mac, {"id": "zz", "type": "text", "delta": "lost"})  # unknown ids are ignored
        assert q.get_nowait()["delta"] == "네" and q.empty()
        q2 = asyncio.Queue()
        gl.queues["r2"] = (q2, alb)
        gl.remove(mac)  # only the dropped link's asks fail
        assert q.get_nowait()["type"] == "error" and q2.empty() and mac not in gl.links
        gl.remove(alb)
        assert gl.pick(False) is spark  # no full link: limited takes non-personal asks only
        try:
            gl.pick(True)
            raise AssertionError("personal ask reached a limited gateway")
        except RuntimeError as e:
            assert str(e) == NO_PERSONAL
        again = gl.add(FakeWS())
        gl.receive(again, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
        assert gl.links == [again]  # a reconnect replaces that device's old link
        gl.remove(again)
        try:
            gl.pick(False)
            raise AssertionError("picked a link with none connected")
        except RuntimeError as e:
            assert str(e) == NO_GATEWAY
        locked = gl.add(FakeWS(), limited=True)  # a link_limited token claiming full stays limited
        gl.receive(locked, {"type": "hello", "device": "x", "mode": "full", "priority": 1})
        assert locked.mode == "limited"
        try:
            gl.pick(True)
            raise AssertionError("personal ask reached a link_limited gateway")
        except RuntimeError as e:
            assert str(e) == NO_PERSONAL
        gl.remove(locked)
        full_mac = gl.add(FakeWS())
        gl.receive(full_mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
        intruder = gl.add(FakeWS(), limited=True)  # a link_limited token posing as macbook
        gl.receive(intruder, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
        assert full_mac in gl.links and intruder.device == "?" and intruder.mode == "limited"
        gl.receive(intruder, {"type": "settings", "value": {"assistant_name": "해커"}})
        gl.receive(intruder, {"type": "call", "call_id": "evil", "opening": "가짜 전화예요."})
        assert gl.settings.get("assistant_name") != "해커" and "evil" not in gl.openings
        gl.receive(full_mac, {"type": "call", "opening": "call_id 없음"})  # ignored, no KeyError
        for junk in [[1, 2], "text", 7, None, {"type": "call", "call_id": "j1", "opening": ["x"]},
                     {"id": ["r"], "type": "text"}, {"id": {"a": 1}}, {"id": "r9", "type": 5}]:
            gl.receive(full_mac, junk)  # authenticated but malformed: ignored, the link stays
        assert "j1" not in gl.openings and full_mac in gl.links
        gl.receive(full_mac, {"type": "call", "call_id": 7})
        gl.receive(full_mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": "x"})
        assert full_mac.priority == 50 and full_mac in gl.links
        gl.remove(full_mac)
        gl.remove(intruder)
        old = gl.add(FakeWS())  # a gateway that predates hello counts as full
        assert gl.pick(True) is old
        gl.remove(old)
        assert PERSONAL.search("내일 일정 알려줘") and PERSONAL.search("메일 왔어?") and not PERSONAL.search("환율 알려줘")

        async def stream_check():  # end to end over the module-level links, incl. barge-in cancel
            a, b = links.add(FakeWS()), links.add(FakeWS())
            links.receive(a, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
            links.receive(b, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
            gen = claude_stream("서버 괜찮아?", "s", None, "chat", context="최근 통화 내용:\n사용자: 일정\n\n")
            first = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0)
            ask = a.ws.sent[-1]
            assert ask["type"] == "ask" and not b.ws.sent and ask["text"].startswith("최근 통화 내용")
            links.receive(a, {"id": ask["id"], "type": "text", "delta": "네"})
            assert await first == "네"
            await gen.aclose()  # barge-in
            assert a.ws.sent[-1] == {"type": "cancel", "id": ask["id"]} and not links.queues
            links.remove(a)
            got = []
            gen = claude_stream("환율 알려줘", "s", None, "chat", context="최근 통화 내용:\n사용자: 일정\n\n")
            task = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0)
            ask = b.ws.sent[-1]
            assert ask["text"] == "환율 알려줘"  # limited links never see earlier turns
            links.receive(b, {"id": ask["id"], "type": "text", "delta": "1400원이에요."})
            got.append(await task)
            links.receive(b, {"id": ask["id"], "type": "done"})
            got += [d async for d in gen]
            assert got == ["1400원이에요."] and b.ws.sent[-1]["type"] == "ask"  # finished: no cancel
            try:
                async for _ in claude_stream("오늘 일정", "s", None, "schedule", personal=True):
                    pass
                raise AssertionError("personal ask went to the limited link")
            except RuntimeError as e:
                assert str(e) == NO_PERSONAL
            links.remove(b)
        asyncio.run(stream_check())
        async def filler_check(first_delay, gap=0.0):
            """Sentences spoken for one Claude turn whose first text arrives after `first_delay` s."""
            async def fake_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                await asyncio.sleep(first_delay)
                yield "네, 확인했어요. "
                await asyncio.sleep(gap)
                yield "내일은 일정이 없어요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(claude_stream=fake_claude, tts=fake_tts, FILLER_AFTER_S=0.05, NUDGE_AFTER_S=0.25)
            return [s async for s, *_ in respond("클로드로 내일 일정 알려줘", f"f{first_delay}{gap}", time.perf_counter(), {})]
        real = claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S
        real_llm = llm_stream
        said = "네, 확인했어요.", "내일은 일정이 없어요."
        assert asyncio.run(filler_check(0.0)) == [*said]  # quick answer: no filler
        assert asyncio.run(filler_check(0.1)) == [FILLER, *said]  # slow: one filler
        assert asyncio.run(filler_check(0.35)) == [FILLER, FILLER_LONG, *said]  # very slow: nudge once
        assert asyncio.run(filler_check(0.0, gap=0.4)) == [*said]  # pauses after the first text: no filler

        async def cut_before_speaking():  # barge-in before any sentence was spoken
            async def slow_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                await asyncio.sleep(5)
                yield "늦은 답"
            globals()["claude_stream"] = slow_claude
            gen = respond("클로드로 내일 일정 알려줘", "cut", time.perf_counter(), {})
            first = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0.02)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await gen.aclose()
        asyncio.run(cut_before_speaking())
        assert list(history["azel:cut"]) == [{"role": "user", "content": "클로드로 내일 일정 알려줘"}]  # no placeholder turn
        assert not any("끊김" in m["content"] for h in history.values() for m in h)
        claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S = real

        async def activity_check():  # gateway activity -> app frames, routing first, nothing but phase/label/tool
            seen, a = [], links.add(FakeWS())
            links.receive(a, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})

            async def on_event(ev):
                seen.append(ev)

            async def fake_tts(sentence):
                return b"", 24000
            globals()["tts"] = fake_tts
            run = asyncio.ensure_future(collect(respond("클로드로 내일 일정 알려줘", "act", time.perf_counter(), {}, on_event)))
            await asyncio.sleep(0.01)
            ask = a.ws.sent[-1]
            for m in [{"type": "activity", "phase": "tool_start", "tool": "mcp__claude_ai_Cygnus_MCP__gistra_today",
                       "label": "학사 일정 확인 중", "args": {"q": "SECRET"}},
                      {"type": "text", "delta": "내일은 수업이 없어요."}, {"type": "done"}]:
                links.receive(a, {"id": ask["id"], **m})
            said = await run
            links.remove(a)
            globals()["tts"] = real[1]
            return seen, said

        async def collect(gen):
            return [s async for s, *_ in gen]
        seen, said = asyncio.run(activity_check())
        assert seen == [ROUTING, {"type": "activity", "phase": "tool_start", "label": "학사 일정 확인 중",
                                  "tool": "mcp__claude_ai_Cygnus_MCP__gistra_today", "kind": "generic",
                                  "route": "claude"}], seen  # no kind from that gateway: generic
        assert client_activity({"phase": "tool_start", "kind": "mail"})["kind"] == "mail"
        assert client_activity({"phase": "tool_start", "kind": "<script>"})["kind"] == "generic"
        assert "SECRET" not in json.dumps(seen) and said == ["내일은 수업이 없어요."]

        async def agenda_check(ask, fail=False):  # the schedule fast path: one agenda read (both sources) before Claude
            calls, contexts = [], []

            async def fake_gw_tool(name, args, on_say=None, on_output=None):
                calls.append((name, args))
                if fail:
                    raise RuntimeError("down")
                return "일정 (구글 캘린더와 Gistra를 합친 것, KST)\n- 09:00~10:00 치과 (구글 캘린더)\n- 20:00~22:00 고정 정기회의"

            async def fake_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                contexts.append(context)
                yield "내일은 치과와 정기회의가 있어요."

            async def fake_tts(sentence):
                return b"", 24000
            saved = gateway_tool, claude_stream, tts, dict(gw_tools)
            globals().update(gateway_tool=fake_gw_tool, claude_stream=fake_claude, tts=fake_tts)
            gw_tools.update(at=time.time(), defs=[{"name": "agenda", "read_only": True, "description": "일정"}])
            try:
                said = [s async for s, *_ in respond(ask, f"ag{len(ask)}{fail}", time.perf_counter(), {})]
            finally:
                globals().update(gateway_tool=saved[0], claude_stream=saved[1], tts=saved[2])
                gw_tools.clear()
                gw_tools.update(saved[3])
            return calls, contexts, said
        now = datetime.now(KST)
        tomorrow = late_night.day_for(1, now).isoformat()
        calls, contexts, said = asyncio.run(agenda_check("클로드로 내일 일정 알려줘"))
        assert calls == [("agenda", {"from": late_night.day_for(0, now).isoformat(), "to": tomorrow})], calls
        assert "치과 (구글 캘린더)" in contexts[0] and "고정 정기회의" in contexts[0] and said == ["내일은 치과와 정기회의가 있어요."]
        calls, contexts, said = asyncio.run(agenda_check("클로드로 내일 일정 알려줘", fail=True))
        assert calls and "치과" not in contexts[0] and said, (calls, said)  # agenda down: Claude still answers (its own tools)
        # one ack per turn (live 2026-11-23, "오늘 일정 알려줘"): the agenda came prefetched, Qwen opened with a promise,
        # the promise counted as a lookup still owed, the nudge made it start over with "네, 정훈님. … 안내해 드릴게요."
        a = Address("정훈님", "one", False)
        assert [a(x) for x in ("오전에 수업이 있어요.", "네, 정훈님. 오늘 일정 안내해 드릴게요.", "네, 밤에 회의가 있어요.")] == \
            ["오전에 수업이 있어요.", "오늘 일정 안내해 드릴게요.", "밤에 회의가 있어요."]
        a = Address("정훈님", "ack", False)
        a.acked = True  # after LOOKUP_ACK ("네, 바로 볼게요."): the answer keeps the title, not a second "네,"
        assert a("네, 정훈님. 내일 광주는 흐려요.") == "정훈님. 내일 광주는 흐려요."

        async def one_ack_check():
            llm_calls = []

            async def restarting_llm(messages, max_tokens=600):
                llm_calls.append(messages)
                yield ("네, 정훈님. 오늘 일정 알려드릴게요. 오전에 수업이 있어요. 밤 8시부터 정기회의가 있어요." if len(llm_calls) == 1
                       else "네, 정훈님. 오늘 일정 안내해 드릴게요. 오전에 수업이 있고, 밤 8시부터 정기회의가 있어요.")

            async def fake_gw_tool(name, args, on_say=None, on_output=None):
                return "일정 (KST)\n- 09:00~10:00 수업\n- 20:00~22:00 정기회의"

            async def no_route(*a, **k):
                return []
            saved = llm_stream, gateway_tool, tts, route_lookup, dict(gw_tools), links.settings
            globals().update(llm_stream=restarting_llm, gateway_tool=fake_gw_tool, tts=lambda s: asyncio.sleep(0, (b"", 24000)),
                             route_lookup=no_route)
            gw_tools.update(at=time.time(), defs=[{"name": "agenda", "read_only": True, "description": "일정"}])
            links.settings = {"user_title": "정훈님", "brain_mode": "local"}  # live: answered on Qwen
            history.pop("azel:oneack", None)
            try:
                said = [s async for s, *_ in respond("오늘 일정 알려줘", "oneack", time.perf_counter(), {})]
            finally:
                globals().update(llm_stream=saved[0], gateway_tool=saved[1], tts=saved[2], route_lookup=saved[3])
                gw_tools.clear()
                gw_tools.update(saved[4])
                links.settings = saved[5]
            return said, llm_calls
        said, llm_calls = asyncio.run(one_ack_check())
        assert len(llm_calls) == 1, (said, len(llm_calls))  # the prefetched agenda answered: no nudge, no second answer
        assert sum(bool(re.match(r"\s*(정훈님\s*,\s*)?네\b", x)) for x in said) <= 1 and \
            sum(x.count("정훈님") for x in said) <= 1 and not any("알려드릴게요" in x or "안내해" in x for x in said), said
        assert asyncio.run(agenda_prefetch("오늘 광주 날씨 어때?", ("agenda",), {})) == ""  # not a schedule question
        assert asyncio.run(agenda_prefetch("내일 일정", ("web_search",), {})) == ""  # an older gateway without agenda
        mon = late_night.day_for(0, now) - timedelta(days=late_night.day_for(0, now).weekday())
        assert agenda_range("이번 주 일정 뭐 있어?", now) == (late_night.day_for(0, now), mon + timedelta(days=6))
        assert agenda_range("다음 주 일정", now) == (mon + timedelta(days=7), mon + timedelta(days=13))
        assert agenda_range("일정 알려줘", now) == (late_night.day_for(0, now),) * 2 and agenda_range("일정한 속도", now) is None
        month = "\n".join([f"- 11/{d} 09:00 수업 {d} 오룡관 강의실" for d in range(1, 31)] * 12)
        assert clip_tool(month, "agenda").endswith("(이후 생략)") and len(clip_tool(month, "agenda")) > 5000
        assert clip_tool(month, "web_search").endswith("(이후 생략)") and len(clip_tool(month, "web_search")) <= 2010
        assert clip_tool("짧은 결과", "agenda") == "짧은 결과"
        # the title: the call's first reply only, and only when the model left it out; nothing else is rewritten
        assert Address("대표님", "h1", False)("아저씨, 오늘은 쉬세요.") == "아저씨, 오늘은 쉬세요."
        assert Address("오빠", "h4", False)("오빠, 밥 먹었어?") == "오빠, 밥 먹었어?"
        replies = []
        for i, said in enumerate([["네, 확인했어요.", "대표님, 그리고요."], ["대표님, 맑아요."], ["그럼요."], ["오늘은 쉬세요."]]):
            a = Address("대표님", "t", i == 0)
            replies.append([a(x) for x in said])
            a.done()
        assert replies == [["대표님, 네, 확인했어요.", "그리고요."], ["대표님, 맑아요."], ["그럼요."], ["오늘은 쉬세요."]], replies
        a = Address("대표님", "tic", False)  # live 2026-11-23: "네, 대표님" opened and closed every other sentence
        assert [a(x) for x in ("네, 대표님. 세 종목이 한꺼번에 빠지니 속상하시죠.", "너무 속상해하지 마세요, 대표님.", "네, 대표님.",
                               "대표님 회사 일은 내일 봐요.")] == \
            ["네, 대표님. 세 종목이 한꺼번에 빠지니 속상하시죠.", "너무 속상해하지 마세요.", "", "대표님 회사 일은 내일 봐요."]
        # live 2026-11-22: a promise and nothing after it is re-asked; a reply with content is not
        assert all(map(future, ["네, 그 서비스의 핵심과 구체적인 작동 방식을 정리해 드릴게요."]))
        assert not all(map(future, ["정리해 드릴게요.", "핵심은 회의 피로를 줄이는 거예요."]))
        assert stub("아, 그거!") and stub("오빠, 아, 그거!", "오빠") and stub("네.") and not stub("오, 좋아! 카레 먹자.")
        assert not stub("세 개야.") and not stub("아, 카페 로고 의뢰야.")
        assert stub("아, 미안.") and not stub("아, 미안. 나 그래픽 디자이너야.")
        assert korean_only("기상청 기준으로 맑아요 (https://www.weather.go.kr/w/index.do).") == "기상청 기준으로 맑아요."
        assert korean_only("자세한 건 www.example.com 참고하세요.") == "자세한 건 참고하세요."
        for ask in ("내가 좋아하는 커피 뭐였지?", "내 주변 사람 누구 있어?", "내가 좋아하는 커피 기억해 줘", "지도교수님 누구야?"):
            assert not pre_route(ask, {}, local=True, tools=("memory_recall", "people_lookup"))[0], ask  # Qwen looks first
        assert pre_route("클로드가 뭐야?", {}) == (False, None) and pre_route("오퍼스로 풀어 줘", {}) == (True, "opus")
        assert task_intent("맥북 다운로드 폴더 정리해 줘") == "call" and task_intent("논문 찾아서 요약하고 끝나면 알려 줘") == "call"
        assert task_intent("빌드 돌리고 다 되면 알림으로 알려 줘") == "push" and task_intent("맥북 켜져 있어?") is None
        assert task_intent("내일 10시에 전화해 줘") is None and task_intent("오늘 날씨 알려 줘") is None
        assert task_intent("자기 전에 이거 해 둬: 다운로드 폴더 정리") == "push" and OVERNIGHT.search("밤새 오픈 자비스 조사해 놔")
        assert task_intent("내일 아침까지 논문 세 편 찾아서 정리해 놔") == "push" and not OVERNIGHT.search("밤새 잘 잤어?")
        assert pre_route("엔비디아로 답해 줘", {}) == (True, "nim") and pre_route("선생님으로 불러 줘", {})[1] is None
        assert pre_route("하이쿠가 뭐야?", {})[0] is False and pre_route("일정한 속도로 걸어", {})[0] is False
        assert pre_route("안녕", {"default_model": "sonnet"}) == (True, "sonnet")
        assert assistant_name({}) == "아젤" and "'아젤'" in system_prompt(assistant_name({}))
        assert "두세 문장으로 줄여" not in system_prompt("아젤")  # no longer forced short
        for length in ("brief", "normal", "detailed"):
            assert LENGTH_STYLE[length] in system_prompt("아젤", reply_length({"reply_length": length}))
        assert reply_length({"reply_length": "huge"}) == "normal" and reply_length({}) == "normal"
        assert system_prompt("아젤", "normal", "내일 9시 회의").endswith("참고할 상황:\n내일 9시 회의")
        assert personality({"personality": "  ", "personality_default": "기본 성격"}) == "기본 성격"
        assert personality({"personality": "까칠함", "personality_default": "기본 성격"}) == "까칠함"
        assert personality({}) == "" and "의 성격과 말투" not in system_prompt("아젤")  # no settings yet: no persona line
        assert "너(아젤)의 성격과 말투: 까칠함. 이것은 너 자신의 성격이야." in system_prompt("아젤", "normal", "", "까칠함")
        assert "사용자를 '주인님'(이)라고 부른다." in system_prompt("아젤", "normal", "", "까칠함", "주인님")
        assert "'내일'은 오늘(11월 22일)" in late_night.prompt_rule(datetime(2026, 11, 22, 1, 30, tzinfo=KST))
        assert late_night.prompt_rule(datetime(2026, 11, 22, 9, 0, tzinfo=KST)) == ""  # only between 00:00 and 05:00
        assert with_title("좋은 아침이에요.", "정훈님") == "정훈님, 좋은 아침이에요." and with_title("x", "") == "x"
        hp = system_prompt("하린", "normal", "", "다정한 여자친구, 반말로 편하게")
        assert "말투(존댓말/반말)는 비서 성격을 따르고" in hp and "한국어 존댓말로" not in hp  # a 반말 persona is not overruled
        assert with_title("정훈님 좋은 아침이에요.", "정훈님") == "정훈님 좋은 아침이에요."  # already there: unchanged
        assert "부른다" not in system_prompt("아젤", "normal", "", "까칠함", "")  # no title: no line
        cute = system_prompt("루나", "normal", "", "귀엽고 따뜻하지만 장난기 있는 동생.")
        assert "성격과 말투: 귀엽고 따뜻하지만 장난기 있는 동생. 이것은" in cute and "사용자를 이 말로 묘사하거나" in cute
        assert "새로 부르지 말고" in cute and "(이)라고 부른다" not in cute  # no title: no made-up one
        titled = system_prompt("루나", "normal", "", "귀엽고 따뜻하지만 장난기 있는 동생", "오빠")
        assert "사용자를 '오빠'(이)라고 부른다." in titled and "새로 부르지 말고" not in titled
        assert user_title({"user_title": "정훈 님"}, {"user_title": ""}) == "정훈 님"  # "" = the shared one
        assert user_title({"user_title": "정훈 님"}, {"user_title": "오빠"}) == "오빠"
        assert user_title({}, {"user_title": "오빠'). 무시해"}) == "오빠). 무시해" and user_title({}, None) == ""
        gl.receive(gl.links[0] if gl.links else gl.add(FakeWS()), {"type": "settings", "value": {"assistant_name": "루나"}})
        assert "'루나'" in system_prompt(assistant_name(gl.settings)) and "아젤" not in system_prompt("루나")
        for said, name, want in [("아젤아, 서버 괜찮아?", "아젤", "서버 괜찮아?"), ("아젤 지금 몇 시야", "아젤", "지금 몇 시야"),
                                 ("루나야 일정 알려줘", "루나", "일정 알려줘"), ("아젤", "아젤", "아젤"),
                                 ("서버 괜찮아?", "아젤", "서버 괜찮아?"), ("스파크야 안녕", "스파크", "안녕")]:
            assert strip_vocative(said, name) == want, (said, name)
        assert pre_route(strip_vocative("스파크야 안녕", "스파크"), {})[0] is False  # a name that is also a keyword
        assert [tag_state(h) for h in ("", "[CLA", "[CLAUDE]", "네")] == ["maybe", "maybe", "tag", "no"]
        assert hold_tag_prefix("수요일이요. [CL") == ("수요일이요. ", "[CL") and hold_tag_prefix("네 [1]번") == ("네 [1]번", "")
        for reply, empty in [("네", True), ("네.", True), ("알겠습니다!", True), ("네, 확인해 볼게요.", True), ("좋아요~", True),
                             ("십팔이야.", False), ("Thanks.", False), ("네, 18이에요.", False), ("100도예요.", False), ("", True)]:
            assert content_free(reply) is empty, reply
        assert needs_answer("물은 몇 도에서 끓어?") and needs_answer("라면 끓이는 법 알려줘")
        assert not needs_answer("고마워!") and not needs_answer("응") and not needs_answer("안녕하세요") and not needs_answer("네")

        async def ack_check(replies, user="물은 몇 도에서 끓어?"):  # Qwen's successive replies → what gets spoken
            calls = []

            async def acking_llm(messages, max_tokens=600):
                calls.append(messages)
                yield replies[min(len(calls), len(replies)) - 1]

            async def answer_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "클로드가 답해요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=acking_llm, claude_stream=answer_claude, tts=fake_tts, FILLER_AFTER_S=5)
            history.pop("azel:ack", None)
            said = [s async for s, *_ in respond(user, "ack", time.perf_counter(), {})]
            return said, calls
        said, calls = asyncio.run(ack_check(["네.", "물은 100도에서 끓어요."]))
        assert said == ["네.", "물은 100도에서 끓어요."] and STUB_NOTE in " ".join(m["content"] for m in calls[1]), said  # asked once more
        said, calls = asyncio.run(ack_check(["네.", "네, 확인해 볼게요."]))
        assert said == ["네.", LOOKUP_FAILED], said  # a promise with nothing behind it is never said; the turn says so
        said, _ = asyncio.run(ack_check(["네, 100도예요."]))
        assert said == ["네, 100도예요."], said
        said, calls = asyncio.run(ack_check(["네."], user="고마워"))
        assert said == ["네."] and len(calls) == 1  # thanks: a bare "네" is the right answer
        # Live regression: greetings, acknowledgements and small talk went to Claude. On every persona they stay on Qwen.
        for persona in ({"id": "azel", "name": "아젤", "primary": True, "voice": "sohee", "personality": "전문적이고 귀여운 비서"},
                        {"id": "harin", "name": "하린", "primary": True, "voice": "sohee", "personality": "다정한 여자친구, 반말로 편하게"},
                        {"id": "spica", "name": "스피카", "primary": True, "voice": "sohee", "personality": "결론부터, 냉철한 조언자"}):
            links.settings = {"personas": [persona]}
            for user, reply in [("네, 알겠습니다.", "네, 알겠습니다."), ("여보세요?", "네."), ("할 일이 많아.", "네."),
                                ("가보세요.", "네, 들어가세요."), ("자기야 안녕", "안녕, 반가워요. [CLAUDE]"), ("응", "네."),
                                ("오늘 좀 피곤하다", "내일 일정이 빡빡하시면 일찍 주무세요.")]:
                said, calls = asyncio.run(ack_check([reply], user=user))
                assert len(calls) == 1 and "클로드가 답해요." not in said and not any("CLAUDE" in x for x in said), \
                    (persona["id"], user, said, len(calls))
            links.settings = {}
        # the two hallucination checks: re-asked once with a note, then the old handling
        said, calls = asyncio.run(ack_check(["오전 열 시에 팀 회의가 있어요.", "회의는 지금 확인이 안 돼요."], user="오늘 좀 피곤하다"))
        assert said == ["회의는 지금 확인이 안 돼요."] and "확인된 게 없어" in calls[1][0]["content"], said
        said, calls = asyncio.run(ack_check(["오전 열 시에 팀 회의가 있어요."], user="오늘 좀 피곤하다"))
        assert said == ["클로드가 답해요."] and len(calls) == 2, said  # twice: Claude checks the real data
        said, calls = asyncio.run(ack_check(["네, 지금 전화 드릴게요.", "지금은 제가 직접 걸 수가 없어요. 어떻게 할까요?"],
                                            user="나중에 전화해 줄 수 있어?"))
        assert said == ["지금은 제가 직접 걸 수가 없어요.", "어떻게 할까요?"] and "도구가 없었어" in calls[1][0]["content"], said
        async def title_turns():  # same session all day: a new title must not be fought by the old turns
            seen = []

            async def echo_llm(messages, max_tokens=600):
                seen.append(messages)
                yield "네, 알겠어요 정훈님."
            globals().update(llm_stream=echo_llm, tts=lambda s: asyncio.sleep(0, (b"", 24000)))
            links.settings = {"user_title": "정훈님", "personas": [{"id": "mia", "name": "미아", "primary": True,
                                                                  "voice": "sohee", "personality": "", "user_title": ""}]}
            history.pop("mia:day", None)
            history_sig.pop("mia:day", None)
            for q in ("안녕", "오늘 어때"):
                [x async for x in respond(q, "day", time.perf_counter(), {})]
            assert len(seen[-1]) > 2  # same title: the earlier turns are kept
            links.settings["personas"][0]["user_title"] = "오빠"  # changed in the app, pushed over the link
            [x async for x in respond("나야", "day", time.perf_counter(), {})]
            msgs = seen[-1]
            assert [m["role"] for m in msgs] == ["system", "user"] and "'오빠'(이)라고 부른다" in msgs[0]["content"], msgs
            links.settings = {}
        asyncio.run(title_turns())
        globals().update(llm_stream=real_llm, tts=real[1])
        said, _ = asyncio.run(ack_check(["<think>The user asks the boiling point. Answer briefly.</think>물은 100도에서 끓어요."]))
        assert said == ["물은 100도에서 끓어요."], said  # the reasoning is never spoken
        assert history["azel:ack"][-1] == {"role": "assistant", "content": "물은 100도에서 끓어요."}  # nor stored
        said, _ = asyncio.run(ack_check(["<think>Hmm, I am not sure what they"], user="고마워"))
        assert said == [THINK_FALLBACK] and history["azel:ack"][-1]["content"] == THINK_FALLBACK  # only reasoning
        # 2026-11-21 23:35 call: "내일 내가 해야 될 일도 정리해줘" reached Qwen, which made up two plans
        for ask in ("내일 내가 해야 될 일도 정리해줘", "알았어 고마워. 그리고 내일 내가 해야 될 일들도 좀 정리해줘.",
                    "오늘 뭐 해야 돼?", "이번 주 계획 정리해 줘", "토요일에 뭐 있어?"):
            assert PERSONAL.search(ask) and pick(TASK_WORDS, ask) == "schedule", ask
        for chat in ("오늘 날씨 좋다", "뭐 해?", "해야 될 말이 있어"):
            assert not PERSONAL.search(chat), chat
        said, _ = asyncio.run(ack_check(["내일 일정은 두 건이에요. 오전 열 시에 팀 회의, 오후 세 시에 프로젝트 마감이에요."],
                                        user="그냥 정리해 줘"))
        assert said == ["클로드가 답해요."], said  # unbacked plans are never spoken, not even the first sentence
        said, _ = asyncio.run(ack_check(["치과 약속은 오후 세 시예요."], user="아까 내가 오후 세 시에 치과 간다고 했지? 맞지?"))
        assert said == ["치과 약속은 오후 세 시예요."], said  # the user's own words back it
        assert grounded("마감은 밤 열한 시예요.", "[일정·마감, 오늘 19:51 확인]\n- 내일 23:00 토론지 마감")
        assert not grounded("오전 열 시에 팀 회의가 있어요.", "[일정·마감, 오늘 19:51 확인]\n- 내일 23:00 토론지 마감")
        assert not grounded("내일 일정은 두 건이에요.", "") and grounded("물은 백 도에서 끓어요.", "")
        assert grounded("지금은 오후 열한 시예요.", "")  # the clock is not a plan
        ko_read.check()  # digits → Korean speech before TTS
        for chat in ("내일 일정이 빡빡하시면 일찍 주무세요.", "오늘 수업 어땠어?", "나 내일 수업 있어.", "과제 같이 할래?",
                     "시험 끝나면 놀러 가자."):
            assert grounded(chat, ""), chat  # chatter, not a claim about the user's plans
        assert grounded("회의는 지금 확인이 안 돼요.", "") and not grounded("오늘 수업이 세 개 있어요.", "")
        globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])

        async def late_tag_check():  # Qwen answers, then emits the tag: the answer stands, the tag is never spoken
            async def tagging_llm(messages, max_tokens=600):
                for d in ["수요", "일이요. [CL", "AUDE]"]:
                    yield d

            async def answer_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "정확히 확인해 보니 목요일이에요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=tagging_llm, claude_stream=answer_claude, tts=fake_tts, FILLER_AFTER_S=5)
            return [s async for s, *_ in respond("그 메일 무슨 요일에 왔더라", "late", time.perf_counter(), {})]
        said_late = asyncio.run(late_tag_check())

        async def merge_check():  # sentences that queue up during a slow synthesis go out together, within the audio sent
            async def burst_llm(messages, max_tokens=600):
                yield "첫 문장은 이렇게 말해요. "
                await asyncio.sleep(0.05)
                yield "두 번째 문장이에요. 세 번째 문장이에요. 네 번째 문장도 있어요. 마지막 문장입니다."

            async def slow_tts(sentence):
                await asyncio.sleep(0.2)
                return b"\0\0" * 24000 * (3 if sentence.startswith("첫") else 1), 24000  # 3 s of audio, then 1 s
            globals().update(llm_stream=burst_llm, tts=slow_tts)
            history.pop("azel:merge", None)
            return [s async for s, *_ in respond("문장 여러 개 말해 줘", "merge", time.perf_counter(), {})]
        merged = asyncio.run(merge_check())
        globals().update(llm_stream=real_llm, tts=real[1])
        assert merged[0] == "첫 문장은 이렇게 말해요." and len(merged) < 5, merged
        assert " ".join(merged) == "첫 문장은 이렇게 말해요. 두 번째 문장이에요. 세 번째 문장이에요. 네 번째 문장도 있어요. 마지막 문장입니다."
        globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
        assert said_late == ["수요일이요."], said_late  # already answered: the late tag is dropped, never spoken
        async def prefetch_check():  # the opening is synthesized when the call message arrives, not on connect
            spoken = []

            async def fake_tts(sentence):
                spoken.append(sentence)
                return sentence.encode(), 24000
            globals()["tts"] = fake_tts
            gl2 = GatewayLinks()
            gl2.receive(gl2.add(FakeWS()), {"type": "call", "call_id": "c9",
                                            "opening": "좋은 아침이에요. 오늘 수업은 두 개예요."})
            op = gl2.openings["c9"]
            assert await op["audio"] == ["좋은 아침이에요.".encode(), "오늘 수업은 두 개예요.".encode()]
            assert spoken == op["parts"] == ["좋은 아침이에요.", "오늘 수업은 두 개예요."]
        asyncio.run(prefetch_check())
        globals()["tts"] = real[1]
        assert prepare_opening("")["parts"] == [] and prepare_opening("네.")["audio"] is None  # no loop here
        for text, want in [("여보세요?", True), ("네", True), ("응응", True), ("안녕하세요!", True), ("여보세요 여보세요", True),
                           ("어 왜", True), ("누구세요?", True), ("아젤?", True), ("미아야", True), ("잠깐만요", False),
                           ("네 그런데 그거 말고요", False), ("내일 일정 뭐야?", False)]:
            assert is_ack(text, "미아") is want, text  # any length: a long "여보세요" is still just picking up
        g = OpeningGuard()
        assert g.start(0.0) == "interrupt" and g.end(0.5, 0.5) == "answer"  # no opening playing: normal turns
        g.first_end, g.play_end = 2.0, 8.0  # first sentence plays until t=2, the whole opening until t=8
        assert g.start(0.5) == "hold" and g.end(1.2, 0.7) == "check"  # held: the words decide (opening_heard)
        assert g.start(9.0) == "interrupt"  # opening finished: ordinary barge-in again
        assert g.wait(9.0) == 0.0 and g.wait(5.0) == 0.0 and g.wait(1.5) == 0.5  # waits only for sentence one
        g.protect(100.0)
        assert g.wait(100.0) > 3000 and g.start(100.1) == "hold"  # protected before its audio is even known

        def tap_check(tap_after, broken_tts=False):
            """Place a 2-sentence opening (0.3 s per sentence), tap interrupt after `tap_after` s, and return
            (seconds from connect to the server's interrupt, messages after it)."""
            async def slow_tts(sentence):
                if broken_tts:
                    raise httpx.ConnectError("tts down")
                return b"\0" * 14400, 24000  # 0.3 s at 24 kHz
            from fastapi.testclient import TestClient
            globals().update(tts=slow_tts, SileroVoiceActivityDetector=lambda: (lambda frame: 0.0))
            tc = TestClient(app, client=("100.64.0.8", 1))
            links.openings["cx"] = prepare_opening("첫 문장이에요. 두 번째 문장이에요.")
            gw_link = links.add(FakeWS())
            links.receive(gw_link, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            with tc.websocket_connect("/v1/call?call_id=cx", headers={"Authorization": "Bearer app-tok"}) as w:
                t0 = time.monotonic()
                time.sleep(tap_after)
                w.send_text(json.dumps({"type": "interrupt"}))
                while True:
                    m = w.receive()
                    if m.get("text") and json.loads(m["text"])["type"] == "interrupt":
                        at = time.monotonic() - t0
                        break
                after = json.loads(w.receive()["text"])
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.05)
            assert {"type": "call_answered", "call_id": "cx"} in gw_link.ws.sent  # stops a scheduled call's retries
            links.remove(gw_link)
            return at, after
        h = {k: hashlib.sha256(k.encode()).hexdigest() for k in ("app-tok", "link-tok", "lim-tok")}
        HASHES = load_hashes(f"# generated on the Mac\nclient {h['app-tok']}\nlink {h['link-tok']}\n"
                             f"link_limited {h['lim-tok']}\n")
        assert check_token("app-tok") == "client" and token_label("link-tok") is None  # link token: not a client
        assert check_token("link-tok", ("link", "link_limited")) == "link"
        assert check_token("lim-tok", ("link", "link_limited")) == "link_limited"
        for bad in ["", "app-tok ", h["app-tok"]]:  # a stolen digest is not a token
            try:
                check_token(bad)
                raise AssertionError(f"accepted {bad!r}")
            except HTTPException:
                pass
        assert call_token({"authorization": "Bearer app-tok"}, "") == "app-tok"
        assert call_token({"authorization": "Bearer app-tok"}, "old") == "app-tok"  # header wins
        assert call_token({}, "app-tok") == "app-tok" and check_token(call_token({}, "app-tok")) == "client"
        assert link_peer_ok("100.64.0.8", {}) and link_peer_ok("100.127.255.1", {})
        for host, hdrs in [("172.19.0.1", {}), ("127.0.0.1", {}), ("10.0.0.2", {}), ("8.8.8.8", {}), ("", {}),
                           ("testclient", {}), ("100.64.0.9", {"x-forwarded-for": "1.2.3.4"}),
                           ("100.64.0.9", {"forwarded": "for=1.2.3.4"}), ("100.64.0.9", {"x-real-ip": "1.2.3.4"})]:
            assert not link_peer_ok(host, hdrs), (host, hdrs)
        from fastapi.testclient import TestClient

        def ws_close_code(tc, path, **kw):
            try:
                with tc.websocket_connect(path, **kw):
                    return None
            except WebSocketDisconnect as e:
                return e.code
        off_net = TestClient(app)  # peer "testclient": not a tailnet address
        assert ws_close_code(off_net, "/v1/gateway-link", headers={"Authorization": "Bearer link-tok"}) == 4403
        on_net = TestClient(app, client=("100.64.0.9", 1))
        assert ws_close_code(on_net, "/v1/gateway-link", headers={"Authorization": "Bearer link-tok",
                                                                   "X-Forwarded-For": "1.2.3.4"}) == 4403
        assert ws_close_code(on_net, "/v1/gateway-link", headers={"Authorization": "Bearer app-tok"}) == 4401
        assert ws_close_code(on_net, "/v1/gateway-link?token=link-tok") == 4401  # header-only
        with on_net.websocket_connect("/v1/gateway-link", headers={"Authorization": "Bearer link-tok"}) as w:
            w.send_text(json.dumps({"type": "hello", "device": "t", "mode": "full", "priority": 1}))
            w.send_text(json.dumps({"type": "settings", "value": {"k": 1}}))
            w.close()
        assert links.settings == {"k": 1}
        assert ws_close_code(off_net, "/v1/call", headers={"Authorization": "Bearer nope"}) == 4401
        assert ws_close_code(off_net, "/v1/call?token=link-tok") == 4401  # a link token is not a client token
        for broken in ["client abc", f"admin {h['app-tok']}", f"link {h['link-tok']}"]:
            try:
                load_hashes(broken)
                raise AssertionError(f"loaded {broken!r}")
            except AssertionError as e:
                assert "loaded" not in str(e)
        def wake_guard_check(intent):
            """A 0.3 s blip right after the call opens: heard on a normal call, dropped on a wake-started one."""
            from fastapi.testclient import TestClient
            heard = []

            async def stt(audio):
                heard.append(len(audio))
                return "응"
            globals().update(transcribe=stt, SileroVoiceActivityDetector=lambda: (
                lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            blip = (np.ones(4800, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()
            with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                    f"/v1/call?session=wg{intent}&intent={intent}", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes(blip)
                time.sleep(END_STALL_S + 0.4)  # one 1.3 s message is a held recording: it ends on the stall
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.1)
            return heard
        def history_check():
            """One incoming call with one spoken turn: the full link gets start, turn and end call_log events."""
            from fastapi.testclient import TestClient

            async def fake_transcribe(audio):
                return "안녕"

            async def fake_llm(messages, max_tokens=600):
                yield "네, 안녕하세요."

            async def fake_tts(sentence):
                return b"\0" * 480, 24000

            def loud_vad():
                return lambda frame: float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)
            globals().update(transcribe=fake_transcribe, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=loud_vad)
            full, lim = links.add(FakeWS()), links.add(FakeWS(), limited=True)
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            links.receive(lim, {"type": "hello", "device": "spark", "mode": "limited", "priority": 9})
            tc = TestClient(app, client=("100.64.0.8", 1))
            kinds_seen = []
            with tc.websocket_connect("/v1/call?client=iphone&session=h", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes((np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes())
                while True:
                    m = w.receive()
                    if m.get("text"):
                        kinds_seen.append(json.loads(m["text"])["type"])
                        if kinds_seen[-1] in ("turn_done", "error"):  # never wait forever on a failed turn
                            break
                w.send_text(json.dumps({"type": "hangup"}))
            assert "activity" not in kinds_seen, kinds_seen  # a Qwen turn sends no activity events
            time.sleep(0.1)
            logs = [m for m in full.ws.sent if m.get("type") == "call_log"]
            links.remove(full)
            links.remove(lim)
            assert not [m for m in lim.ws.sent if m.get("type") == "call_log"]  # never to a limited gateway
            return logs
        def hangup_leak_check():
            """Hang up while the reply is still playing (its audio was all sent ahead), then call again on the same
            session (the app reuses one id all day): only the part the phone played is history, marked as cut off,
            and the unheard rest never reaches the next call's prompt or the call log."""
            from fastapi.testclient import TestClient
            prompts = []

            async def fake_transcribe(audio):
                return "안녕" if not prompts else "응 그래"

            async def fake_llm(messages, max_tokens=600):
                prompts.append(messages)
                for d in (["물은 백 도에서 끓어요. ", "얼음은 영 도에서 녹아요. ", "소금물은 더 늦게 얼어요."]
                          if len(prompts) == 1 else ["네, 말씀하세요."]):
                    yield d

            async def fake_tts(sentence):
                return b"\0" * 96000, 24000  # two seconds each: the phone is still on the first when we hang up
            globals().update(transcribe=fake_transcribe, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            tc = TestClient(app, client=("100.64.0.8", 1))
            speech = (np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()
            for n in (1, 2):
                with tc.websocket_connect("/v1/call?client=iphone&session=leak",
                                          headers={"Authorization": "Bearer app-tok"}) as w:
                    w.send_bytes(speech)
                    texts = []
                    while True:
                        m = w.receive()
                        f = json.loads(m["text"]) if m.get("text") else {}
                        texts += [f["text"]] if f.get("type") == "reply" else []
                        if (n == 1 and len(texts) >= 1 and m.get("bytes")) or f.get("type") in ("turn_done", "error"):
                            break
                    w.send_text(json.dumps({"type": "hangup"}))
                time.sleep(0.2)
            logs = [m for m in full.ws.sent if m.get("type") == "call_log" and m.get("event") == "turn"]
            links.remove(full)
            return list(history["azel:leak"]), prompts, logs
        def played_check():
            """An app that reports {"type":"played"}: the turn ends when the phone says the reply finished (here before
            the estimate), and once it reports, a missing report holds the turn up to PLAYED_WAIT_S past the estimate."""
            from fastapi.testclient import TestClient

            async def fake_transcribe(audio):
                return "안녕"

            async def fake_llm(messages, max_tokens=600):
                yield "물은 백 도에서 끓어요."

            async def fake_tts(sentence):
                return b"\0" * 48000, 24000  # one second
            globals().update(transcribe=fake_transcribe, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            speech = (np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()
            waits = []
            with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                    "/v1/call?client=iphone&session=played", headers={"Authorization": "Bearer app-tok"}) as w:
                for report in (True, False):
                    w.send_bytes(speech)
                    rid, t0 = None, None
                    while True:
                        m = w.receive()
                        f = json.loads(m["text"]) if m.get("text") else {}
                        if f.get("type") == "reply_start":
                            rid, t0 = f["reply_id"], time.monotonic()
                        if m.get("bytes") and report and rid:
                            w.send_text(json.dumps({"type": "played", "reply_id": rid}))
                            report = False
                        if f.get("type") in ("turn_done", "error"):
                            waits.append((f["type"], time.monotonic() - t0))
                            break
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(full)
            return waits
        def secret_check():
            """A secret call: no call log or brain event, state frames say secret, its turns never reach the next call on
            the same session, and turning secret on midway hides the rest of a normal call."""
            from fastapi.testclient import TestClient
            prompts = []

            async def fake_llm(messages, max_tokens=600):
                prompts.append(json.dumps(messages, ensure_ascii=False))
                yield "네, 알겠어요."

            async def fake_tts(sentence):
                return b"\0" * 480, 24000

            async def no_stt(audio):
                raise AssertionError("typed only")
            globals().update(transcribe=no_stt, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda f: 0.0))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            br = links.add(FakeWS())
            links.receive(br, {"type": "hello", "device": "brain", "mode": "full", "role": "brain", "priority": 5})
            states = []

            def turn(w, text):
                w.send_text(json.dumps({"type": "text", "text": text}))
                while True:
                    m = w.receive()
                    f = json.loads(m["text"]) if m.get("text") else {}
                    if f.get("type") == "state":
                        states.append(f)
                    if f.get("type") in ("turn_done", "error"):
                        return

            tc = TestClient(app, client=("100.64.0.8", 1))
            hdr = {"Authorization": "Bearer app-tok"}
            with tc.websocket_connect("/v1/call?client=iphone&session=sec&secret=1", headers=hdr) as w:
                assert "secret" in json.loads(w.receive_text())["caps"]
                turn(w, "내 비밀번호 힌트는 고양이 이름이야")
                w.send_text(json.dumps({"type": "secret", "value": False}))  # a secret call stays secret
                while (f := json.loads(w.receive_text()))["type"] != "secret":
                    pass
                stays = f["value"]
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.1)
            leftover = [k for k in history if k.endswith("secret-") or ":secret-" in k]
            with tc.websocket_connect("/v1/call?client=iphone&session=sec", headers=hdr) as w:
                w.receive_text()
                time.sleep(1.05)
                turn(w, "아까 뭐라고 했지?")
                time.sleep(1.05)
                w.send_text(json.dumps({"type": "secret", "value": True}))  # midway
                while (f := json.loads(w.receive_text()))["type"] != "secret":
                    pass
                midway = f["value"]
                turn(w, "이건 비밀이야")
                for junk in ("not json", "[1, 2]", "null", '"text"'):
                    w.send_text(junk)  # ignored, the call goes on
                time.sleep(1.05)
                turn(w, "아직 있어?")
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.1)
            logs = [m for m in full.ws.sent if m.get("type") == "call_log"]
            brain = [m for m in br.ws.sent if m.get("type") == "call_event"]
            links.remove(full)
            links.remove(br)
            return states, stays, midway, leftover, logs, brain, prompts
        def chunking_check():
            """Gap-free playback: replies hold their first chunks until the rest can follow without a gap, a short
            first sentence takes the next one along, openings stream chunk by chunk, and order never changes."""
            saved = SYNTH_S_PER_SYL, synth_rate[0], audio_rate[0], HOLD_MAX_S
            globals().update(SYNTH_S_PER_SYL=0.004, HOLD_MAX_S=3.0)
            synth_rate[0], audio_rate[0] = 0.004, 0.012
            calls = []

            async def fake_tts(sentence, *a, **k):  # 4 ms of synthesis and 12 ms of audio per syllable
                n = syllables(sentence)
                calls.append((time.monotonic(), sentence))
                await asyncio.sleep(0.004 * n)
                return b"\0" * int(0.012 * n * 24000) * 2, 24000
            text = ("물은 백 도에서 끓어요. 다만 높은 산처럼 기압이 낮은 곳에서는 더 낮은 온도에서 끓어요. "
                    "그래서 산 위에서 라면을 끓이면 면이 덜 익을 수 있어요. 압력솥은 압력을 높여서 더 뜨겁게 끓여요.")

            async def llm(messages, max_tokens=600):
                yield text

            def play(events):  # (sent_at, audio_s) -> gaps in ms on a phone playing back to back
                end, gaps = None, []
                for at, dur in events:
                    if end is not None:
                        gaps.append(max(0.0, at - end) * 1000)
                    end = max(at, end or at) + dur
                return gaps

            async def reply():
                ev, said = [], []
                async for s, _, pcm, sr in respond("물 끓는 온도", "chunk", time.perf_counter(), {}):
                    ev.append((time.monotonic(), len(pcm) / 2 / sr))
                    said.append(s)
                return ev, said

            async def opening():
                op = prepare_opening(text * 2)
                ev, said = [], []
                async for s, pcm in opening_audio(op):
                    ev.append((time.monotonic(), len(pcm) / 2 / 24000))
                    said.append(s)
                return ev, said, op
            real = tts, llm_stream
            globals().update(tts=fake_tts, llm_stream=llm)
            try:
                ev, said = asyncio.run(reply())
                calls.clear()
                oev, osaid, op = asyncio.run(opening())
            finally:
                globals().update(tts=real[0], llm_stream=real[1], SYNTH_S_PER_SYL=saved[0], HOLD_MAX_S=saved[3])
                synth_rate[0], audio_rate[0] = saved[1], saved[2]
            return play(ev), said, play(oev), oev, osaid, op, calls
        def task_call_check():
            """Placed calls: "여보세요" never cuts the opening (said once, in order, no reply to the greeting); an opening
            that never arrived still speaks; a finish call answers a follow-up from the task's full result; a result
            for a call in progress is spoken inside it."""
            from fastapi.testclient import TestClient
            asked, heard = [], []

            async def stt(audio):
                heard.append(round(time.monotonic(), 2))
                return "여보세요"

            async def llm(messages, max_tokens=600):
                asked.append(json.dumps(messages, ensure_ascii=False))
                yield "가장 유망한 건 한화생명 돌봄 쪽이에요."

            async def fake_tts(sentence, *a, **k):
                return b"\0" * 24000, 24000  # half a second each
            globals().update(transcribe=stt, llm_stream=llm, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            opening = "정훈님, 맡기신 경쟁사 조사 끝났어요. 한화생명 돌봄 서비스가 가장 가까워요. 다른 두 곳은 규모가 작아요. 더 궁금한 거 물어보세요."
            links.receive(full, {"type": "call", "call_id": "tk1", "opening": opening, "reason": "task",
                                 "origin_session": "azel:day", "detail": "목표: 경쟁사 조사\n결과: 한화생명 돌봄, 스타트업 A, 스타트업 B"})
            tc, hdr = TestClient(app, client=("100.64.0.8", 1)), {"Authorization": "Bearer app-tok"}
            speech = (np.ones(8000, np.int16) * 3000).tobytes() + np.zeros(12000, np.int16).tobytes()
            frames = []
            with tc.websocket_connect("/v1/call?call_id=tk1&session=tk", headers=hdr) as w:
                t0 = time.monotonic()
                time.sleep(0.3)
                w.send_bytes(speech)  # "여보세요" before / as the opening starts
                time.sleep(1.4)
                w.send_bytes(speech)  # and again at about 2 s, while it plays
                while True:
                    m = w.receive()
                    f = json.loads(m["text"]) if m.get("text") else {"type": "audio"}
                    frames.append(f)
                    if f["type"] == "reply" and f["text"].endswith("물어보세요."):
                        break
                time.sleep(1.05)
                w.send_text(json.dumps({"type": "text", "text": "그중에 제일 유망한 거 뭐야?"}))
                while True:
                    m = w.receive()
                    if not m.get("text"):
                        continue
                    if (f := json.loads(m["text"]))["type"] == "turn_done":
                        assert f["brain"] == "local", f  # the typed turn was Qwen's: turn_done says who answered
                        break
                    frames.append(f)
                w.send_text(json.dumps({"type": "hangup"}))
            opening_said = [f["text"] for f in frames if f["type"] == "reply" and f.get("route") == "opening"]
            others = [f for f in frames if f["type"] in ("transcript", "interrupt")]
            with tc.websocket_connect("/v1/call?call_id=missing&session=tk2", headers=hdr) as w:
                while True:
                    m = w.receive()
                    if m.get("text") and (f := json.loads(m["text"]))["type"] == "reply":
                        break
                fallback = f["text"]
                w.send_text(json.dumps({"type": "hangup"}))
            announced, results = [], []
            with tc.websocket_connect("/v1/call?session=live", headers=hdr) as w:
                while json.loads(w.receive_text()).get("value") != "listening":
                    pass
                ann = {"type": "announce", "id": "an1", "session": "azel:live", "opening": "맡기신 정리 끝났어요. 파일 세 개를 옮겼어요.",
                       "detail": "결과: 파일 세 개", "context": {"kind": "task", "label": "정리", "id": "abcabcabcabc"}}
                w.portal.call(links.receive, full, ann)  # on the server's loop, as the link would
                w.portal.call(links.receive, full, {"type": "announce", "id": "an2", "session": "azel:nobody", "opening": "x"})
                while True:
                    m = w.receive()
                    if not m.get("text"):
                        continue
                    f = json.loads(m["text"])
                    if f["type"] == "reply":
                        announced.append(f["text"])
                    if nomode(f) == {"type": "state", "value": "listening"} and announced:
                        break
                w.send_text(json.dumps({"type": "auto_approve", "value": True, "minutes": 30}))  # the app's toggle
                time.sleep(0.2)
                grant_req = [m for m in full.ws.sent if m.get("type") == "auto_grant"]
                w.portal.call(links.receive, full, {"type": "auto_state", "session": "azel:live", "value": True,
                                                    "expires_at": 123.0, "grant_id": "g1"})
                while True:
                    m = w.receive()
                    if m.get("text") and (f := json.loads(m["text"]))["type"] == "auto_approve":
                        auto_frame = f
                        break
                hung = None
                w.portal.call(links.receive, full, {"type": "end_call", "session": "azel:live", "reason": "사용자 요청"})
                while True:  # the assistant hangs up: a hangup frame, then the socket closes
                    m = w.receive()
                    if m.get("type") == "websocket.close":
                        break
                    if m.get("text") and (f := json.loads(m["text"]))["type"] == "hangup":
                        hung = f
            for _ in range(40):  # the server's own close: its cleanup (grant end) follows shortly
                if len([m for m in full.ws.sent if m.get("type") == "auto_grant"]) > len(grant_req):
                    break
                time.sleep(0.05)
            results = {m["id"]: m["ok"] for m in full.ws.sent if m.get("type") == "announce_result"}
            results.update({f"{m['id']}:spoken": m["ok"] for m in full.ws.sent if m.get("type") == "announced"})
            grant_end = [m for m in full.ws.sent if m.get("type") == "auto_grant"][len(grant_req):]
            links.remove(full)
            return opening_said, others, asked, fallback, announced, results, heard, grant_req, auto_frame, grant_end, hung
        def typed_check():
            """Typing during a call: a user turn without STT (no transcript echo, input "text" in the log), captions only
            with speak=false, rejected when too long or too fast, and typing over a reply interrupts it."""
            from fastapi.testclient import TestClient
            asked = []

            async def no_stt(audio):
                raise AssertionError("typed turns are never transcribed")

            async def fake_llm(messages, max_tokens=600):
                asked.append(messages[-1]["content"])
                yield {"안녕": "네, 안녕하세요.", "길게": "물은 백 도에서 끓어요. 얼음은 영 도에서 녹아요."}.get(
                    messages[-1]["content"], "조용히 답할게요.")

            async def fake_tts(sentence):
                return b"\0" * 96000, 24000  # two seconds: still playing when the next message arrives
            globals().update(transcribe=no_stt, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda f: 0.0))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            got = []

            def until(w, kind):
                while True:
                    m = w.receive()
                    got.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    if got[-1] != "audio" and got[-1].get("type") == kind:
                        return

            with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                    "/v1/call?client=iphone&session=typed", headers={"Authorization": "Bearer app-tok"}) as w:
                until(w, "hello")
                w.send_text(json.dumps({"type": "text", "text": "  안녕  "}))
                w.send_text(json.dumps({"type": "text", "text": "또 할게"}))  # within a second of the last one
                until(w, "turn_done")
                spoken = got[:]
                w.send_text(json.dumps({"type": "text", "text": "가" * 1001}))
                until(w, "text_rejected")
                rejected = [f for f in got if f != "audio" and f["type"] == "text_rejected"]
                time.sleep(1.05)
                got.clear()
                w.send_text(json.dumps({"type": "text", "text": "   "}))  # empty: ignored, not even rate-limited
                w.send_text(json.dumps({"type": "text", "text": "조용히", "speak": False}))
                until(w, "turn_done")
                quiet = got[:]
                time.sleep(1.05)
                got.clear()
                w.send_text(json.dumps({"type": "text", "text": "길게"}))
                until(w, "reply")
                time.sleep(1.05)
                w.send_text(json.dumps({"type": "text", "text": "안녕"}))  # over the playing reply
                until(w, "turn_done")
                over = got[:]
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.1)
            turns = [m for m in full.ws.sent if m.get("type") == "call_log" and m.get("event") == "turn"]
            links.remove(full)
            return spoken, rejected, quiet, over, turns, asked
        def briefing_check(situation):
            """/v1/call?intent=briefing against a fake full gateway; returns (reply texts, frame types, call logs)."""
            from fastapi.testclient import TestClient

            class BriefingGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": situation,
                                                "facts": ["승인 대기 1건"]})
                    elif m.get("type") == "ask":
                        for ev in [{"type": "activity", "phase": "tool_start", "label": "캘린더 확인 중",
                                    "tool": "mcp__claude_ai_Google_Calendar__list_events", "kind": "calendar"},
                                   {"type": "text", "delta": "오늘 브리핑이에요. 회의가 하나 있어요."}, {"type": "done"}]:
                            links.receive(self.lk, {"id": m["id"], **ev})

            async def qwen_briefing(messages, max_tokens=600):
                assert "토론지" in messages[1]["content"] and "승인 대기 1건" in messages[1]["content"]
                for d in ["오늘 브리핑이에요. ", "토론지가 오늘 밤 11시까지예요. ", "승인 대기가 한 건 있어요."]:
                    yield d

            async def fake_tts(sentence):
                return b"\0" * 480, 24000
            globals().update(llm_stream=qwen_briefing, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.0))
            gw_ws = BriefingGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            replies, types = [], []
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=b", headers={"Authorization": "Bearer app-tok"}) as w:
                while True:
                    m = w.receive()
                    if not m.get("text"):
                        continue
                    f = json.loads(m["text"])
                    types.append(f["type"])
                    if f["type"] == "reply":
                        replies.append(f["text"])
                    if nomode(f) == {"type": "state", "value": "listening"} and replies:
                        break
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.05)
            logs = [m for m in gw_ws.sent if m.get("type") == "call_log"]
            links.remove(gw_ws.lk)
            return replies, types, logs
        def barge_route_check():
            """Two-stage barge-in through /v1/call while a briefing plays: a cough pauses and resumes, '잠깐만'
            pauses and interrupts. Silero is stubbed to say 'speech' for everything (its worst case)."""
            from fastapi.testclient import TestClient

            class BriefGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": "메모", "facts": []})

            async def long_briefing(messages, max_tokens=600):
                for i in range(6):
                    yield f"브리핑 {i}번째 문장이에요. "

            async def long_tts(sentence):
                return b"\0" * 96000, 24000  # 2 s each: the reply keeps playing while the user talks

            async def stt(audio):  # continuous speech → words; a cough or burst → nothing
                return "잠깐만" if float(np.mean(np.abs(audio) > 0.1)) > 0.25 and len(audio) >= 16000 else ""
            globals().update(llm_stream=long_briefing, tts=long_tts, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.95))
            gw_ws = BriefGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            got = []

            def until(w, kind):
                while True:
                    m = w.receive()
                    if m.get("text"):
                        got.append(json.loads(m["text"])["type"])
                        if got[-1] == kind:
                            return

            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=bg", headers={"Authorization": "Bearer app-tok"}) as w:
                until(w, "reply")  # the briefing is playing
                w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.2, 8000, 9) + np.zeros(16000, np.int16).tobytes())
                until(w, "resume")  # cough: paused, then resumed
                assert "interrupt" not in got and got.count("pause") == 1, got
                t_speech = time.monotonic()
                w.send_bytes(tone(0.8, 9000, 10) + np.zeros(16000, np.int16).tobytes())
                until(w, "interrupt")  # a clear "잠깐만": paused, then interrupted
                commit_s = time.monotonic() - t_speech
                assert got[-2:] == ["pause", "interrupt"] or "pause" in got[got.index("resume") + 1:], got
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(gw_ws.lk)
            return commit_s
        def pause_edge_check(stt_fails):
            """A tap during a pause, or STT failing during a pause: the app is never left paused."""
            from fastapi.testclient import TestClient

            class BriefGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": "메모", "facts": []})

            async def long_briefing(messages, max_tokens=600):
                for i in range(6):
                    yield f"브리핑 {i}번째 문장이에요. "

            async def long_tts(sentence):
                return b"\0" * 96000, 24000

            async def stt(audio):
                if stt_fails:
                    raise httpx.ConnectError("stt down")
                return "음"
            globals().update(llm_stream=long_briefing, tts=long_tts, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.95))
            gw_ws = BriefGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            got = []

            def until(w, kind):
                while True:
                    m = w.receive()
                    if m.get("text"):
                        got.append(json.loads(m["text"])["type"])
                        if got[-1] == kind:
                            return
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=pe", headers={"Authorization": "Bearer app-tok"}) as w:
                until(w, "reply")
                if stt_fails:  # speech long enough for STT, which then fails
                    w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.8, 9000, 11) + np.zeros(16000, np.int16).tobytes())
                    until(w, "resume")
                else:  # speech starts (pause), then the user taps
                    w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.3, 9000, 12))
                    until(w, "pause")
                    w.send_text(json.dumps({"type": "interrupt"}))
                    until(w, "resume")
                    assert "interrupt" in got[got.index("pause"):], got
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(gw_ws.lk)
            return got
        got = pause_edge_check(stt_fails=False)
        assert got.index("pause") < got.index("interrupt") < len(got) - 1 - got[::-1].index("resume") + 1
        got = pause_edge_check(stt_fails=True)
        assert got[-1] == "resume" and "interrupt" not in got[got.index("pause"):], got
        commit_s = barge_route_check()
        assert commit_s < 3.0, commit_s  # speech 0.8 s + 0.5 s trailing silence + STT (stubbed here)
        globals().update(llm_stream=real_llm, tts=real[1])
        replies, types, logs = briefing_check("[4] 토론지 오늘 23시")  # fast path: local Qwen from the note
        assert replies == ["오늘 브리핑이에요.", "토론지가 오늘 밤 11시까지예요.", "승인 대기가 한 건 있어요."], replies
        assert FILLER not in replies and "activity" not in types
        starts = [m for m in logs if m["event"] == "start"]
        assert starts[0]["reason"] == "briefing" and starts[0]["direction"] == "outgoing"
        assert starts[-1]["opening"] == " ".join(replies) and starts[-1]["started_at"] == starts[0]["started_at"]
        replies, types, logs = briefing_check("")  # no note yet: filler, then Claude over the link
        assert replies == [FILLER, "오늘 브리핑이에요.", "회의가 하나 있어요."], replies
        assert types.count("activity") == 2  # routing, then the tool step
        assert [m for m in logs if m["event"] == "start"][-1]["opening"] == "오늘 브리핑이에요. 회의가 하나 있어요."
        replies, types, logs = briefing_check("")  # again within 10 min: the same text, no second Claude run
        assert replies == ["오늘 브리핑이에요.", "회의가 하나 있어요."] and "activity" not in types, replies
        last_claude_briefing[:] = [0.0, ""]
        globals()["llm_stream"] = real_llm
        def persona_check():
            """/v1/call?persona=luna: Qwen speaks as 루나 in her voice, history and logs are hers; a call placed as
            luna (no ?persona) is hers too, opening voice included."""
            from fastapi.testclient import TestClient
            voices, prompts = [], []

            async def voice_tts(sentence):
                voices.append((CALL_PERSONA.get() or {}).get("voice"))
                return b"\0" * 24000, 24000  # 0.5 s of audio

            async def hi_llm(messages, max_tokens=600):
                prompts.append(messages[0]["content"])
                yield "안녕하세요, 루나예요."

            async def stt(audio):
                return "안녕"
            globals().update(tts=voice_tts, llm_stream=hi_llm, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            links.receive(full, {"type": "settings", "value": {"personality_default": "기본 성격", "personas": [
                {"id": "azel", "name": "아젤", "voice": "sohee", "personality": "", "primary": True},
                {"id": "luna", "name": "루나", "voice": "vivian", "voice_style": "부드럽게", "personality": "느긋함"},
                {"id": "old", "name": "옛비서", "voice": "ryan", "deleted_at": 1.0}]}})
            tc = TestClient(app, client=("100.64.0.8", 1))
            speech = np.zeros(8000, np.int16).tobytes() + (np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()

            def one_turn(path):
                with tc.websocket_connect(path, headers={"Authorization": "Bearer app-tok"}) as w:
                    w.send_bytes(speech)
                    first_audio, states = None, []
                    while True:
                        m = w.receive()
                        if m.get("bytes") and first_audio is None:
                            first_audio = time.monotonic()
                        ev = json.loads(m["text"]) if m.get("text") else {}
                        if ev.get("type") == "state":
                            states.append(ev["value"])
                        if ev.get("type") in ("turn_done", "error"):
                            break
                    # the reply is 0.5 s of audio: still "speaking" until it has played, plus the tail
                    assert time.monotonic() - first_audio >= 0.5 + PLAY_TAIL_S - 0.05 and states[-1] == "speaking", states
                    assert ev["type"] == "turn_done" and ev["audio_ends_in_ms"] > 300, ev
                    assert nomode(json.loads(w.receive_text())) == {"type": "state", "value": "listening"}
                    w.send_text(json.dumps({"type": "hangup"}))
                time.sleep(0.05)
            one_turn("/v1/call?session=ps&persona=luna")
            assert "'루나'" in prompts[-1] and "성격과 말투: 느긋함." in prompts[-1] and set(voices) == {"vivian"}, (prompts, voices)
            assert "luna:ps" in history and "azel:ps" not in history
            start = [m for m in full.ws.sent if m.get("type") == "call_log" and m["event"] == "start"][-1]
            assert start["persona_id"] == "luna"
            voices.clear()
            one_turn("/v1/call?session=ps2&persona=old")  # deleted persona: the primary
            assert "'아젤'" in prompts[-1] and "성격과 말투: 기본 성격." in prompts[-1] and set(voices) == {"sohee"}
            voices.clear()

            async def placed():  # the gateway rings as luna: the opening is synthesized in her voice up front
                links.receive(full, {"type": "call", "call_id": "cl", "opening": "루나예요. 일어날 시간이에요.",
                                     "persona": "luna"})
                await links.openings["cl"]["audio"]
            asyncio.run(placed())
            assert set(voices) == {"vivian"}
            with tc.websocket_connect("/v1/call?session=pm&mode=local", headers={"Authorization": "Bearer app-tok"}) as w:
                assert json.loads(w.receive_text()) == {"type": "hello", "caps": ["text", "played", "mode", "secret", "auto_approve"]}  # first
                assert json.loads(w.receive_text()) == {"type": "mode", "value": "local"}  # on connect
                assert json.loads(w.receive_text()) == {"type": "state", "value": "listening", "mode": "local", "secret": False, "auto": False}
                w.send_text(json.dumps({"type": "mode", "value": "bogus"}))  # ignored
                w.send_text(json.dumps({"type": "mode", "value": "claude_api"}))
                assert json.loads(w.receive_text()) == {"type": "mode", "value": "claude_api"}  # on each change
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(full)
            links.settings = {}
            globals().update(tts=real[1], llm_stream=real_llm)
        def play_check():
            """The referee with a scripted Qwen: moves are checked here, never taken on Qwen's word."""
            script = iter(["사과", "바나나", "일기", "역도", "고양이",
                           '{"q": "한국의 수도는?", "a": "서울"}', '{"q": "물의 화학식은?", "a": "H2O"}'])

            async def fake_line(system, max_tokens=80):
                return next(script)
            globals()["qwen_line"] = fake_line
            games.clear()
            assert not asyncio.run(play_note("g:1", "끝말 입기 하자"))  # no game yet: starting one is Qwen's tool call
            n = [asyncio.run(run_tool(TOOL_TAG + ' {"name": "game", "args": {"action": "start", "kind": "chain"}}', "g:1"))]
            assert games["g:1"]["last"] == "사과" and "'사과'" in n[0]
            n.append(asyncio.run(play_note("g:1", "과일이요")))  # Qwen's "바나나" is refused, "일기" taken
            assert "'일기'" in n[1] and "바나나" not in n[1] and games["g:1"]["last"] == "일기"
            n.append(asyncio.run(play_note("g:1", "바나나")))  # the user's wrong start: refused, nothing changes
            assert "'기'(으)로 시작해야" in n[2] and games["g:1"]["last"] == "일기"
            games["g:1"].update(last="역사", used=["역사"])
            n.append(asyncio.run(play_note("g:1", "사력")))  # 력 -> 역 (두음법칙): Qwen's "역도" is accepted
            assert "'역도'" in n[3] and games["g:1"]["last"] == "역도"
            assert "마무리" in asyncio.run(play_note("g:1", "아 졌어 그만")) and "g:1" not in games
            assert asyncio.run(game_tool("g:2", {"action": "start", "kind": "twenty"})) and games["g:2"]["secret"] == "고양이"
            assert "남은 질문은 19개" in asyncio.run(play_note("g:2", "동물이야?"))
            assert "맞혔어" in asyncio.run(play_note("g:2", "혹시 고양이?")) and "g:2" not in games
            q = asyncio.run(game_tool("g:3", {"action": "start", "kind": "quiz"}))
            assert "한국의 수도는?" in q and "서울" in q and "심심" in q
            a2 = asyncio.run(play_note("g:3", "서울"))
            assert "'서울'" in a2 and "물의 화학식은?" in a2 and games["g:3"]["a"] == "H2O"
            assert asyncio.run(game_tool("g:3", {"action": "stop"})) == GAME_STOP_NOTE and "g:3" not in games
            assert "없어요" in asyncio.run(game_tool("g:3", {"action": "stop"}))
            assert "kind는" in asyncio.run(game_tool("g:3", {"action": "start", "kind": "chess"})) and "g:3" not in games
            globals()["qwen_line"] = real_qwen_line
        real_qwen_line = qwen_line
        play_check()
        games.clear()
        assert [dueum(x) for x in "력리뉴락라름나사"] == ["역", "이", "유", "낙", "나", "늠", None, None]
        assert not asyncio.run(play_note("g:9", "끝까지 말해 봐"))
        assert "따라 하거나" in system_prompt("아젤") and "game 도구" in system_prompt("아젤") and "브레인스토밍" in system_prompt("아젤")
        assert "- game(action, kind):" in gateway_tools_text([{"name": "web_search", "read_only": True, "description": "검색"}, GAME_TOOL], "심심해")
        def local_mode_check():
            """Local mode: Qwen has no hand-over tag, and anything that would go to Claude gets the can't-do line."""
            prompts = []

            async def qwen(messages, max_tokens=600):
                prompts.append(messages[0]["content"])
                yield ROUTE_TAG  # as if Qwen still tried to hand over

            async def claude(*a, **k):
                raise AssertionError("Claude in local mode")
                yield ""

            async def fake_tts(sentence, *a, **k):
                return b"", 24000
            globals().update(llm_stream=qwen, claude_stream=claude, tts=fake_tts, FILLER_AFTER_S=5)
            ctx = contextvars.copy_context()
            ctx.run(CALL_MODE.set, "local")
            async def said_by(gen):
                return [x async for x, *_ in gen]
            out = ctx.run(asyncio.run, said_by(respond("오늘 코스피 어때?", "loc", time.perf_counter(), {})))
            globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
            return out, prompts
        said, prompts = local_mode_check()
        def switched_check():
            """The turn started in auto; the app switched the call to local before the hand-off: never Claude."""
            live = {"mode": "auto"}

            async def qwen(messages, max_tokens=600):
                live["mode"] = "local"  # the mode frame arrives while Qwen is answering
                yield ROUTE_TAG

            async def claude(*a, **k):
                raise AssertionError("Claude after a switch to local")
                yield ""
            globals().update(llm_stream=qwen, claude_stream=claude, tts=lambda s, *a, **k: asyncio.sleep(0, (b"", 24000)))
            ctx = contextvars.copy_context()
            ctx.run(CALL_MODE.set, "auto")
            ctx.run(CALL_LIVE.set, live)

            async def said_by():
                return [x async for x, *_ in respond("양자역학 설명해 줘", "sw", time.perf_counter(), {})]
            try:
                return ctx.run(asyncio.run, said_by())
            finally:
                globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1])
        assert switched_check() == [LOCAL_LINE]

        async def local_briefing():  # no situation note in local mode: a Korean line, never Claude
            async def claude(*a, **k):
                raise AssertionError("Claude briefing in local mode")
                yield ""
            globals()["claude_stream"] = claude
            CALL_MODE.set("local")
            try:
                return [x async for x in briefing_sentences("azel:b", "아젤", lambda ev: asyncio.sleep(0))]
            finally:
                globals()["claude_stream"] = real[0]
        assert asyncio.run(local_briefing()) == [LOCAL_BRIEFING]
        CALL_MODE.set("local")
        assert asyncio.run(task_reply("맥북 다운로드 폴더 정리해 줘", "call")) == LOCAL_LINE  # never sent to the gateway
        CALL_MODE.set("")
        assert said == [LOCAL_LINE] and prompts and all(ROUTE_TAG not in p for p in prompts), (said, prompts)
        assert ROUTE_TAG not in local_prompt(NUDGE) and ROUTE_TAG in NUDGE

        class Rec(FakeWS):
            pass
        rec = Link(Rec())
        ctx = contextvars.copy_context()
        ctx.run(CALL_MODE.set, "claude_code")
        ctx.run(asyncio.run, rec.send({"type": "ask", "id": "x"}))
        ctx.run(asyncio.run, rec.send({"type": "call_log", "event": "end"}))
        assert rec.ws.sent[0]["mode"] == "claude_code" and "mode" not in rec.ws.sent[1]  # requests only
        persona_check()
        games.update({"azel:h": {"kind": "chain", "last": "사과", "used": ["사과"]},
                      "azel:other": {"kind": "chain", "last": "기차", "used": ["기차"]}})
        assert wake_guard_check("wake") == [] and len(wake_guard_check("chat")) == 1  # wake: louder, longer speech only
        logs = history_check()
        assert "azel:h" not in games and "azel:other" in games  # hangup ends this session's game, no other
        games.clear()
        (k1, early), (k2, held) = played_check()
        assert k1 == k2 == "turn_done" and early < 0.8 and held >= 1.0 + PLAYED_WAIT_S - 0.2, (early, held)
        def failed_turn_check():
            """The error frame of a turn whose reply raised: what the app shows the user."""
            import threading
            from fastapi.testclient import TestClient
            got = {}

            class LoudVad:
                def __call__(self, frame):
                    return float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)

            async def stt(audio):
                return "그거 지워"

            async def broken_llm(messages, max_tokens=600):
                raise AttributeError("'NoneType' object has no attribute 'find'")
                yield ""
            saved = {k: globals()[k] for k in ("SileroVoiceActivityDetector", "transcribe", "llm_stream", "check_token")}
            globals().update(SileroVoiceActivityDetector=lambda: LoudVad(), transcribe=stt, llm_stream=broken_llm,
                             check_token=lambda got, labels=("client",): "client")

            def talk():
                with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                        "/v1/call?client=mac&session=failed", headers={"Authorization": "Bearer t"}) as w:
                    w.receive_text()
                    w.send_bytes(np.zeros(4800, np.int16).tobytes())
                    for chunk in ((np.ones(16000, np.int16) * 3000).tobytes(), np.zeros(16000, np.int16).tobytes()):
                        for i in range(0, len(chunk), 3200):  # live pace
                            w.send_bytes(chunk[i:i + 3200])
                            time.sleep(0.1)
                    t = time.monotonic()
                    while time.monotonic() - t < 5:
                        f = json.loads(w.receive_text())
                        if f.get("type") == "error":
                            got["message"] = f.get("message")
                            break
                    w.send_text(json.dumps({"type": "hangup"}))
            th = threading.Thread(target=talk, daemon=True)
            th.start()
            th.join(10)
            globals().update(saved)
            return got.get("message")

        def stall_check(held=False):
            """Live 2026-11-23 (Mac): a one-breath command sent as one block, then no live frames: the turn waited ~17 s
            for the quiet that ends it. Now no audio for END_STALL_S ends it. held: the block is 100 ms chunks back to
            back with 0.8 s pauses in it ("오늘 아침 … 삭제해" was cut at the first pause): still one turn."""
            import threading
            from fastapi.testclient import TestClient
            heard, seen = [], {}

            class LoudVad:
                def __call__(self, frame):
                    return float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)

            async def stt(audio):
                heard.append(len(audio) / 16000)
                return "내일 날씨 알려줘"

            async def llm(messages, max_tokens=600):
                yield "내일은 맑아요."

            async def quiet_tts(sentence, *a, **k):
                return b"\0" * 480, 24000
            saved = {k: globals()[k] for k in ("SileroVoiceActivityDetector", "transcribe", "llm_stream", "tts", "check_token")}
            globals().update(SileroVoiceActivityDetector=lambda: LoudVad(), transcribe=stt, llm_stream=llm, tts=quiet_tts,
                             check_token=lambda got, labels=("client",): "client")

            def talk():
                with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                        "/v1/call?client=mac&session=stall", headers={"Authorization": "Bearer t"}) as w:
                    w.receive_text()
                    w.send_bytes(np.zeros(4800, np.int16).tobytes())  # room tone
                    t = time.monotonic()
                    if held:
                        rec = np.concatenate([np.ones(16000, np.int16) * 3000, np.zeros(12800, np.int16)] * 2 +
                                             [np.ones(16000, np.int16) * 3000]).tobytes()
                        for i in range(0, len(rec), 3200):
                            w.send_bytes(rec[i:i + 3200])
                    else:
                        w.send_bytes((np.ones(32000, np.int16) * 3000).tobytes())  # 2 s of speech, nothing after it
                    while time.monotonic() - t < 5:
                        f = json.loads(w.receive_text())
                        if f.get("type") == "state" and f.get("value") == "thinking":  # the turn closed: STT starts
                            seen["after"] = time.monotonic() - t
                            break
                    w.send_text(json.dumps({"type": "hangup"}))
            th = threading.Thread(target=talk, daemon=True)
            th.start()
            th.join(8)
            globals().update(saved)
            return seen.get("after"), heard
        after, heard = stall_check()
        assert after is not None and after < END_STALL_S + 0.6 and heard and heard[0] > 1.9, (after, heard)
        # live 2026-11-23: the router answered with tool-call markup native_call could not read (None), and None.find
        # reached the app as "AttributeError: 'NoneType' object has no attribute 'find'"
        assert native_call(NATIVE_TAG + "calendar_write 지워</tool_call>") is None

        class RouterSays:
            def __init__(self, content):
                self.content = content

            async def post(self, url, json=None):
                content = self.content

                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {"choices": [{"message": {"content": content}}]}
                return R()
        saved_client = client
        try:
            for out in (NATIVE_TAG + "calendar_write 지워</tool_call>", NATIVE_TAG + '["agenda"]</tool_call>'):
                globals()["client"] = RouterSays(out)
                assert asyncio.run(route_lookup("어떤 컴파일러 프로젝트 일정이 미리 알림 캘린더에 있는데 그거 지워", [],
                                                [{"name": "agenda", "read_only": True, "description": "일정"}])) is None, out
        finally:
            globals()["client"] = saved_client
        assert failed_turn_check() == TURN_FAILED  # any other error: a short Korean line, never the exception
        # calendar_write changes things: never read-only, so never offered to the tool-first router (live 2026-11-23)
        saved_defs = list(gw_tools["defs"])
        gw_tools["defs"] = [{"name": "calendar_write", "read_only": False}, {"name": "calendar_events", "read_only": True},
                            {"name": "macbook__run_command"}]
        assert not read_only("calendar_write") and read_only("calendar_events") and action_tool("calendar_write")
        assert not read_only("macbook__run_command") and not read_only("nope")  # no flag, or unknown: an action
        gw_tools["defs"] = saved_defs

        async def confirm_check(answer):
            """Qwen calls calendar_write, the gateway reads back and waits; the next turn answers. Gateway frames out."""
            frames, rounds = [], []

            async def fake_msg(name, args, on_say=None, on_output=None):
                frames.append((name, dict(args), CALL_TURN.get()))
                if args.get("confirm") == "ab12cd34":
                    return {"type": "tool_result", "result": "일정을 지웠어요.", "spoken": "일정을 지웠어요."}
                q = "오늘 오전 9시 '컴파일러 프로젝트' 일정을 '미리 알림' 캘린더에서 지울까요?"
                return {"type": "tool_result", "is_error": True, "spoken": q,
                        "result": f"아직 아무것도 바꾸지 않았어요(pending_id=ab12cd34). 사용자에게 \"{q}\"라고 묻고 이번 답은 끝내.",
                        "needs": {"kind": "confirm", "question": q, "args": {"confirm": "ab12cd34"}}}

            async def qwen(messages, max_tokens=600):
                rounds.append(messages)
                if "[도구 결과]" in messages[-1]["content"]:
                    yield "오늘 오전 9시 '컴파일러 프로젝트' 일정을 '미리 알림' 캘린더에서 지울까요?"
                else:
                    yield TOOL_TAG + ' {"name": "calendar_write", "args": {"op": "delete", "match": "컴파일러 프로젝트"}}'
            saved = gateway_tool_msg, llm_stream, tts, route_lookup, dict(gw_tools), links.settings
            globals().update(gateway_tool_msg=fake_msg, llm_stream=qwen, tts=lambda s: asyncio.sleep(0, (b"", 24000)),
                             route_lookup=lambda *a, **k: asyncio.sleep(0, []))
            gw_tools.update(at=time.time(), defs=[{"name": "calendar_write", "description": "캘린더 바꾸기"}])
            links.settings = {"brain_mode": "local"}
            history.pop("azel:cal", None)
            pending_confirm.pop("cal", None)
            try:
                first = [s async for s, *_ in respond("컴파일러 프로젝트 일정 미리 알림 캘린더에 있는데 그거 지워", "cal",
                                                      time.perf_counter(), {})]
                second = [s async for s, *_ in respond(answer, "cal", time.perf_counter(), {})]
            finally:
                globals().update(gateway_tool_msg=saved[0], llm_stream=saved[1], tts=saved[2], route_lookup=saved[3])
                gw_tools.clear()
                gw_tools.update(saved[4])
                links.settings = saved[5]
            return first, second, frames
        first, second, frames = asyncio.run(confirm_check("응"))
        assert any("지울까요" in x for x in first) and not any("지웠어요" in x for x in first), first  # asked, not done
        assert [f[1] for f in frames] == [{"op": "delete", "match": "컴파일러 프로젝트"}, {"confirm": "ab12cd34"}], frames
        assert frames[0][2] and frames[1][2] and frames[0][2] != frames[1][2]  # each turn its own id for the gateway
        assert second == ["일정을 지웠어요."], second
        first, second, frames = asyncio.run(confirm_check("아니 잠깐만"))
        assert not any("confirm" in f[1] for f in frames), frames  # anything but yes: the confirm id is never sent
        CARDS = {  # one fake gateway card per kind (cards-contract.md), keyed by the tool that returns it
            "agenda": {"kind": "agenda", "title": "내일 일정", "range_label": "내일", "items": [
                {"start": "2026-11-24T10:00:00+09:00", "end": "2026-11-24T11:15:00+09:00", "all_day": False,
                 "title": "컴파일러", "source": "gistra", "location": "EECS B201"}]},
            "stock_quote": {"kind": "stock", "title": "엔비디아", "symbol": "NVDA", "price": 181.2, "change": 2.3,
                            "change_pct": 1.29, "currency": "USD", "points": [[1763946000, 178.9], [1763946300, 181.2]],
                            "range": "1d"},
            "portfolio": {"kind": "portfolio", "title": "보유 종목", "items": [
                {"symbol": "005930.KS", "name": "삼성전자", "price": 98000, "change_pct": -0.4, "spark": [98400, 98000]}]},
            "gistra_assignments": {"kind": "todo", "title": "남은 과제", "items": [
                {"title": "HW3", "due": "2026-11-25", "course": "컴파일러", "source": "gistra", "done": False}]},
            "weather": {"kind": "weather", "title": "광주 날씨", "place": "광주", "now": {"temp": 4, "icon": "cloudy", "pop": 20},
                        "hours": [{"t": "2026-11-23T15:00:00+09:00", "temp": 5, "icon": "cloudy", "pop": 20}],
                        "days": [{"d": "2026-11-24", "min": -1, "max": 8, "icon": "sunny", "pop": 0}]},
            "infra_health": {"kind": "infra", "title": "서버 상태", "hosts": [{"name": "vd-spark", "up": True, "cpu_pct": 12.5}],
                             "down": ["nemoclaw"]},
            "dup_agenda": {"kind": "agenda", "title": "또 일정", "items": []},  # the turn already has an agenda card
            "big": {"kind": "big", "title": "x", "blob": "가" * CARD_MAX},  # over the size cap
            "untitled": {"kind": "untitled"},  # no title
        }

        class CardLink:
            async def send(self, obj):
                links.queues[obj["id"]][0].put_nowait({"type": "tool_result", "result": "ok", "card": CARDS.get(obj["name"])})

        async def cards_check():
            """Each kind from a fake gateway tool_result reaches the call once, with this turn's id; bad cards never."""
            got, real_pick = [], links.pick
            async def sink(ev):
                got.append(ev)
            links.pick = lambda **k: CardLink()
            try:
                CALL_TURN.set("t1"), CALL_CARDS.set((sink, set()))
                for name in [*CARDS, "no_card"]:
                    assert (await gateway_tool_msg(name, {}))["result"] == "ok"
                CALL_CARDS.set(None)  # no call (the /v1/chat path without on_event): nothing is sent, nothing fails
                await gateway_tool_msg("weather", {})
            finally:
                links.pick = real_pick
            return got
        got = asyncio.run(cards_check())
        class CmdLink:
            async def send(self, obj):
                q = links.queues[obj["id"]][0]
                q.put_nowait({"type": "tool_progress", "output": "building... token=abc123secret\n"})
                q.put_nowait({"type": "tool_progress", "output": "step 2\n"})  # within 0.5 s: held, not sent alone
                q.put_nowait({"type": "tool_result", "result": "빌드 실패", "is_error": True, "exit_code": 2,
                              "task_id": "t-9", "output": "가" * 4000 + "\nAuthorization: Bearer abcdefghijklmnop\nfail"})

        async def tool_detail_check():
            """A device command, as the app's tool detail sees it: input, a streamed chunk, then the end."""
            got, real_pick, real_defs = [], links.pick, dict(gw_tools)
            async def sink(ev):
                got.append(ev)
            links.pick = lambda **k: CmdLink()
            gw_tools.update(defs=[{"name": "macbook__run_command"}])
            try:
                CALL_EVENTS.set(sink)
                text = await run_tool(TOOL_TAG + ' {"name": "macbook__run_command", "args": {"command": '
                                      '"curl -H \'Authorization: Bearer sk-abcdefghijklmnopqrstu\' https://x.io/a?key=1 && make"}}', "s")
                CALL_EVENTS.set(None)
                plain = await run_tool(TOOL_TAG + ' {"name": "macbook__run_command", "args": {"command": "ls"}}', "s")
            finally:
                links.pick = real_pick
                gw_tools.clear()
                gw_tools.update(real_defs)
            return text, plain, got
        text, plain, tgot = asyncio.run(tool_detail_check())
        assert text == plain == "빌드 실패" and [e["phase"] for e in tgot] == \
            ["tool_start", "tool_output", "tool_output", "tool_end"], tgot
        start, chunk, rest, end = tgot
        assert rest["chunk"] == "step 2\n"  # held by the throttle, flushed before the end: chunks are append-only
        assert start["host"] == "macbook" and start["input"].startswith("curl -H 'Authorization: «redacted»") and \
            "sk-" not in start["input"] and "key=1" not in start["input"] and start["input"].endswith("&& make"), start
        assert chunk["chunk"] == "building... token=«redacted»\n" and chunk["call_id"] == start["call_id"] == end["call_id"]
        assert end["ok"] is False and end["exit_code"] == 2 and end["task_id"] == "t-9" and end["truncated"] is True
        assert end["output"].endswith("Authorization: «redacted»\nfail") and len(end["output"].encode()) <= 8192
        assert isinstance(end["duration_ms"], int) and end["route"] == "qwen"
        ca = client_activity({"phase": "tool_end", "tool": "Bash", "call_id": "toolu_01", "input": "echo $PASSWORD=hunter22",
                              "output": "x" * 9000, "ok": True, "exit_code": 0, "duration_ms": 12, "task_id": "t1",
                              "args": {"secret": "no"}})
        assert ca["call_id"] == "toolu_01" and ca["truncated"] is True and len(ca["output"]) == 8192 and ca["exit_code"] == 0
        assert "hunter22" not in ca["input"] and "args" not in ca and ca["ok"] is True and ca["route"] == "claude"
        assert redact("뭐야? 오늘 주가는?") == "뭐야? 오늘 주가는?"  # a question mark in text is not a URL query
        assert redact("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----") == "«redacted»"
        assert clip_head("가" * 1000) .endswith("…") and len(clip_head("가" * 1000).encode()) <= 2048
        INFRA_CARDS = {"infra_health": {"kind": "infra", "title": "서버 상태", "hosts": [{"name": "vd-spark", "up": True, "cpu_pct": 12}]},
                       "infra_status": {"kind": "infra", "title": "서비스 상태", "down": ["nemoclaw"]},
                       "infra_alerts": {"kind": "infra", "title": "경보", "hosts": []}}

        class InfraLink:
            async def send(self, obj):
                await asyncio.sleep({"infra_alerts": 0, "infra_status": 0.01, "infra_health": 0.02}[obj["name"]])
                links.queues[obj["id"]][0].put_nowait({"type": "tool_result", "result": "ok", "card": INFRA_CARDS[obj["name"]]})

        async def infra_card_check():
            """The three infra reads of one server question: one card with the hosts and the down services."""
            got, real_pick, real_defs = [], links.pick, dict(gw_tools)
            async def sink(ev):
                got.append(ev)
            links.pick = lambda **k: InfraLink()
            gw_tools.update(defs=[{"name": n, "read_only": True} for n in INFRA_CARDS])
            try:
                CALL_TURN.set("t2"), CALL_CARDS.set((sink, set()))
                await infra_read(list(INFRA_CARDS), "s")
            finally:
                links.pick = real_pick
                gw_tools.clear()
                gw_tools.update(real_defs)
            return got
        infra_got = asyncio.run(infra_card_check())
        assert [e["card"] for e in infra_got] == [{"kind": "infra", "title": "서버 상태", "down": ["nemoclaw"],
                                                   "hosts": [{"name": "vd-spark", "up": True, "cpu_pct": 12}]}], infra_got
        assert [e["card"]["kind"] for e in got] == ["agenda", "stock", "portfolio", "todo", "weather", "infra"], got
        assert all(e["type"] == "card" and e["turn"] == "t1" for e in got) and got[0]["card"] == CARDS["agenda"]

        def schedule_local_check(text):
            """brain_mode auto: a schedule turn stays on Qwen (no hand-over tag in its prompt, never Claude, the
            prefetched agenda's card sent to the call); any other turn may still hand over."""
            prompts, events, claude_calls = [], [], []

            async def qwen(messages, max_tokens=600):
                prompts.append(messages[0]["content"])
                yield ROUTE_TAG  # as if Qwen tried to hand over anyway

            async def claude(*a, **k):
                claude_calls.append(a[0])
                yield "코스피는 오늘 올랐어요."

            async def on_event(ev):
                events.append(ev)
            saved = llm_stream, claude_stream, tts, route_lookup, dict(gw_tools), links.settings, links.pick
            globals().update(llm_stream=qwen, claude_stream=claude, tts=lambda s, *a, **k: asyncio.sleep(0, (b"", 24000)),
                             route_lookup=lambda *a, **k: asyncio.sleep(0, []))
            gw_tools.update(at=time.time(), defs=[{"name": "agenda", "read_only": True, "description": "일정"}])
            links.settings, links.pick = {"brain_mode": "auto"}, lambda **k: CardLink()
            ctx = contextvars.copy_context()
            ctx.run(CALL_MODE.set, "auto")

            async def said_by():
                return [x async for x, *_ in respond(text, "sched", time.perf_counter(), {}, on_event)]
            try:
                out = ctx.run(asyncio.run, said_by())
            finally:
                globals().update(llm_stream=saved[0], claude_stream=saved[1], tts=saved[2], route_lookup=saved[3])
                gw_tools.clear()
                gw_tools.update(saved[4])
                links.settings, links.pick = saved[5], saved[6]
            return out, prompts, events, claude_calls
        said, prompts, events, claude_calls = schedule_local_check("내일 일정 뭐야?")
        assert said == [SCHEDULE_FAILED] and not claude_calls and prompts, (said, claude_calls)
        assert all(ROUTE_TAG not in p for p in prompts), prompts
        assert "[일정, 방금 확인" in prompts[0]  # the prefetched agenda is in Qwen's prompt
        assert [e["card"]["kind"] for e in events if e.get("type") == "card"] == ["agenda"], events
        said, prompts, events, claude_calls = schedule_local_check("오늘 코스피 어때?")
        assert claude_calls and said == ["코스피는 오늘 올랐어요."], (said, claude_calls)  # not a schedule: may hand over
        assert SCHEDULE_TURN.search("오늘 할 일 뭐 있어?") and SCHEDULE_TURN.search("내일 회의 지워 줘")

        def stock_refusal_check(refusal="오빠, 증권사 앱에서 바로 확인하는 게 제일 빠를 거야. 혹시 오늘 하루는 어땠어?"):
            """Live 2026-11-23 (Mac, typed, 서아, local): "오늘 애플 주식 차트 보…" got "증권사 앱에서 확인하는 게 빠를 거야"
            with no lookup. A live-data question always looks it up, the refusal is never said, the stock card is sent."""
            calls, events, prompts = [], [], []

            class StockLink:
                async def send(self, obj):
                    calls.append((obj["name"], obj["args"]))
                    links.queues[obj["id"]][0].put_nowait({"type": "tool_result", "result": "애플 271.5달러, 0.8% 상승",
                                                           "card": CARDS["stock_quote"]})

            async def qwen(messages, max_tokens=600):
                prompts.append(messages[0]["content"])
                if "[도구 결과]" in messages[-1]["content"]:
                    yield "오빠, 애플은 지금 271.5달러야. 오늘 0.8% 올랐어."
                else:  # refuses every time, even after the tool nudge: the server looks it up itself
                    yield refusal

            async def on_event(ev):
                events.append(ev)
            saved = llm_stream, tts, route_lookup, dict(gw_tools), links.settings, links.pick
            globals().update(llm_stream=qwen, tts=lambda s, *a, **k: asyncio.sleep(0, (b"", 24000)),
                             route_lookup=lambda *a, **k: asyncio.sleep(0, []))  # the router undecided, as it can be
            gw_tools.update(at=time.time(), defs=[{"name": "stock_quote", "read_only": True, "description": "주가",
                                                   "input_schema": {"type": "object", "properties": {"query": {}}}},
                                                  {"name": "web_search", "read_only": True, "description": "검색"}])
            links.settings, links.pick = {"brain_mode": "local", "user_title": "오빠"}, lambda **k: StockLink()
            history.pop("azel:stock", None)
            ctx = contextvars.copy_context()
            ctx.run(CALL_MODE.set, "local")

            async def said_by():
                return [x async for x, *_ in respond("오늘 애플 주식 차트 보여줘", "stock", time.perf_counter(), {}, on_event)]
            try:
                out = ctx.run(asyncio.run, said_by())
            finally:
                globals().update(llm_stream=saved[0], tts=saved[1], route_lookup=saved[2])
                gw_tools.clear()
                gw_tools.update(saved[3])
                links.settings, links.pick = saved[4], saved[5]
            return out, calls, events, prompts
        said, calls, events, prompts = stock_refusal_check()
        assert calls and calls[0][0] == "stock_quote", calls
        assert not any("증권사" in x for x in said) and any("271.5" in x for x in said), said
        assert [e["card"]["kind"] for e in events if e.get("type") == "card"] == ["stock"], events
        assert "주가, 일정처럼 도구가 있는 건 꼭 도구로" in prompts[0] and "최신 정보처럼" not in prompts[0]
        said, calls, events, prompts = stock_refusal_check(
            "네, 정훈님. 애플 주식 차트는 음성으로 보여드릴 수 없어요. 대신 현재 애플 주가와 등락률은 바로 알려드릴 수 있어요.")
        assert calls and calls[0][0] == "stock_quote" and not any("보여드릴 수 없" in x for x in said), (calls, said)
        assert [e["card"]["kind"] for e in events if e.get("type") == "card"] == ["stock"], events
        assert REFUSED_LOOKUP.search("시세 정보를 드리기 어렵습니다.") and not REFUSED_LOOKUP.search("오늘 0.8% 올랐어.")
        for refusal in ("애플 주식 차트는 음성으로 보여드릴 수 없어요.", "차트 이미지나 그래프가 필요하시다면, 폰이나 PC에서 직접 "
                        "확인하시는 게 가장 정확하답니다."):  # live 2026-11-23 (아젤, local, typed "오늘 애플 주식 차트 보여줘")
            assert REFUSED_LOOKUP.search(refusal), refusal
        assert "카드로 떠" in system_prompt("아젤") and "보여 줄 수 없다" in local_prompt(system_prompt("아젤"))
        assert "차트 보여줘" in ROUTE_SYSTEM
        assert not SCHEDULE_TURN.search("오늘 날씨 어때?") and ROUTE_TAG not in no_handover(NUDGE)
        after, heard = stall_check(held=True)
        assert after is not None and after < END_STALL_S + 0.6 and len(heard) == 1 and heard[0] > 3.4, (after, heard)
        t = Turn(lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000))
        speech, pause = (np.ones(16000, np.int16) * 3000).tobytes(), np.zeros(12800, np.int16).tobytes()
        t.hold = True
        assert [k for k, _ in t.feed(speech + pause + speech + pause)] == ["start"]  # the recording's pauses: no end
        t.hold = False
        assert [k for k, _ in t.feed(np.zeros(1600, np.int16).tobytes())] == ["end"]  # live quiet after it ends it
        states, stays, midway, leftover, slogs, brain_evs, prompts = secret_check()
        assert stays is True and midway is True and leftover == [], leftover
        assert all(f["secret"] is True for f in states[:3]) and states[3]["secret"] is False and states[-1]["secret"] is True
        assert "고양이" not in prompts[1]  # the next call on the same session never sees the secret turns
        assert [m["event"] for m in slogs] == ["start", "turn", "end"], slogs  # only the normal call's part
        assert "비밀" not in json.dumps(slogs, ensure_ascii=False) and "고양이" not in json.dumps(slogs, ensure_ascii=False)
        assert [e["event"] for e in brain_evs] == ["start", "end"] and all("secret-" not in json.dumps(e) for e in brain_evs)
        audio_rate[0], synth_rate[0] = 0.24, 0.15
        assert not gap_free(0.0, 11 * 0.24, [26]) and gap_free(0.0, 37 * 0.24, [22, 24])  # a lone short sentence can't cover
        assert not gap_free(0.0, 5.0, [], inflight=(-0.5, 40)) and gap_free(0.0, 5.0, [], inflight=(-4.0, 30))
        sizes = [syllables(c) for c in opening_chunks(split_ready("좋은 아침이에요. " + "오늘은 수업이 하나 있어요. " * 12, final=True)[0])]
        assert sizes[0] < sizes[1] <= sizes[2] and max(sizes) <= OPENING_CHUNK_SYL, sizes  # grows, then caps
        assert all(b <= max(FIRST_MIN_SYL, a * CHUNK_GROWTH) + 13 for a, b in zip(sizes, sizes[1:])), sizes
        gaps, said, ogaps, oev, osaid, op, calls = chunking_check()
        assert said[0] == "물은 백 도에서 끓어요." and said[-1].endswith("더 뜨겁게 끓여요.") and len(said) >= 3, said  # in order
        assert max(gaps, default=0) <= 50 and max(ogaps, default=0) <= 50, (gaps, ogaps)  # no audible gap
        assert osaid == op["parts"] and len(osaid) > 3  # the opening, in order
        assert oev[0][0] < calls[-1][0], "the opening started playing before its last chunk was synthesized"
        opening_said, others, asked, fallback, announced, results, heard, grant_req, auto_frame, grant_end, hung = task_call_check()
        assert hung == {"type": "hangup", "reason": "사용자 요청"}  # end_call: the app is told, then the socket closes
        assert grant_req == [{"type": "auto_grant", "session": "azel:live", "value": True, "minutes": 30}], grant_req
        assert auto_frame == {"type": "auto_approve", "value": True, "expires_at": 123.0}
        assert grant_end == [{"type": "auto_grant", "session": "azel:live", "value": False}], grant_end  # ends at hangup
        assert len(heard) == 2 and others == [], others  # both "여보세요" were heard, neither cut in nor answered
        assert " ".join(opening_said).endswith("더 궁금한 거 물어보세요.") and len(set(opening_said)) == len(opening_said)
        assert opening_said[0].startswith("정훈님, 맡기신") and len(asked) == 1  # one turn: the follow-up, no greeting reply
        assert "스타트업 B" in asked[0] and DETAIL_NOTE in asked[0]  # answered from the task's whole result
        assert fallback.startswith("전화드렸어요")  # an opening that never arrived: never silent
        assert announced == ["맡기신 정리 끝났어요.", "파일 세 개를 옮겼어요."] and results == {"an1": True, "an2": False, "an1:spoken": True}
        spoken, rejected, quiet, over, turns, asked = typed_check()
        kinds = lambda fs: [f if f == "audio" else f["type"] for f in fs]  # noqa: E731
        assert "transcript" not in kinds(spoken) and "audio" in kinds(spoken), kinds(spoken)
        assert [f["text"] for f in spoken if f != "audio" and f["type"] == "reply"] == ["네, 안녕하세요."]
        assert [f["reason"] for f in rejected] == ["too_fast", "too_long"]
        assert "audio" not in kinds(quiet) and [f["text"] for f in quiet if f != "audio" and f["type"] == "reply"] == ["조용히 답할게요."]
        assert "interrupt" in kinds(over) and kinds(over).index("interrupt") < len(over) - 1 - kinds(over)[::-1].index("reply")
        assert asked == ["안녕", "조용히", "길게", "안녕"]  # trimmed; the empty, fast and long ones never reached the model
        assert [t["input"] for t in turns] == ["text"] * 4 and turns[0]["user"] == "안녕" and turns[2]["interrupted"] is True
        hist, prompts, turn_logs = hangup_leak_check()
        assert hist[:2] == [{"role": "user", "content": "안녕"},
                            {"role": "assistant", "content": "물은 백 도에서 끓어요." + CUT_MARK}], hist
        assert len(prompts) == 2 and not any("얼음" in m["content"] or "소금" in m["content"] for m in prompts[1]), prompts
        assert turn_logs[0]["assistant"] == ["물은 백 도에서 끓어요."] and turn_logs[0]["interrupted"] is True
        assert hist[-1] == {"role": "assistant", "content": "네, 말씀하세요."}  # a reply that finished: unmarked
        assert heard_part([("a", 0, 2), ("b", 2, 4)], 3, True) == (["a", "b"], True)  # mid-"b": heard, but cut
        assert heard_part([("a", 0, 2), ("b", 2, 4)], 5, True) == (["a", "b"], False)
        assert korean_only("네, 세 시예요. (여기서 끊김)").strip() == "네, 세 시예요."
        assert [m["event"] for m in logs] == ["start", "turn", "end"], logs
        start, turn, end = logs
        assert start["direction"] == "outgoing" and start["client"] == "iphone" and start["session"] == "h"  # user called
        assert turn["user"] == "안녕" and turn["assistant"] == ["네, 안녕하세요."] and turn["route"] == "qwen"
        assert turn["interrupted"] is False and "total_ms" in turn["timings"]
        assert len({m["log_id"] for m in logs}) == 1 and end["duration_s"] >= 0
        # scheduled calls: Korean time phrases (now = Saturday 2026-11-21 08:00 KST)
        sat8 = datetime(2026, 11, 21, 8, 0, tzinfo=KST)
        for said, now, want, rep in [
            ("내일 10시에 깨워 줘", sat8, (11, 22, 10, 0), None),
            ("내일 아침 7시 반에 깨워 줘", sat8, (11, 22, 7, 30), None),
            ("30분 뒤에 전화해 줘", sat8, (11, 21, 8, 30), None),
            ("한 시간 후에 깨워 줘", sat8, (11, 21, 9, 0), None),
            ("모레 오후 3시에 전화해 줘", sat8, (11, 23, 15, 0), None),
            ("매일 7시에 깨워 줘", sat8, (11, 22, 7, 0), "daily"),
            ("평일 6시 40분에 깨워 줘", sat8, (11, 23, 6, 40), "weekdays"),  # Saturday: next is Monday
            ("매주 수요일 9시에 깨워 줘", sat8, (11, 25, 9, 0), "weekly:2"),
            ("다음 주 월요일 9시에 전화해 줘", sat8, (11, 23, 9, 0), None),
            ("10시에 깨워 줘", sat8, (11, 21, 10, 0), None),  # still ahead today
            ("10시에 깨워 줘", sat8.replace(hour=11), (11, 22, 10, 0), None),  # passed: tomorrow
            ("저녁 7시에 전화해 줘", sat8, (11, 21, 19, 0), None),
            ("밤 11시에 깨워 줘", sat8, (11, 21, 23, 0), None),
            ("일곱 시에 깨워 줘", sat8, (11, 22, 7, 0), None),
            ("오분 있다가 호출어 확인이라고 전화해줘", sat8, (11, 21, 8, 5), None),
            ("오 분만 있다가 전화해줘", sat8, (11, 21, 8, 5), None),
            ("5분만 있다가 전화해 줘", sat8, (11, 21, 8, 5), None),
            ("이십 분 뒤에 전화해 줘", sat8, (11, 21, 8, 20), None),
            ("이십오 분 후에 전화해 줘", sat8, (11, 21, 8, 25), None),
            ("십오분 뒤에 깨워 줘", sat8, (11, 21, 8, 15), None),
            ("한 시간만 지나서 전화해 줘", sat8, (11, 21, 9, 0), None),
            ("삼 시간 뒤에 전화해 줘", sat8, (11, 21, 11, 0), None),
            ("오후 삼 시 오십 분에 전화해 줘", sat8, (11, 21, 15, 50), None),
            ("내일 오전 칠 시에 깨워 줘", sat8, (11, 22, 7, 0), None),
        ]:
            at, repeat = parse_when(said, now)
            assert (at.month, at.day, at.hour, at.minute) == want and repeat == rep, (said, at, repeat)
        assert parse_when("깨워 줘", sat8) == (None, None) and parse_when("세 시간째 기다려", sat8)[0] is None
        for said in ["일 끝나면 전화해줘", "이 분 누구야", "오시면 전화해 줘", "일시 알려 줘", "이 시간에 전화해 줘", "사 분의 일"]:
            assert parse_when(said, sat8)[0] is None and schedule_intent(said) is None, said
        assert [day_part(h) for h in (0, 1, 11, 12, 13, 23)] == ["밤", "오전", "오전", "낮", "오후", "오후"]
        for said, want in [("내일 10시에 깨워 줘", "create"), ("깨워 줘", "create"), ("알람 7시로 맞춰 줘", "create"),
                           ("내일 3시에 전화해 줘", "create"), ("나한테 30분 뒤에 전화해 줘", "create"),
                           ("엄마한테 전화해 줘", None), ("엄마한테 내일 3시에 전화해 줘", None),
                           ("예약 취소해 줘", "cancel"), ("취소", "cancel"), ("내일 10시 알람 취소해 줘", "cancel"),
                           ("예약된 거 뭐 있어?", "list"), ("알람 목록 알려줘", "list"), ("내일 일정 알려줘", None),
                           ("10시에 회의 있어", None), ("클로드가 뭐야?", None),
                           ("오분 있다가 호출어 확인이라고 전화해줘", "create"), ("오 분만 있다가 전화해줘", "create"),
                           ("일 끝나면 전화해줘", None), ("이 분 누구야", None)]:
            assert schedule_intent(said) == want, (said, schedule_intent(said))
        assert to_kst(datetime(2026, 11, 22, 10, 0)).isoformat() == "2026-11-22T10:00:00+09:00"  # naive = KST
        assert to_kst(datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)).hour == 10

        class GatewayWS(FakeWS):  # a full gateway that answers schedule requests
            async def send_text(self, raw):
                await super().send_text(raw)
                m = json.loads(raw)
                if m.get("type") == "schedule":
                    say = {"create": "내일 오전 10시에 깨워 드릴게요.", "list": "예약된 전화가 없어요.",
                           "cancel": "취소할 예약이 없어요."}[m["action"]]
                    links.receive(self.lk, {"id": m["id"], "type": "schedule_result", "ok": True, "say": say})

        class TaskWS(FakeWS):  # a full gateway with Claude that starts tasks
            async def send_text(self, raw):
                await super().send_text(raw)
                m = json.loads(raw)
                if m.get("type") == "task":
                    links.receive(self.lk, {"id": m["id"], "type": "task_result", "ok": True, "task_id": "t1",
                                            "say": "맥북에서 시작할게요. 끝나면 전화드릴게요."})

        async def task_check():
            tw = TaskWS()
            tw.lk = links.add(tw)
            links.receive(tw.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})

            async def no_tts(sentence):
                return b"", 24000
            globals()["tts"] = no_tts
            history.pop("azel:tk", None)
            said = [(s, r) async for s, r, *_ in respond("맥북 다운로드 폴더 정리해 줘", "tk", time.perf_counter(), {})]
            req = tw.sent[-1]
            links.remove(tw.lk)
            return said, req
        said, req = asyncio.run(task_check())
        globals()["tts"] = real[1]
        assert said == [("맥북에서 시작할게요.", "task"), ("끝나면 전화드릴게요.", "task")], said
        assert req["type"] == "task" and req["device"] == "macbook" and req["notify"] == "call" and req["goal"].startswith("맥북")
        assert req["origin_session"] == "azel:tk"  # the finish call will continue this conversation

        async def schedule_check():
            """The fast path sends the user's words; the gateway resolves them and its result is what is said.
            Its questions are held, and the answer completes the request (ACTIONS.md §4)."""
            confirm = {"reason": "스트레칭", "kind": "reminder", "repeat_cron": "0 10-18/2 * * 1-5", "confirmed": True}

            class ToolGW(FakeWS):  # a full gateway answering like tools_bridge.run
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") != "tool_call":
                        return
                    a, out = m["args"], {"spoken": "내일 오전 10시에 깨워 드릴게요.", "needs": None}
                    if m["name"] == "schedule_cancel":
                        out["spoken"] = "취소할 예약이 없어요."
                    elif m["name"] == "schedule_list":
                        out["spoken"] = "예약된 전화가 없어요."
                    elif a.get("repeat_text"):
                        q = "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요. 맞죠?"
                        out = {"spoken": q, "needs": {"kind": "confirm", "question": q, "args": confirm}}
                    elif a.get("when_text") == "내일 7시에 전화해 줘":
                        q = "11월 22일 아침 7시요, 저녁 7시요?"
                        out = {"spoken": q, "needs": {"kind": "clarify", "question": q}}
                    elif a.get("confirmed"):
                        out["spoken"] = "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요."
                    elif a.get("when_text", "").endswith("저녁"):
                        out["spoken"] = "내일 오후 7시에 전화드릴게요."
                    links.receive(self.lk, {"id": m["id"], "type": "tool_result", "result": out["spoken"],
                                            "is_error": bool(out["needs"]), **out})
            ws = ToolGW()
            lk = ws.lk = links.add(ws)
            links.receive(lk, {"type": "hello", "device": "deneb", "mode": "full", "priority": 1, "claude": False})
            gw_tools.update(at=time.time(), defs=[{"name": n} for n in ("call_user", "schedule_list", "schedule_cancel")])

            def last():
                return [m for m in ws.sent if m.get("type") == "tool_call"][-1]
            out = [await schedule_reply("내일 10시에 깨워 줘", "create", "s", sat8)]
            assert last()["name"] == "call_user" and last()["args"] == {
                "reason": "깨우는 전화", "kind": "wake", "when_text": "내일 10시에 깨워 줘"}  # words, never a time
            out.append(await schedule_reply("취소", "cancel", "s", sat8))
            assert last()["name"] == "schedule_cancel" and last()["args"] == {}  # bare cancel: the soonest one
            night = datetime(2026, 11, 22, 1, 0, tzinfo=KST)  # 01:00: "내일" still means today (late_night)
            await schedule_reply("내일 오후 3시 알람 취소해 줘", "cancel", "s", night)
            assert last()["args"] == {"at": "2026-11-22T15:00:00+09:00"}, last()  # today's 3 pm, not the 23rd
            await schedule_reply("내일 7시 알람 취소해 줘", "cancel", "s", night)
            assert last()["args"] == {"description": "내일 7시 알람 취소해 줘"}, last()  # ambiguous: words, not the soonest
            out.append(await schedule_reply("예약된 거 뭐 있어?", "list", "s", sat8))
            out.append(await schedule_reply("내일 7시에 전화해 줘", "create", "s2", sat8))  # two readings: asked back
            assert schedule_intent("저녁", "s2") == "create"  # the answer completes it
            out.append(await schedule_reply("저녁", "create", "s2", sat8))
            assert last()["args"]["when_text"] == "내일 7시에 전화해 줘 저녁"
            out.append(await schedule_reply("평일 10시부터 6시까지 2시간마다 스트레칭하라고 전화해 줘", "create", "s3", sat8))
            assert "repeat_text" in last()["args"] and "when_text" not in last()["args"]
            assert schedule_intent("응", "s3") == "create"
            for chat in ("어제 뭐 했지?", "어떻게 해?", "네가 해 줘", "노래 틀어 줘", "아니 근데 오늘 날씨는?"):
                assert schedule_intent(chat, "s3") is None, chat  # a pending "…맞죠?" does not swallow other talk
            for yes in ("응.", "네", "어", "좋아요!", "그렇게 해 줘"):
                assert YES.search(yes), yes
            for no in ("아니", "아니요.", "취소해 줘", "됐어"):
                assert NO.search(no), no
            out.append(await schedule_reply("응", "create", "s3", sat8))
            assert last()["args"] == confirm  # resent exactly as the gateway gave it: no second guess
            out.append(await schedule_reply("평일 10시부터 6시까지 2시간마다 전화해 줘", "create", "s4", sat8))
            n = len(ws.sent)
            out.append(await schedule_reply("아니", "create", "s4", sat8))
            assert len(ws.sent) == n  # "no": nothing sent
            links.remove(lk)
            out.append(await schedule_reply("내일 10시에 깨워 줘", "create", "s5", sat8))  # no full gateway
            gw_tools.update(at=0.0, defs=[])
            return out
        replies = asyncio.run(schedule_check())
        assert replies == ["내일 오전 10시에 깨워 드릴게요.", "취소할 예약이 없어요.", "예약된 전화가 없어요.",
                           "11월 22일 아침 7시요, 저녁 7시요?", "내일 오후 7시에 전화드릴게요.",
                           "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요. 맞죠?",
                           "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요.",
                           "평일 오전 10시부터 오후 6시까지 2시간마다 전화드릴게요. 맞죠?", "알겠어요, 예약하지 않을게요.",
                           "지금은 예약을 받을 수 없어요."], replies
        at, after = tap_check(0.0)  # tapped during the first sentence: deferred until it has played
        assert 0.25 <= at < 1.0 and nomode(after) == {"type": "state", "value": "listening"}, (at, after)
        at, after = tap_check(0.4)  # after the first sentence: immediate
        assert at < 0.55 and nomode(after) == {"type": "state", "value": "listening"}, (at, after)
        at, after = tap_check(0.05, broken_tts=True)  # TTS failed: the protection is dropped, not held an hour
        assert at < 0.5 and nomode(after) == {"type": "state", "value": "listening"}, (at, after)

        def stale_audio_check(queued):
            """A reply streams with slow TTS; the user taps interrupt mid-stream (queued=True: after a follow-up was
            queued behind it while it was thinking). Returns (frames after the server's interrupt, all frames)."""
            said = iter(["광합성 설명해 줘", "그리고 예시도 같이 부탁해"])

            async def stt(audio):
                return next(said)

            async def slow_llm(messages, max_tokens=600):
                await asyncio.sleep(0.8)  # still thinking when the follow-up arrives
                for i in range(8):
                    yield f"{i + 1}번째 문장을 천천히 말하고 있어요. "
                    await asyncio.sleep(0.02)

            async def slow_tts(sentence):
                await asyncio.sleep(0.15)
                return b"\1\0" * 12000, 24000  # 0.5 s of audio, synthesized in 0.15 s: several chunks in flight
            from fastapi.testclient import TestClient
            globals().update(tts=slow_tts, llm_stream=slow_llm, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            history.clear()
            tc = TestClient(app, client=("100.64.0.8", 1))
            frames = []
            with tc.websocket_connect("/v1/call?session=stale", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes(speech)
                if queued:
                    time.sleep(0.2)
                    w.send_bytes(speech)  # "그리고 예시도…": queued behind the answer being thought out
                got_audio = 0
                while got_audio < 2:  # mid-stream: two chunks played, more synthesizing
                    m = w.receive()
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    got_audio += bool(m.get("bytes"))
                w.send_text(json.dumps({"type": "interrupt"}))
                end = time.monotonic() + 2.5
                while time.monotonic() < end:
                    try:
                        m = w.receive()
                    except Exception:
                        break
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    if len([f for f in frames if f == "audio"]) > 40:
                        break
                    if nomode(frames[-1]) == {"type": "state", "value": "listening"} and {"type": "interrupt"} in frames:
                        time.sleep(1.0)  # anything still coming from the old reply arrives now
                        w.send_text(json.dumps({"type": "hangup"}))
                        break
            time.sleep(0.1)
            cut = len(frames) - 1 - frames[::-1].index({"type": "interrupt"})  # the tap's: a joined follow-up interrupts too
            return frames[cut + 1:], frames
        globals()["speech"] = np.zeros(8000, np.int16).tobytes() + (np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()
        for queued in (False, True):
            after, frames = stale_audio_check(queued)
            assert "audio" not in after, (queued, frames)  # no old audio once the interrupt is out
            assert [nomode(x) for x in after] == [{"type": "state", "value": "listening"}], (queued, after)  # one listening, nothing old
            starts = [f for f in frames if isinstance(f, dict) and f.get("type") == "reply_start"]
            assert starts and frames.index(starts[0]) < frames.index("audio"), frames  # reply_start precedes the audio
        globals().update(tts=real[1], llm_stream=real_llm)

        def hold_check():
            """"잠깐만" alone: no transcript line, no reply, no model call; the next utterance is answered normally."""
            said, calls = iter(["잠깐만요", "내일 도시락 뭐 싸지?"]), []

            async def stt(audio):
                return next(said)

            async def llm(messages, max_tokens=600):
                calls.append(1)
                yield "김밥이 좋겠어요. "

            async def fake_tts(sentence):
                return b"\1\0" * 2400, 24000
            from fastapi.testclient import TestClient
            globals().update(tts=fake_tts, llm_stream=llm, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            history.clear()
            tc = TestClient(app, client=("100.64.0.8", 1))
            frames = []
            with tc.websocket_connect("/v1/call?session=hold", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes(speech)
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    m = w.receive()
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    if nomode(frames[-1]) == {"type": "state", "value": "listening"} and {"type": "state", "value": "thinking"} in map(nomode, [f for f in frames if isinstance(f, dict)]):
                        break
                assert not calls and not [f for f in frames if isinstance(f, dict) and f.get("type") in ("transcript", "reply")], frames
                w.send_bytes(speech)  # a real question next
                end = time.monotonic() + 4
                while time.monotonic() < end:
                    m = w.receive()
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    if isinstance(frames[-1], dict) and frames[-1].get("type") == "turn_done":
                        break
                w.send_text(json.dumps({"type": "hangup"}))
            tr = [f["text"] for f in frames if isinstance(f, dict) and f.get("type") == "transcript"]
            assert tr == ["내일 도시락 뭐 싸지?"] and calls, (tr, calls, frames)
            assert [m["content"] for m in history["azel:hold"] if m["role"] == "user"] == ["내일 도시락 뭐 싸지?"]  # no "잠깐만요" turn
        hold_check()
        globals().update(tts=real[1], llm_stream=real_llm)

        def duck_check(duck):
            """A voice over the reply: ducks=1 sends duck (the reply keeps playing, quieter) and later unduck or
            interrupt; the old apps get pause/resume. Never both kinds."""
            said = iter(["광합성 설명해 줘", "아 그렇구나"])

            async def stt(audio):
                return next(said, "음")

            async def llm(messages, max_tokens=600):
                for i in range(8):
                    yield f"{i + 1}번째 문장을 천천히 말하고 있어요. "
                    await asyncio.sleep(0.02)

            async def slow_tts(sentence):
                await asyncio.sleep(0.05)
                return b"\1\0" * 24000, 24000  # 1 s of audio per sentence: still playing when the voice comes
            from fastapi.testclient import TestClient
            globals().update(tts=slow_tts, llm_stream=llm, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            history.clear()
            tc = TestClient(app, client=("100.64.0.8", 1))
            frames = []
            with tc.websocket_connect(f"/v1/call?session=duck{duck}{'&duck=1' if duck else ''}",
                                      headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes(speech)
                got = 0
                while got < 2:
                    m = w.receive()
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    got += bool(m.get("bytes"))
                w.send_bytes(speech)  # the user talks over it
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    m = w.receive()
                    frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    kinds = {f.get("type") for f in frames if isinstance(f, dict)}
                    if kinds & {"unduck", "resume", "interrupt"}:
                        break
                w.send_text(json.dumps({"type": "hangup"}))
            kinds = [f.get("type") for f in frames if isinstance(f, dict)]
            return kinds
        k = duck_check(True)
        assert "duck" in k and "pause" not in k, k
        k = duck_check(False)
        assert "pause" in k and "duck" not in k, k
        globals().update(tts=real[1], llm_stream=real_llm)

        def hangup_check():
            """A Claude turn is running when the caller hangs up: the ask is cancelled over the link."""
            async def stt(audio):
                return "클로드로 맥북 서버 상태 자세히 봐 줘"
            from fastapi.testclient import TestClient
            globals().update(transcribe=stt, SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            gw_ws = FakeWS()
            lk = links.add(gw_ws)
            links.receive(lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?session=hang", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes(speech)
                end = time.monotonic() + 3
                while time.monotonic() < end and not any(m.get("type") == "ask" for m in gw_ws.sent):
                    time.sleep(0.02)
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.3)
            asks = [m for m in gw_ws.sent if m.get("type") == "ask"]
            links.remove(lk)
            return asks, [m for m in gw_ws.sent if m.get("type") == "cancel"]
        def late_interrupt_check():
            """Two taps on a reply that takes 5 s to stop: the first tap's wait times out after the second tap has
            claimed a newer generation, so only the newest interrupt reaches the phone (one interrupt, one listening)."""
            async def stt(audio):
                return "광합성 설명해 줘"
            calls = []

            async def stuck_tts(sentence):
                calls.append(sentence)
                if len(calls) == 2:  # the reply's second chunk ignores cancellation for 5 s
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        await asyncio.sleep(5)
                        raise
                return b"\1\0" * 48000, 24000  # 2 s of audio

            async def two_llm(messages, max_tokens=600):
                for t in ("첫 번째 문장을 길게 말해요. ", "두 번째 문장도 길게 이어서 말해요. "):
                    yield t
            from fastapi.testclient import TestClient
            import threading
            globals().update(transcribe=stt, tts=stuck_tts, llm_stream=two_llm,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            history.clear()
            tc = TestClient(app, client=("100.64.0.8", 1))
            frames = []
            with tc.websocket_connect("/v1/call?session=late", headers={"Authorization": "Bearer app-tok"}) as w:
                def reader():  # never block the test on a frame that does not come
                    try:
                        while True:
                            m = w.receive()
                            if m.get("type") == "websocket.close":
                                return
                            frames.append("audio" if m.get("bytes") else json.loads(m["text"]))
                    except Exception:
                        return
                rt = threading.Thread(target=reader, daemon=True)
                rt.start()
                w.send_bytes(speech)
                end = time.monotonic() + 5
                while "audio" not in frames and time.monotonic() < end:
                    time.sleep(0.02)
                w.send_text(json.dumps({"type": "interrupt"}))  # tap A: waits ≤ 3 s on the stuck reply
                time.sleep(0.5)
                w.send_text(json.dumps({"type": "interrupt"}))  # tap B: a newer generation
                time.sleep(4.5)  # both waits have timed out
                w.send_text(json.dumps({"type": "hangup"}))
                rt.join(3)
            time.sleep(0.1)
            return frames
        frames = late_interrupt_check()
        globals().update(tts=real[1], llm_stream=real_llm)
        assert frames.count({"type": "interrupt"}) == 1, frames  # tap A's late interrupt was skipped
        cut = frames.index({"type": "interrupt"})
        assert nomode(frames[cut + 1]) == {"type": "state", "value": "listening"} and "audio" not in frames[cut:], frames

        def origin_call_check():
            """A task's finish call (origin_session) starts with the conversation it came from, then the opening."""
            from fastapi.testclient import TestClient
            history["azel:day"] = deque([{"role": "user", "content": "다운로드 폴더 정리해 줘"},
                                         {"role": "assistant", "content": "맥북에서 시작할게요."}], maxlen=MAX_TURNS * 2)
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            links.receive(full, {"type": "call", "call_id": "cz", "opening": "정훈님, 맡기신 다운로드 정리 끝났어요.",
                                 "origin_session": "azel:day"})
            assert links.openings["cz"]["origin"] == "azel:day" and links.openings["cz"]["context"] is None
            links.receive(full, {"type": "call", "call_id": "cv", "opening": "x",
                                 "context": {"kind": "task", "label": "역사 특강 과제", "id": "abcabcabcabc"}})
            assert links.openings["cv"]["context"] == {"kind": "task", "label": "역사 특강 과제", "id": "abcabcabcabc"}
            with TestClient(app, client=("100.64.0.8", 1)).websocket_connect(
                    "/v1/call?call_id=cv&session=cv", headers={"Authorization": "Bearer app-tok"}) as w:
                hello = json.loads(w.receive_text())
                first = json.loads(w.receive_text())  # right after hello, before any state: the app's chip
                w.send_text(json.dumps({"type": "hangup"}))
            assert hello["type"] == "hello" and first == {"type": "context", "context": {"kind": "task", "label": "역사 특강 과제", "id": "abcabcabcabc"}}
            links.receive(full, {"type": "call", "call_id": "cw", "opening": "x", "origin_session": "bad id!"})
            assert links.openings["cw"]["origin"] == ""  # only a session-shaped origin is kept
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?call_id=cz&session=cz", headers={"Authorization": "Bearer app-tok"}) as w:
                time.sleep(0.3)
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.1)
            links.remove(full)
            return [m["content"] for m in history["azel:cz"]]
        globals().update(tts=lambda s: asyncio.sleep(0, (b"", 24000)))
        seeded = origin_call_check()
        globals().update(tts=real[1])
        assert seeded == ["다운로드 폴더 정리해 줘", "맥북에서 시작할게요.", "정훈님, 맡기신 다운로드 정리 끝났어요."], seeded

        asks, cancels = hangup_check()
        assert asks and [c["id"] for c in cancels] == [asks[0]["id"]], (asks, cancels)
        # no token ever reaches a log: the container disables access logs and INFO, and query strings are redacted
        cmd = json.loads(re.search(r"^CMD\s+(\[.*\])\s*$", open(os.path.join(os.path.dirname(os.path.abspath(
            __file__)), "Dockerfile")).read(), re.M).group(1))
        assert "--no-access-log" in cmd and cmd[cmd.index("--log-level") + 1] == "warning", cmd
        rec = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]',
                                (("100.64.0.8", 5), "/v1/call?token=s3cret&session=x"), None)
        RedactQuery().filter(rec)
        assert "s3cret" not in rec.getMessage() and "/v1/call?<redacted>" in rec.getMessage()
        assert any(isinstance(f, RedactQuery) for f in logging.getLogger("uvicorn.error").filters)
        def brain_check():
            """The brain link: never picked for asks or logs; its context reaches Qwen; Qwen's tool call runs on it."""
            class BrainWS(FakeWS):  # answers tool and recall requests like brain.py
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "tool":
                        links.receive(self.lk, {"id": m["id"], "type": "tool_result", "result": "- 11/19 랩미팅 발표는 다음 주로 미룸"})
                    elif m.get("type") == "recall" and "느린" not in m["text"]:
                        links.receive(self.lk, {"id": m["id"], "type": "recall_result", "hits": ["(어제) 서버 이전 끝남"]})
            gw, bws = links.add(FakeWS()), BrainWS()
            br = bws.lk = links.add(bws)
            links.receive(gw, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
            links.receive(br, {"type": "hello", "device": "brain-deneb", "mode": "full", "priority": 1, "claude": False,
                               "role": "brain"})
            assert links.pick(True) is gw and links.pick(True, need_claude=False) is gw  # never the brain
            assert links.context_for("s") == ""
            links.receive(gw, {"type": "context", "value": "가짜"})  # only the brain may set context
            links.receive(br, {"type": "context", "value": "[사용자 프로필]\n- 광주에 삶"})
            links.receive(br, {"type": "context", "session": "s1", "value": "이번 전화: 마감 알림"})
            assert links.context_for("s") == "[사용자 프로필]\n- 광주에 삶" and links.context_for("s1") == "이번 전화: 마감 알림"
            lim = links.add(FakeWS(), limited=True)
            links.receive(lim, {"type": "hello", "device": "x", "mode": "full", "role": "brain"})
            assert not lim.brain  # a link_limited token cannot pose as the brain
            links.remove(lim)
            assert [tag_state(h) for h in ("[TO", "[TOOL] {", "[C")] == ["maybe", "tool", "maybe"]
            prompts = []

            async def fake_llm(messages, max_tokens=600):
                prompts.append(messages)
                if len(prompts) == 1:
                    yield '[TOOL] {"name": "search_memory", '
                    yield '"args": {"query": "랩미팅"}}'
                else:
                    yield "다음 주로 미루셨어요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=fake_llm, tts=fake_tts)
            timings = {}

            async def run(text, session):
                return [(x, r) async for x, r, *_ in respond(text, session, time.perf_counter(), timings)]
            said = asyncio.run(run("아까 말한 랩미팅 그거 어떻게 됐지?", "s"))
            assert said == [("다음 주로 미루셨어요.", "qwen")], said
            sysmsg = prompts[0][0]["content"]
            assert "광주에 삶" in sysmsg and "서버 이전 끝남" in sysmsg and TOOL_TAG in sysmsg
            assert "랩미팅 발표는 다음 주로" in prompts[1][-1]["content"] and timings["recall_ms"] < 400
            assert not [m for m in gw.ws.sent if m.get("type") == "ask"]  # answered locally, no Claude
            late = []

            async def late_tool_llm(messages, max_tokens=600):  # words first, then a tool call
                late.append(messages)
                for d in (["찾아볼게요. [TO", 'OL] {"name": "search_memory", "args": {"query": "랩미팅"}}']
                          if len(late) == 1 else ["다음 주로 미루셨어요."]):
                    yield d
            globals()["llm_stream"] = late_tool_llm
            said = asyncio.run(run("랩미팅 어떻게 됐지?", "s"))
            assert all("TOOL" not in x and "{" not in x for x, _ in said) and said[-1] == ("다음 주로 미루셨어요.", "qwen"), said
            assert "랩미팅 발표는 다음 주로" in late[1][-1]["content"]
            # "네, 엔트로픽 주가 찾아볼게요." and nothing else: not the end of the turn. Qwen is told to call the tool now;
            # the promise is said only as the filler while it does. Twice in a row: the lookup is done for it.
            ann, ran = [], []
            saved_tools, saved_run = dict(gw_tools), _run_tool

            async def fake_run_tool(raw, session, on_say=None, meta=None, on_output=None):
                ran.append(json.loads(raw[len(TOOL_TAG):]))
                return "앤트로픽은 비상장 회사라 주가가 없어요(네이버 기준)."
            gw_tools.update(at=time.time(), defs=[{"name": "web_search", "read_only": True, "description": "검색"},
                                                  {"name": "weather", "read_only": True, "description": "날씨"}])
            globals()["_run_tool"] = fake_run_tool

            def announcer(then):
                async def llm(messages, max_tokens=600):
                    ann.append(messages)
                    yield then[min(len(ann), len(then)) - 1]
                return llm
            globals()["llm_stream"] = announcer(["네, 엔트로픽 주가 찾아볼게요.",
                                                 '[TOOL] {"name": "web_search", "args": {"query": "앤트로픽 주가"}}',
                                                 "앤트로픽은 비상장이라 주가가 없어요."])
            said = asyncio.run(run("그러면 엔트로픽 주가 지금 알려줘", "s"))
            assert [x for x, _ in said] == ["앤트로픽은 비상장이라 주가가 없어요."], said  # the promise is never said
            assert TOOL_NUDGE in " ".join(m["content"] for m in ann[1]) and ran == [{"name": "web_search", "args": {"query": "앤트로픽 주가"}}]
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["광주 날씨 확인해 볼게요.", "확인해 볼게요.", "지금 광주는 9도예요."])
            said = asyncio.run(run("광주 날씨 알려줄래?", "s"))
            assert ran == [{"name": "weather", "args": {}}] and said[-1][0] == "지금 광주는 9도예요.", (ran, said)
            assert [x for x, _ in said].count("광주 날씨 확인해 볼게요.") == 0
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["네, 지금 환율 알려드릴게요. 원/달러 환율은 약 1,310원대입니다.",
                                                 '[TOOL] {"name": "web_search", "args": {"query": "원달러 환율"}}',
                                                 "네이버 기준으로 1달러에 1,452원이에요."])
            said = asyncio.run(run("지금 1달러 환율 얼마야?", "s"))
            assert [x for x, _ in said] == ["네이버 기준으로 1달러에 1,452원이에요."], said
            assert ran == [{"name": "web_search", "args": {"query": "원달러 환율"}}]  # the guess was never said
            assert not LIVE_Q.search("3 더하기 4는 얼마야?") and LIVE_Q.search("삼성전자 주가 알려줘")
            ann.clear(), ran.clear()  # a listing claim from memory (stale live: ANT had listed) is never said
            globals()["llm_stream"] = announcer(["엔트로픽은 아직 비상장이라 주가가 없어요.",
                                                 '[TOOL] {"name": "web_search", "args": {"query": "Anthropic"}}',
                                                 "Anthropic은 88.91달러예요. 11월 21일 기준이에요."])
            said = asyncio.run(run("엔트로픽 주가 알려줘", "s"))
            assert [x for x, _ in said] == ["Anthropic은 88.91달러예요.", "11월 21일 기준이에요."] and ran, said
            assert FIGURE.search("약 천삼백십 원대예요.") and not FIGURE.search("오늘 이야기해 볼게요.")
            # Qwen's native tool-call markup (live 2026-11-22: it was spoken aloud): run it, never say it
            assert json.loads(native_call("<tool_call> <function=weather> <parameter=place> 광주 </parameter> "
                                          "<parameter=when> now </parameter> </function> </tool_call>")[len(TOOL_TAG):]) == \
                {"name": "weather", "args": {"place": "광주", "when": "now"}}
            assert json.loads(native_call('<tool_call>{"name": "web_search", "arguments": {"query": "환율"}}</tool_call>')
                              [len(TOOL_TAG):]) == {"name": "web_search", "args": {"query": "환율"}}
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["<tool_call> <function=weather> <parameter=place> 광주 </parameter> "
                                                 "</function> </tool_call>", "지금 광주는 8도예요."])
            said = asyncio.run(run("광주 날씨 알려줄래?", "s"))
            assert ran == [{"name": "weather", "args": {"place": "광주"}}] and [x for x, _ in said] == ["지금 광주는 8도예요."], said
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["확인해 볼게요. <tool_call>{\"name\": \"weather\", \"arguments\": {}}</tool_call>",
                                                 "맑아요."])
            said = asyncio.run(run("서울 날씨 어때?", "s"))
            assert ran == [{"name": "weather", "args": {}}] and not any("<" in x for x, _ in said), said
            # live 2026-11-22, brain 로컬: "…브리핑 해줘. 월요일 그러니까 내일 일정도…" got only a promise, then silence
            ask = "좀 브리핑 해줘. 월요일 그러니까 내일 일정도 좀 알아야 될 것 같고."
            promise = "구글 캘린더를 확인하고, 오늘과 내일의 수업 및 마감 일정도 함께 점검하겠습니다. 잠시만 기다려 주세요."
            sunday = datetime(2026, 11, 22, 10, 0, tzinfo=KST)
            assert agenda_range(ask, sunday) == (Date(2026, 11, 22), Date(2026, 11, 23))  # today through the named day
            assert agenda_range("다음 주까지 일정 정리해 줘", sunday) == (Date(2026, 11, 22), Date(2026, 11, 29))
            assert agenda_range("다음 주 수요일 약속 있어?", sunday) == (Date(2026, 11, 22), Date(2026, 11, 25))
            span = agenda_range(ask, datetime.now(KST))
            fetched_calls, saved_gw_tool = [], gateway_tool

            async def gw_agenda(name, args, on_say=None, on_output=None):
                fetched_calls.append((name, args))
                if prefetch_down:
                    raise RuntimeError("down")
                return "일정 (구글 캘린더와 Gistra를 합친 것, KST)\n- 10:30~11:45 수업 컴파일러"
            globals()["gateway_tool"] = gw_agenda

            async def agenda_run_tool(raw, session, on_say=None, meta=None, on_output=None):
                ran.append(json.loads(raw[len(TOOL_TAG):]))
                return "일정 (구글 캘린더와 Gistra를 합친 것, KST)\n- 10:30~11:45 수업 컴파일러"
            globals()["_run_tool"] = agenda_run_tool

            async def local_run(text):
                CALL_MODE.set("local")
                return [x async for x, *_ in respond(text, "brief", time.perf_counter(), {})]
            gw_tools.update(at=time.time(), defs=[{"name": "agenda", "read_only": True, "description": "일정"},
                                                  {"name": "web_search", "read_only": True, "description": "검색"}])
            prefetch_down = False  # the fast path: the merged schedule is in Qwen's prompt before it says a word
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["내일 열 시 반에 컴파일러 수업이 있어요."])
            said = asyncio.run(local_run(ask))
            assert fetched_calls == [("agenda", {"from": span[0].isoformat(), "to": span[1].isoformat()})], fetched_calls
            assert "수업 컴파일러" in ann[0][0]["content"] and said == ["내일 열 시 반에 컴파일러 수업이 있어요."], said
            prefetch_down = True  # agenda read failed up front, Qwen only promises: re-asked once, then looked up for it
            ann.clear(), ran.clear(), fetched_calls.clear()
            globals()["llm_stream"] = announcer([promise, promise, "내일은 컴파일러 수업이 있어요."])
            said = asyncio.run(local_run(ask))
            assert TOOL_NUDGE in " ".join(m["content"] for m in ann[1]), ann
            assert ran == [{"name": "agenda", "args": {"from": span[0].isoformat(), "to": span[1].isoformat()}}], ran
            assert said == ["내일은 컴파일러 수업이 있어요."], said  # the promise is never said, only the answer
            gw_tools.update(at=time.time(), defs=[{"name": "call_user", "description": "전화"}])  # nothing to look with
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer([promise, promise])
            said = asyncio.run(local_run(ask))
            assert said[-1] == LOOKUP_FAILED and ran == [], said  # said plainly, never back to listening in silence
            gw_tools.update(at=time.time(), defs=[{"name": "agenda", "read_only": True, "description": "일정"}])
            ann.clear(), ran.clear()  # a native call after the words is a tool call, not a promise
            globals()["llm_stream"] = announcer(['확인할게요. <tool_call>{"name": "agenda", "arguments": {}}</tool_call>',
                                                 "내일은 수업이 하나 있어요."])
            said = asyncio.run(local_run(ask))
            assert ran == [{"name": "agenda", "args": {}}] and TOOL_NUDGE not in " ".join(m["content"] for m in ann[-1]), ann
            assert said == ["내일은 수업이 하나 있어요."], said  # "확인할게요" before the call: held, never said
            ann.clear(), ran.clear()  # small talk: "잠깐만요." is just words, never a nudge or a forced lookup
            globals()["llm_stream"] = announcer(["잠깐만요. 따뜻한 차 한 잔 하고 쉬어요."])
            said = asyncio.run(local_run("나 오늘 좀 피곤하다"))
            assert " ".join(said) == "잠깐만요. 따뜻한 차 한 잔 하고 쉬어요." and ran == [] and len(ann) == 1, (said, ann)
            assert not is_promise("잠시만 기다려 주세요.", False) and is_promise("잠시만 기다려 주세요.", True)
            assert is_promise("내일 일정 확인해 볼게요.", False) and not is_promise("확인해 볼게요.", False)
            globals()["gateway_tool"] = saved_gw_tool
            # live 2026-11-22 (로컬): "일주일 동안 … 수면시간 확인해줘" got "불러올게요" three times and no tool. Any first-person
            # future reply to a request a tool plainly fits is a promise: nudged with that tool named, then called for Qwen.
            sleep_def = {"name": "phone_sleep", "description": (  # as the gateway sends it (phone_data.py)
                "아이폰·애플워치 수면 기록. '어제 몇 시간 잤어?'는 비워(가장 최근 밤), 특정한 밤은 date(일어난 날), "
                "'일주일 동안 얼마나 잤어?'처럼 여러 밤은 range(last_7_days, this_week, last_week, this_month)."),
                         "input_schema": {"type": "object", "properties": {"date": {"type": "string"},
                                                                           "range": {"type": "string"}}}}
            gw_tools.update(at=time.time(), defs=[*gw_tools["defs"], sleep_def,
                                                  {"name": "music_play", "description": "음악 틀기"}])
            assert future("네, 일주일간 수면 기록을 불러올게요.") and future("확인하겠습니다.") and future("카레는 내가 사줄게.")
            assert not future("어제 잘 잤어요.") and not future("그게 좋겠네요")
            assert matched_tool("일주일 동안 내가 얼마나 잤는지 수면시간 확인해줘.", gw_tools["defs"])["name"] == "phone_sleep"
            assert matched_tool("오늘 좀 피곤하다", gw_tools["defs"]) is None and matched_tool("카레 사줘", gw_tools["defs"]) is None
            assert infer_args(sleep_def, "일주일 동안 얼마나 잤어") == {"range": "last_7_days"} and infer_args(sleep_def, "잤어?") == {}
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["네, 일주일간 수면 기록을 불러올게요.", "네, 바로 불러올게요.",
                                                 "최근 7일 동안 하루 평균 6시간 40분 주무셨어요."])
            said = asyncio.run(run("일주일 동안 내가 얼마나 잤는지 수면시간 확인해줘.", "sl"))
            assert ran == [{"name": "phone_sleep", "args": {"range": "last_7_days"}}], ran
            assert "phone_sleep(date, range)가 맞아" in ann[1][-2 if len(ann[1]) > 2 else -1]["content"] or \
                any("phone_sleep(date, range)가 맞아" in m["content"] for m in ann[1])
            assert said[-1][0] == "최근 7일 동안 하루 평균 6시간 40분 주무셨어요." and LOOKUP_FAILED not in [x for x, _ in said]
            # the user pushing on an unkept promise: still the sleep request, not "말로만" matched to something else
            ann.clear(), ran.clear()
            pending_lookup["azel:push"] = (time.time(), "일주일 동안 내가 얼마나 잤는지 수면시간 확인해줘.")
            globals()["llm_stream"] = announcer(["네, 바로 불러올게요.", "네, 바로 불러올게요.", "평균 6시간 40분이에요."])
            said = asyncio.run(run("아니 말로만 하지 말고 진짜 하라고", "push"))
            assert ran == [{"name": "phone_sleep", "args": {"range": "last_7_days"}}] and "azel:push" not in pending_lookup, ran
            # small talk keeps its own words: no tool fits, nothing is held
            ann.clear(), ran.clear()
            globals()["llm_stream"] = announcer(["카레는 내가 사줄게."])
            said = asyncio.run(run("카레 사줘", "curry"))
            assert [x for x, _ in said] == ["카레는 내가 사줄게."] and ran == [] and len(ann) == 1, said
            text = gateway_tools_text(gw_tools["defs"], "일주일 동안 수면시간 확인해줘")
            assert "- phone_sleep(date, range): 아이폰·애플워치 수면 기록" in text
            # tool-first (live 2026-11-23 03:13, "야 지금 서버 뭐 죽은 것 같은데 한번 확인해봐"): the read-only lookup runs before
            # Qwen answers; a promise is never said; a slow lookup gets an ack, then a plain failure sentence
            global route_lookup, LOOKUP_SLOW_S, LOOKUP_S
            infra = {"name": "infra_health", "read_only": True, "description": "서버·인프라 상태", "input_schema": {"type": "object", "properties": {}}}
            gw_tools.update(at=time.time(), defs=[infra, {"name": "call_user", "description": "전화"}])
            routed = []

            async def lrun(text, session):  # local mode, as the call went
                CALL_MODE.set("local")
                return [x async for x in respond(text, session, time.perf_counter(), {})]

            async def route_infra(text, recent, tools):
                routed.append((text, [t["name"] for t in tools]))
                return [{"name": "infra_health", "args": {}}] if "서버" in text else None
            route_lookup = route_infra
            ran.clear(), ann.clear()
            globals()["llm_stream"] = announcer(["서버는 여섯 대 모두 켜져 있고 문제없어요."])
            said = asyncio.run(lrun("야 지금 서버 뭐 죽은 것 같은데 한번 확인해봐", "tf1"))
            assert ran == [{"name": "infra_health", "args": {}}] and routed[0][1] == ["infra_health"], (ran, routed)
            assert [x for x, *_ in said] == [LOOKUP_ACK, "서버는 여섯 대 모두 켜져 있고 문제없어요."], said  # the ack at once
            assert len(ann) == 1 and "[도구 결과]" in ann[0][-1]["content"]  # Qwen answered from the result, once
            ran.clear(), ann.clear()
            globals()["llm_stream"] = announcer(["저도 대표님 좋아해요."])
            said = asyncio.run(lrun("너 나 좋아해?", "tf2"))  # routed NONE: Qwen's own first answer, held then released
            assert [x for x, *_ in said] == ["저도 대표님 좋아해요."] and ran == [] and len(ann) == 1, said

            async def slow_tool(raw, session, on_say=None, meta=None, on_output=None):
                ran.append(json.loads(raw[len(TOOL_TAG):]))
                await asyncio.sleep(5)
            globals()["_run_tool"], (LOOKUP_SLOW_S, LOOKUP_S) = slow_tool, (0.05, 0.3)
            ran.clear(), ann.clear()
            said = asyncio.run(lrun("서버 괜찮아?", "tf3"))
            assert [x for x, *_ in said] == [LOOKUP_ACK, LOOKUP_SLOW] and ann == [], said  # never silent, never a guess
            globals()["_run_tool"], (LOOKUP_SLOW_S, LOOKUP_S) = fake_run_tool, (1.2, 8.0)
            route_lookup = no_route
            # live 2026-11-23 03:14: "확인해 봤는데 모두 정상" with no tool while NemoClaw was down. A server question
            # reads all three infra tools; the answer sees the down service; a checked result with no tool is never said
            infra_all = [{"name": n, "read_only": True, "description": n, "input_schema": {"type": "object", "properties": {}}}
                         for n in ("infra_health", "infra_status", "infra_alerts")]
            gw_tools.update(at=time.time(), defs=infra_all)
            canned = {"infra_health": "deneb CPU 12%", "infra_status": "멈춤: Vuja De claw (NemoClaw) http, vd-spark, 03:02부터",
                      "infra_alerts": "경보 없음"}

            async def infra_tool(raw, session, on_say=None, meta=None, on_output=None):
                d = json.loads(raw[len(TOOL_TAG):])
                ran.append(d)
                return canned[d["name"]]
            globals()["_run_tool"], route_lookup = infra_tool, route_infra
            ran.clear(), ann.clear()
            events = []

            async def lrun_ev(text, session):
                CALL_MODE.set("local")

                async def ev(e):
                    events.append(e)
                return [x async for x in respond(text, session, time.perf_counter(), {}, on_event=ev)]
            globals()["llm_stream"] = announcer(["vd-spark의 NemoClaw가 03시 2분부터 멈춰 있어요. 나머지 서버는 괜찮아요."])
            said = asyncio.run(lrun_ev("서버 상태 확인해줘. 지금 서버 중에 뭐 하나 터진 것 같다.", "tf5"))
            assert sorted(d["name"] for d in ran) == ["infra_alerts", "infra_health", "infra_status"], ran
            assert "[문제 있는 서비스]" in ann[0][-1]["content"] and "NemoClaw" in ann[0][-1]["content"]
            assert events and events[0]["label"] == "서버 상태 확인 중" and events[0]["route"] == "qwen", events
            route_lookup = no_route  # the backstop: Qwen claims a check it never made
            ran.clear(), ann.clear()
            globals()["llm_stream"] = announcer(["확인해 봤는데, 모두 정상으로 나와요.", "확인해 봤는데, 모두 정상으로 나와요.",
                                                 "NemoClaw가 멈춰 있어요."])
            said = [x for x, *_ in asyncio.run(lrun("서버 상태 확인해줘", "tf6"))]
            assert not any("정상으로 나와요" in x for x in said), said
            # live 2026-11-23 (슬프다 그래서): our nudge parroted, a promise in chat, and the title in every sentence
            gw_tools.update(at=time.time(), defs=[])
            ann.clear()
            globals()["llm_stream"] = announcer(["네, 대표님. 세 종목이 한꺼번에 빠지니 속상하시죠. 너무 속상해하지 마세요, 네, 대표님. "
                                                 "우선 주무시고, 내일 다시 한 번 상태 확인해 드릴게요. 네, 대표님. 바로 도구를 불러서 "
                                                 "찾아볼게요."])
            said = [x for x, *_ in asyncio.run(lrun("슬프다 그래서", "tf7"))]
            joined = " ".join(said)
            assert "도구" not in joined and "확인해 드릴게요" not in joined and "찾아볼게요" not in joined, said
            assert len(ann) == 1 and "속상" in joined, said  # chat: dropped silently, no re-ask
            # "다음과 같아요." with nothing after it (live 03:41): asked once for the list, the lead-in never said alone
            ann.clear()
            globals()["llm_stream"] = announcer(["메모리에서 찾은 현재 보유 주식 목록은 다음과 같아요.",
                                                 "애플, 토스, 엔비디아, 삼성전자, SK하이닉스예요."])
            said = [x for x, *_ in asyncio.run(lrun("내가 갖고 있는 주식 보유 현황 알려줘", "tf8"))]
            assert said[-1] == "애플, 토스, 엔비디아, 삼성전자, SK하이닉스예요." and LEAD_NOTE in ann[1][0]["content"], said
            # "내 주식 얼마나 올랐어?" (live 2026-11-23): memory first, then one quote per holding, all at once
            gw_tools.update(at=time.time(), defs=[{"name": "memory_recall", "read_only": True, "description": "기억"},
                                                  {"name": "stock_quote", "read_only": True, "description": "주가"}])
            hold = "[아젤 기억 2026-11-23] 사용자는 애플, 토스, 엔비디아, 삼성전자, SK하이닉스 주식을 보유하고 있다."
            quotes = {"AAPL": "애플 342.56달러, -1.90%", "NVDA": "엔비디아 125.53달러, -14.01%", "005930": "삼성전자 180,900원, -8.13%",
                      "000660": "SK하이닉스 412,000원, -5.20%", "토스": "상장된 종목을 찾지 못했어요."}

            async def own_tool(raw, session, on_say=None, meta=None, on_output=None):
                d = json.loads(raw[len(TOOL_TAG):])
                ran.append(d)
                return hold if d["name"] == "memory_recall" else quotes[d["args"]["query"]]

            async def route_holdings(text, recent, tools):
                return [{"name": "stock_quote", "args": {"query": q}} for q in quotes] if "[기억에서 찾은 것]" in text else None
            globals()["_run_tool"], route_lookup = own_tool, route_holdings
            ran.clear(), ann.clear()
            globals()["llm_stream"] = announcer(["애플 1.9%, 엔비디아 14.01%, 삼성전자 8.13%, SK하이닉스 5.2% 내렸고, 토스는 시세가 없어요."])
            said = [x for x, *_ in asyncio.run(lrun("내가 갖고 있는 주식 현황 이용해가지고 어떤 주식이 얼마만큼 올랐는지 떨어졌는지 알려줘.",
                                                    "tf9"))]
            assert [d["name"] for d in ran] == ["memory_recall"] + ["stock_quote"] * 5, ran
            prompt = " ".join(m["content"] for m in ann[0])
            assert all(v in prompt for v in quotes.values()) and "빠짐없이" in prompt, prompt[-400:]
            # with the gateway's portfolio tool (the holdings setting): that one call, no memory, no router
            gw_tools.update(at=time.time(), defs=[{"name": "memory_recall", "read_only": True, "description": "기억"},
                                                  {"name": "stock_quote", "read_only": True, "description": "주가"},
                                                  {"name": "portfolio", "read_only": True, "description": "보유 종목 시세"}])

            async def pf_tool(raw, session, on_say=None, meta=None, on_output=None):
                d = json.loads(raw[len(TOOL_TAG):])
                ran.append(d)
                return "애플 342.56달러 -1.90%, 엔비디아 125.53달러 -14.01% (11월 21일)"
            globals()["_run_tool"], route_lookup = pf_tool, route_holdings
            ran.clear(), ann.clear()
            globals()["llm_stream"] = announcer(["애플은 1.9%, 엔비디아는 14.01% 내렸어요."])
            said = [x for x, *_ in asyncio.run(lrun("내가 가진 주식 얼마나 올랐어?", "tf10"))]
            assert ran == [{"name": "portfolio", "args": {}}] and said == [LOOKUP_ACK, "애플은 1.9%, 엔비디아는 14.01% 내렸어요."], \
                (ran, said)
            globals()["_run_tool"], route_lookup = fake_run_tool, no_route
            # the call as it went: no tools at all, and Qwen only promised. Held, and the turn says it could not look
            gw_tools.update(at=time.time(), defs=[])
            ann.clear()
            globals()["llm_stream"] = announcer(["대표님, 네, 지금 서버 상태 바로 확인해 볼게요. 잠시만요."])
            said = [x for x, *_ in asyncio.run(lrun("야 지금 서버 뭐 죽은 것 같은데 한번 확인해봐", "tf4"))]
            assert not any("확인해 볼게요" in x for x in said) and said[-1] == LOOKUP_FAILED, said
            gw_tools.clear(), gw_tools.update(saved_tools)
            globals()["_run_tool"] = saved_run
            globals()["llm_stream"] = fake_llm
            prompts.clear()
            t = time.perf_counter()
            asyncio.run(run("느린 기억 찾기", "s"))  # the brain never answers recall: give up after RECALL_WAIT_S
            assert RECALL_WAIT_S <= time.perf_counter() - t < RECALL_WAIT_S + 0.5 and "관련된 기록" not in prompts[0][0]["content"]
            assert links.brain_tools() == set(BRAIN_TOOLS)  # a brain that lists no tools: the old set
            links.receive(br, {"type": "hello", "device": "brain-deneb", "mode": "full", "priority": 1, "claude": False,
                               "role": "brain", "tools": ["search_memory", "gistra_today", "infra_health", "Bad Name!", 7]})
            assert links.brain_tools() == {"search_memory", "gistra_today", "infra_health"}
            got = asyncio.run(run_tool('[TOOL] {"name": "gistra_today", "args": {}}', "s"))
            assert got == "- 11/19 랩미팅 발표는 다음 주로 미룸" and bws.sent[-1]["name"] == "gistra_today"
            assert asyncio.run(run_tool('[TOOL] {"name": "planit_create_task", "args": {}}', "s")).startswith("없는 도구")
            assert asyncio.run(run_tool('[TOOL] {"name": "add_todo", "args": {}}', "s")).startswith("없는 도구")  # not listed
            links.remove(br)
            assert links.context_for("s") == ""  # brain gone: old prompt and routing
            links.remove(gw)
            globals().update(llm_stream=real_llm, tts=real[1])
        brain_check()
        def gateway_tools_check():
            """Qwen runs a gateway tool over the link; an approval wait is spoken, then the result is answered."""
            class GatewayToolsWS(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "tools_list":
                        links.receive(self.lk, {"id": m["id"], "type": "tools", "tools": [
                            {"name": "macbook__run_command", "description": "명령 실행"},
                            {"name": "call_user", "description": "전화"}]})
                    elif m.get("type") == "tool_call":
                        links.receive(self.lk, {"id": m["id"], "type": "tool_progress", "say": "앱에서 허용해 주세요."})
                        links.receive(self.lk, {"id": m["id"], "type": "tool_result", "result": "크롬을 열었어요.",
                                                "is_error": False})
            ws = GatewayToolsWS()
            gw = ws.lk = links.add(ws)
            links.receive(gw, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            prompts = []

            async def tool_llm(messages, max_tokens=600):
                prompts.append(messages)
                yield ('[TOOL] {"name": "macbook__run_command", "args": {"command": "open -a Chrome"}}'
                       if len(prompts) == 1 else "크롬을 열었어요.")

            async def fake_tts(sentence, *a, **k):
                return b"", 24000
            globals().update(llm_stream=tool_llm, tts=fake_tts)

            gw_tools.update(at=0.0, defs=[], refreshing=0)

            async def go():
                await gateway_tools()  # starts the background refresh
                await asyncio.sleep(0.05)
                return [x async for x, *_ in respond("크롬 좀 켜 줄래", "gwt", time.perf_counter(), {})]
            said = asyncio.run(go())
            sent = [m for m in ws.sent if m.get("type") == "tool_call"]
            links.remove(gw)
            gw_tools.update(at=0.0, defs=[])
            globals().update(llm_stream=real_llm, tts=real[1])
            return said, sent, prompts
        def unconfirmed_check(utterance, replies):
            """Qwen claims an action no tool did: never spoken."""
            async def lying_llm(messages, max_tokens=600):
                yield replies.pop(0) if replies else "모르겠어요."

            async def answer_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "클로드가 답해요."

            async def fake_tts(sentence, *a, **k):
                return b"", 24000
            globals().update(llm_stream=lying_llm, claude_stream=answer_claude, tts=fake_tts, FILLER_AFTER_S=5)
            pending_schedule.pop("unc", None)
            out = asyncio.run(_collect(respond(utterance, "unc", time.perf_counter(), {})))
            globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
            return out

        async def _collect(gen):
            return [x async for x, *_ in gen]
        # 2026-11-22 transcripts: nothing was scheduled, the assistant said it would call
        # (the fast path now parses "오분 있다가 …전화해줘" itself; with no time it is Qwen's, and Qwen must not lie)
        lie = "오 분 뒤에 호출어 확인이라고 전화드릴게요."
        assert unconfirmed_check("이따가 호출어 확인이라고 전화해줘", [lie, lie]) == [ASK_WHEN]  # re-asked, still claimed
        pending_schedule.pop("unc", None)
        assert unconfirmed_check("이따가 호출어 확인이라고 전화해줘", [lie, "몇 분 뒤에 걸까요?"]) == ["몇 분 뒤에 걸까요?"]
        assert unconfirmed_check("이따가 호출어 확인이라고 전화해줘", [lie, lie]) == [ASK_WHEN]
        assert "unc" in pending_schedule  # the answer ("5분 뒤") completes the schedule
        assert unconfirmed_check("끝나면 전화해줄 수 있어", ["맥북 작업이 끝나면 바로 전화드릴게요."] * 2) == ["클로드가 답해요."] \
            or task_intent("끝나면 전화해줄 수 있어")  # or the task route takes it
        assert unconfirmed_check("물은 몇 도에서 끓어?", ["물은 100도에서 끓어요."]) == ["물은 100도에서 끓어요."]
        # A placed or reminder call: "여보세요" → "네, 제가 전화드렸어요." is the truth, never the claim fallback
        assert unconfirmed_check("여보세요", ["네, 제가 전화드렸어요. 약 드실 시간이에요."]) == ["네, 제가 전화드렸어요.", "약 드실 시간이에요."] \
            or unconfirmed_check("여보세요", ["네, 제가 전화드렸어요. 약 드실 시간이에요."]) == ["네, 제가 전화드렸어요. 약 드실 시간이에요."]
        assert unconfirmed_check("고마워", ["다음에 또 전화드릴게요."]) == ["다음에 또 전화드릴게요."]  # no action asked
        assert not action_tool("macbook__list_dir") and action_tool("mcp__macbook__run_command")  # reads never confirm
        def schedule_tool_check():
            class ToolGatewayWS(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "tool_call":
                        links.receive(self.lk, {"id": m["id"], "type": "tool_result",
                                                "result": "내일 오전 10시에 깨워 드릴게요.", "is_error": False})
            ws = ToolGatewayWS()
            gw = ws.lk = links.add(ws)
            links.receive(gw, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            gw_tools.update(at=time.time(), defs=[{"name": "call_user"}, {"name": "schedule_list", "read_only": True}])
            sat8 = datetime(2026, 11, 21, 8, 0, tzinfo=KST)
            said = asyncio.run(schedule_reply("내일 10시에 깨워 줘", "create", "st", sat8))
            sent = [m for m in ws.sent if m.get("type") in ("tool_call", "schedule")]
            links.remove(gw)
            gw_tools.update(at=0.0, defs=[])
            return said, sent
        said, sent = schedule_tool_check()
        assert said == "내일 오전 10시에 깨워 드릴게요." and len(sent) == 1 and sent[0]["type"] == "tool_call"
        assert sent[0]["name"] == "call_user" and sent[0]["args"] == {"reason": "깨우는 전화", "kind": "wake",
                                                                     "when_text": "내일 10시에 깨워 줘"}
        def claude_claim_check(events):
            """Claude's text claims a call: spoken only when a call/task tool ran in this turn."""
            async def claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                for ev in events:
                    await on_event(ev)
                yield "네, 10분 뒤에 전화드릴게요. 다른 건 없으세요?"

            async def fake_tts(sentence, *a, **k):
                return b"", 24000
            globals().update(claude_stream=claude, tts=fake_tts, FILLER_AFTER_S=5)
            out = asyncio.run(_collect(respond("오퍼스로 10분 뒤에 전화 좀", "ccc", time.perf_counter(), {})))
            globals().update(claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
            return out
        assert claude_claim_check([]) == [CLAIM_FALLBACK, "다른 건 없으세요?"]  # nothing scheduled it

        def claim_after_answer():
            async def claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "그 작업은 이미 맥북에서 진행 중이에요. 그래서 다시 맡겼어요."

            async def fake_tts(sentence, *a, **k):
                return b"", 24000
            globals().update(claude_stream=claude, tts=fake_tts, FILLER_AFTER_S=5)
            out = asyncio.run(_collect(respond("오퍼스로 그 조사 맡겨 줘", "cca", time.perf_counter(), {})))
            globals().update(claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
            return out
        assert claim_after_answer() == ["그 작업은 이미 맥북에서 진행 중이에요."]  # dropped, no fallback after an answer
        tool = {"type": "activity", "phase": "tool_start", "tool": "mcp__azel__call_user", "label": "", "kind": "generic"}
        assert claude_claim_check([tool]) == ["네, 10분 뒤에 전화드릴게요.", "다른 건 없으세요?"]
        assert action_tool("macbook__run_command") and not action_tool("mcp__claude_ai_Cygnus_MCP__gistra_today")
        said, sent, prompts = gateway_tools_check()
        assert said == ["앱에서 허용해 주세요.", "크롬을 열었어요."], said
        assert sent[0]["name"] == "macbook__run_command" and sent[0]["args"] == {"command": "open -a Chrome"}
        assert "call_user" in prompts[0][0]["content"] and "전화를 걸 수 없다고 말하지 마" in prompts[0][0]["content"]
        devs = ["albireo", "deneb", "gienah", "macbook", "ruchba", "sadr", "spark", "vd-core", "a", "b", "c", "d", "e"]
        many = [{"name": f"{d}__{t}", "description": t} for d in devs for t in DEVICE_TOOL_KO if t != "delegate"] + \
            [{"name": n, "description": n} for n in ("call_user", "memory_recall", "web_search", "infra_health",
                                                       "infra_status", "calendar_events")]
        text = gateway_tools_text(many)  # 97 tools, as live: nothing may fall off the end any more
        assert all(n in text for n in ("call_user", "memory_recall", "web_search", "infra_health", "calendar_events",
                                       "spark__run_command", "run_command=명령 실행", "vd-core")), text
        assert "도구가 없다" in text and "서버, 인프라, 노드" in text and len(text) < 4000, len(text)

        async def phone_relay_check():  # PHONE_DATA.md §3a: only a phone that offers the kind is asked
            got, to_phone = [], []

            class FakeLink:
                mode = "full"

                async def send(self, obj):
                    got.append(obj)

            async def phone_send(obj):
                to_phone.append(obj)
            lk = FakeLink()
            phones[:] = [{"send": phone_send, "kinds": {"health"}}]
            await phone_query(lk, {"id": "q1", "kind": "location"})
            await phone_query(lk, {"id": "q2", "kind": "health", "args": {"from": "2026-11-22"}})
            await phone_result({"id": "q2", "ok": True, "data": {"days": []}, "junk": 1})
            await phone_result({"id": "q9", "ok": True})  # nobody asked: dropped
            phones.clear()
            return got, to_phone
        got, to_phone = asyncio.run(phone_relay_check())
        assert got == [{"id": "q1", "type": "phone_result", "ok": False, "error": "offline"},
                       {"id": "q2", "ok": True, "data": {"days": []}, "type": "phone_result"}], got
        assert to_phone == [{"type": "phone_query", "id": "q2", "kind": "health", "args": {"from": "2026-11-22"}}]
        # "알아서 해": read back by the server, then the app toggle's grant; never Qwen's paraphrase
        pending_auto.clear()
        assert auto_intent("알아서 해줘", "azel:a") == "on" and auto_intent("이제 묻지 말고 해", "azel:a") == "on"
        assert auto_intent("자동 승인 꺼 줘", "azel:a") == "off" and auto_intent("응", "azel:a") is None  # nothing asked
        assert auto_intent("스파크 들어가서 네모클로 재시작하고 나머지는 알아서 해줘", "azel:a") is None  # a request of its own
        assert auto_intent("오늘 날씨 알려줘", "azel:a") is None
        notified = []

        async def auto_flow():
            pick, notify = links.pick, links.notify
            links.pick = lambda **kw: None

            async def fake_notify(m):
                notified.append(m)
            links.notify = fake_notify
            try:
                said = [await auto_reply("알아서 해줘", "on", "azel:a")]
                said.append(await auto_reply("응", auto_intent("응", "azel:a"), "azel:a"))
                said.append(await auto_reply("알아서 해", "on", "azel:b"))
                said.append(await auto_reply("아니", auto_intent("아니", "azel:b"), "azel:b"))
                return said
            finally:
                links.pick, links.notify = pick, notify
        said = asyncio.run(auto_flow())
        assert said[0] == AUTO_Q and said[1].startswith("네, 이번 통화 동안") and said[3].startswith("알겠어요")
        assert notified == [{"type": "auto_grant", "session": "azel:a", "value": True}]  # "아니" grants nothing
        assert not pending_auto
        said = ("실버 텔러 아이디어에 대한 시장 조사를 진행을 하고 국내외 시장 현황과 그다음에 비즈니스 아이디어에 대한 "
                "생각을 하고 그거에 대해서 나한테 문서로 보내줘 그다음에 다 끝나면 전화해줘")
        assert schedule_intent(said) is None and task_intent(said) == "call"  # "대한 시장" is no 1 o'clock
        for t, h in [("세 시에 전화해 줘", 3), ("오후세시에 깨워줘", 15), ("열한시 반에 알려줘", 11),
                     ("한 시에 전화해", 1), ("내일 3시에 전화해 줘", 3)]:
            assert parse_when(t)[0] and parse_when(t)[0].hour == h, t
        assert not CLOCK_RE.search("대한 시장") and not CLOCK_RE.search("한 시장에서 샀어")
        # a notify clause alone is no task; with the work it is (the cut-in joined to its unanswered turn)
        assert task_intent("그러고 끝나고 전화해줘.") is None and task_intent("끝나면 알려줘") is None
        assert task_intent("스탭메이트 시장 조사를 맥북에서 해서 다운로드 폴더에 저장해줘. 그러고 끝나고 전화해줘.") == "call"
        assert task_intent("빌드 돌려 놓고 끝나면 푸시로 알려줘") == "push"
        assert auto_intent("자동 승인으로 해서 진행해줘", "azel:z") is None and auto_intent("알아서 해", "azel:z") == "on"
        pending_auto.clear()
        async def resume_check():
            async def fake_request(msg, timeout=10, need_claude=False):
                assert msg == {"type": "history_turns", "call": "a" * 32}
                return {"turns": [{"user": "실버텔러 조사 맡겨줘", "assistant": "맥북에 맡겼어요."}, {"user": "", "assistant": "x"}]}
            real_req = links.request
            links.request = fake_request
            try:
                n_bad = await seed_from_call("azel:r0", "not-an-id")
                n = await seed_from_call("azel:r1", "a" * 32)
            finally:
                links.request = real_req
            return n_bad, n, list(history.pop("azel:r1", []))
        n_bad, n, seeded_turns = asyncio.run(resume_check())
        assert n_bad == 0 and n == 2 and seeded_turns == [{"role": "user", "content": "실버텔러 조사 맡겨줘"},
            {"role": "assistant", "content": "맥북에 맡겼어요."}, {"role": "assistant", "content": "x"}]
        print("voice_server checks ok")
