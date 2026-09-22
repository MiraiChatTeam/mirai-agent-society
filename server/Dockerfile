FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser server/app ./app
COPY --chown=appuser:appuser server/alembic.ini .
COPY --chown=appuser:appuser server/migrations ./migrations
COPY --chown=appuser:appuser server/tests ./tests
COPY --chown=appuser:appuser data ./data

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
