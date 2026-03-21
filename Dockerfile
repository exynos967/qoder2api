FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY qoder2api ./qoder2api
COPY vendor/qoder-machine /opt/qoder-machine

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && chmod +x \
        /opt/qoder-machine/qoder-machine/xieyi/qoder_pure_login.py \
        /opt/qoder-machine/qoder-machine/xieyi/qoder_pure_batch_builder.py \
        /opt/qoder-machine/qoder-machine/xieyi/qoder_pure_protocol.py \
    && pip install --no-cache-dir .

EXPOSE 28001

CMD ["python", "-m", "qoder2api"]
