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
COPY --chown=appuser:appuser skill ./skill
COPY --chown=appuser:appuser docs/MAS_CONSTITUTION.md docs/mas_constitution_v1.json docs/POLICY.md docs/PROTOCOL.md docs/PRIVACY.md docs/AUTH.md docs/ADMISSION_AND_MODERATION.md docs/DATASET_POLICY.md docs/CONTENT_ENVIRONMENT.md docs/WORLD_PULSE_ACQUISITION.md docs/CHALLENGE_CORPUS.md docs/CHALLENGE_CORPUS_FREEZE.md ./docs/

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec python -m app.serve"]
