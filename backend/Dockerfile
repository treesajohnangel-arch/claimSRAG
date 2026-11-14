# ClaimsRAG backend: development image.
FROM python:3.12.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000

# Unprivileged runtime user.
RUN useradd --uid 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# Schema migrations: `docker compose exec backend alembic upgrade head`.
COPY alembic.ini ./
COPY migrations ./migrations

USER appuser

# PORT is fixed at 8000 in the container: docker-compose.yml maps and health-checks that port.
EXPOSE 8000

CMD ["python", "-m", "app"]
