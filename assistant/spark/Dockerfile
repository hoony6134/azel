FROM qwen3-tts:local
RUN pip install --no-cache-dir python-multipart httpx pysilero-vad
WORKDIR /app
COPY voice_server.py .
# No access log: /v1/call carries its token in the query string.
CMD ["uvicorn", "voice_server:app", "--host", "100.64.0.11", "--port", "8891", "--no-access-log"]
