FROM qwen3-tts:local
RUN pip install --no-cache-dir python-multipart httpx pysilero-vad
WORKDIR /app
COPY voice_server.py late_night.py ./
# No access log, and warnings only: uvicorn logs every WebSocket handshake ("WebSocket /v1/call?token=…
# [accepted]") through its error logger at INFO, which --no-access-log does not cover. voice_server.py also
# redacts query strings in uvicorn's log lines. Checked by `python voice_server.py --check`.
CMD ["uvicorn", "voice_server:app", "--host", "100.64.0.11", "--port", "8891", "--no-access-log", "--log-level", "warning"]
