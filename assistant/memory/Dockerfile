FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv
WORKDIR /app
ENV HF_HOME=/models UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
# Bake the embedding model into the image so restarts never need the network.
RUN .venv/bin/python -c "from huggingface_hub import hf_hub_download as d; r='Xenova/multilingual-e5-small'; d(r,'tokenizer.json'); d(r,'onnx/model_quantized.onnx')" \
    && chmod -R a+rX /models
COPY memory_service.py ./
COPY tests ./tests
ENV HF_HUB_OFFLINE=1 BISEO_HOME=/config
# Host networking (compose.yaml): bind ruchba's Tailscale IP only (BISEO_HOST, default 100.64.0.10), never 0.0.0.0.
CMD [".venv/bin/python", "memory_service.py"]
