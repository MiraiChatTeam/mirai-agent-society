#!/bin/sh
set -eu

cleanup() {
  docker compose --profile test rm -sf test-api test-db >/dev/null
}

trap cleanup EXIT INT TERM
cleanup

docker compose --profile test up -d --build test-api

docker compose --profile test exec -T test-api \
  python -m unittest discover -s tests -v
docker compose --profile test exec -T test-api python -m tests.integration_scenario
docker compose --profile test exec -T test-api python -m tests.content_scenario
docker compose --profile test exec -T test-api python -m tests.acquisition_scenario
docker compose --profile test exec -T test-api python -m tests.challenge_corpus_scenario
docker compose --profile test exec -T test-api python -m tests.observatory_scenario

echo "Isolated MAS test suite passed; ephemeral test services were removed."
