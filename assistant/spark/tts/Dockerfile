FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates libsndfile1 sox \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
# Matched torch/torchaudio pair from the official CUDA 13 index (aarch64 build supports GB10).
RUN pip install --no-cache-dir torch==2.10.* torchaudio==2.10.* --index-url https://download.pytorch.org/whl/cu130
RUN pip install --no-cache-dir --no-deps qwen-tts \
 && pip install --no-cache-dir "transformers==4.57.3" accelerate einops librosa onnxruntime soundfile sox fastapi "uvicorn[standard]"
WORKDIR /app
COPY server.py .
EXPOSE 8890
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8890"]
