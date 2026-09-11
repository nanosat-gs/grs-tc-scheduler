# TC Scheduler: laço de planejamento + API de leitura (5591) no mesmo processo.
#
# Sem gcc nem libpq-dev: o psycopg2-binary traz wheel manylinux pronto, e
# compilar do zero custaria ~200 MB de imagem para nada. Se algum dia o wheel
# não existir para a plataforma alvo, o build falha aqui de forma explícita e
# aí sim vale reintroduzir os dois.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# git é obrigatório: o pip resolve `spacelab-tracking @ git+https://...`
# clonando. Sem ele o build falha com "Cannot find command 'git'".
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# A tag da biblioteca de tracking como ARG: `--build-arg TRACKING_REF=v0.2.0`
# invalida a layer do pip de forma explícita. Precisa acompanhar o pyproject,
# e precisa bater com a do grs-station-manager.
ARG TRACKING_REF=v0.1.0
ENV TRACKING_REF=${TRACKING_REF}

COPY pyproject.toml README.md ./
COPY src/ ./src/
# tools/ entra na imagem: station_demo.py é rodado com `docker compose exec`
# e é o caminho documentado para exercitar uma passagem sem esperar uma real.
COPY tools/ ./tools/

RUN pip install --no-cache-dir -e .

# Cache local de dados orbitais (TRACKING_DATA_DIR). Montado como volume no
# compose, para que o fallback offline do CelesTrak sobreviva a um restart.
RUN mkdir -p /app/data

CMD ["python", "-m", "tc_scheduler.main"]
