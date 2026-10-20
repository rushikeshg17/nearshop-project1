.PHONY: setup db-up db-down schema seed api web test retrain lint

# The local MongoDB from docker-compose.yml: a single-node replica set on localhost:27017.
LOCAL_MONGODB_URI ?= mongodb://localhost:27017/?replicaSet=rs0
LOCAL_MONGODB_DB ?= nearshop
# The test suite reads only this URI and refuses anything but a local server. It reaches the same
# local server as LOCAL_MONGODB_URI, written differently because the suite also refuses a URI equal
# to NEARSHOP_MONGODB_URI; each run creates and drops its own nearshop_test_<random> database.
NEARSHOP_TEST_MONGODB_URI ?= mongodb://127.0.0.1:27017/?directConnection=true
# How db-up starts MongoDB. CI, which runs MongoDB as a service container, runs make test DB_UP=
DB_UP ?= docker compose up -d --wait mongo

setup:
	cd backend && uv sync && ( [ -f .env ] || cp .env.example .env )
	cd frontend && npm install

db-up:
	$(DB_UP)

db-down:
	docker compose down

schema:
	cd backend && uv run python -m database.schema

# Always seeds the local database, whatever backend/.env points at. To seed a demo cluster, run the seed
# module yourself with NEARSHOP_ENVIRONMENT=demo and --confirm-db (see README).
seed: db-up
	cd backend && NEARSHOP_MONGODB_URI='$(LOCAL_MONGODB_URI)' NEARSHOP_MONGODB_DB='$(LOCAL_MONGODB_DB)' \
		uv run python -m database.seed.seed --confirm-db '$(LOCAL_MONGODB_DB)'

api:
	cd backend && uv run uvicorn app.main:app --reload --reload-dir app --port 8000

web:
	cd frontend && npm run dev

test: db-up
	cd backend && NEARSHOP_TEST_MONGODB_URI='$(NEARSHOP_TEST_MONGODB_URI)' uv run pytest -q

retrain:
	cd backend && uv run python -m app.ai.pipeline

lint:
	cd backend && uv run ruff check app database tests
	cd frontend && npx tsc --noEmit && npx eslint src
