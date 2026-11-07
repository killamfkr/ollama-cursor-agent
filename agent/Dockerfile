FROM python:3.12-slim-bookworm

ARG APP_VERSION=0.2.6
LABEL org.opencontainers.image.version="${APP_VERSION}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV PYTHONUNBUFFERED=1
ENV AGENT_WORKSPACE_ROOT=/workspace
ENV AGENT_OLLAMA_BASE_URL=http://ollama:11434

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
