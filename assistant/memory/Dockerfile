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
# 0.0.0.0 inside the container only; compose publishes the port on the Tailscale IP alone.
CMD [".venv/bin/uvicorn", "memory_service:app", "--host", "0.0.0.0", "--port", "8894"]
