FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 CUDA_VISIBLE_DEVICES="" \
    NVIDIA_VISIBLE_DEVICES=void OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    TOKENIZERS_PARALLELISM=false PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock /requirements.lock
RUN python -m pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /requirements.lock \
    && python -m pip check \
    && useradd --uid 99 --gid 100 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin laya
WORKDIR /app
COPY sidecar.py events.py test_sidecar.py /app/
USER 99:100
LABEL io.subtitle.project="anime-subtitle-worker" io.subtitle.purpose="laya-permanent-readonly-diagnostics" \
      io.subtitle.temporary="false" io.subtitle.creator="codex" \
      io.subtitle.laya.sdk="c7527708f9f5220c669d8aa385077cd28d04708a" \
      io.subtitle.laya.model="1c5edc17a7acd8701df6fc341c0d179f1c62c982"
CMD ["python", "/app/sidecar.py", "serve"]
