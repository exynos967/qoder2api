FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY qoder2api ./qoder2api

RUN pip install --no-cache-dir .

EXPOSE 28001

CMD ["python", "-m", "qoder2api"]
