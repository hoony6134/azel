"""OpenAI-compatible /v1/audio/speech server for Qwen3-TTS CustomVoice."""
import io
import os
import threading

import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from qwen_tts import Qwen3TTSModel

MODEL_ID = os.environ.get("TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
DEFAULT_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "Sohee")
MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "4000"))

model = Qwen3TTSModel.from_pretrained(
    MODEL_ID, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa"
)
SPEAKERS = {str(s).lower(): str(s) for s in model.get_supported_speakers()}
_gpu_lock = threading.Lock()  # one generation at a time on the shared GPU
_MEDIA = {"wav": ("WAV", "audio/wav"), "mp3": ("MP3", "audio/mpeg"), "flac": ("FLAC", "audio/flac")}

app = FastAPI(title="Qwen3-TTS")


def max_tokens_for(text: str) -> int:
    return int(12 * (2.0 + 0.3 * len(text)))


class SpeechRequest(BaseModel):
    input: str
    voice: str = DEFAULT_VOICE
    model: str | None = None
    response_format: str = "mp3"
    speed: float = 1.0  # shortcut: accepted for OpenAI compatibility but ignored; Qwen3-TTS has no speed knob
    instructions: str | None = None
    language: str = "Auto"


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL_ID}


@app.get("/v1/audio/voices")
def voices():
    return {"voices": sorted(SPEAKERS.values()), "default": DEFAULT_VOICE}


@app.post("/v1/audio/speech")
def speech(req: SpeechRequest):
    text = req.input.strip()
    if not text:
        raise HTTPException(400, "input is empty")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"input longer than {MAX_CHARS} characters")
    # Unknown voices (e.g. OpenAI's "nova") fall back to the default speaker.
    speaker = SPEAKERS.get(req.voice.lower(), SPEAKERS.get(DEFAULT_VOICE.lower(), DEFAULT_VOICE))
    fmt = _MEDIA.get(req.response_format.lower())
    if fmt is None:
        raise HTTPException(400, f"response_format must be one of {sorted(_MEDIA)}")
    kwargs = {"instruct": req.instructions} if req.instructions else {}
    # Cap audio length (12 codec tokens per second): ~0.3 s per character + 2 s slack.
    # Very short inputs like "네," can otherwise run away into many seconds of noise.
    kwargs["max_new_tokens"] = max_tokens_for(text)
    with _gpu_lock:
        wavs, sr = model.generate_custom_voice(
            text=text, language=req.language, speaker=speaker, **kwargs
        )
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format=fmt[0])
    return Response(buf.getvalue(), media_type=fmt[1])
