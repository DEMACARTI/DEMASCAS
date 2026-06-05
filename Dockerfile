FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/demascas \
    DEMASCAS_HOST=0.0.0.0 \
    DEMASCAS_PORT=5001 \
    DEMASCAS_SKIP_MCP_BOOT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash demascas

WORKDIR /app/DEMASCAS

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

RUN mkdir -p /home/demascas/.demascas \
    && chown -R demascas:demascas /home/demascas /app

USER demascas

EXPOSE 5001

ENTRYPOINT ["tini", "--"]
CMD ["python", "main.py"]