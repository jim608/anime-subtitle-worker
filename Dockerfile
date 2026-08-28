FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/work/huggingface \
    XDG_CACHE_HOME=/work/cache \
    TORCH_HOME=/work/torch \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.11/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/lib/python3.11/site-packages/nvidia/cufft/lib:/usr/local/lib/python3.11/site-packages/nvidia/curand/lib:/usr/local/lib/python3.11/site-packages/nvidia/cusolver/lib:/usr/local/lib/python3.11/site-packages/nvidia/cusparse/lib:/usr/local/lib/python3.11/site-packages/nvidia/nccl/lib:/usr/local/lib/python3.11/site-packages/nvidia/nvjitlink/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        libgomp1 \
        libsndfile1 \
        mkvtoolnix \
        opencc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade "pip<26" "setuptools<81" wheel \
    && python -m pip install -r /app/requirements.txt \
    && python -m pip install --no-deps stable-ts==2.19.1

ARG SOURCE_REVISION=unknown
RUN printf '%s\n' "$SOURCE_REVISION" > /app/.source-revision

COPY *.py /app/
COPY acceptance /app/acceptance
COPY config.yaml /app/config.yaml

CMD ["python", "main.py", "--config", "config.yaml", "--auto-watch"]
