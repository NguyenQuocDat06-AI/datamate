FROM --platform=linux/amd64 python@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944 AS deps
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu128
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && pip check && pip freeze > /opt/requirements.lock.txt
FROM deps AS submit
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false PYTHONHASHSEED=42 NCCL_P2P_DISABLE=1 \
    NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost OMP_NUM_THREADS=4 \
    DATAMATE_AUDIT_LOG_DIR=/result/logs \
    PYTHONPATH=/code/src
WORKDIR /code/src
COPY code/ /code/
RUN chmod +x /code/predict.sh && cp /opt/requirements.lock.txt /code/requirements.lock.txt
ENTRYPOINT []
CMD ["bash", "/code/predict.sh"]
