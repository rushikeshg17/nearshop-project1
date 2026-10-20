# NearShop API

FastAPI + MongoDB (PyMongo, Pydantic document models). See the root README for setup, the demo script and the architecture.

- `uv run uvicorn app.main:app --reload`: run the API (interactive docs at http://localhost:8000/docs)
- `uv run python -m database.seed.seed`: rebuild and seed the demo database, then train all models
- `uv run pytest -q`: tests
- `make migrate` (from the repository root): apply pending schema migrations to the local database, then create or update collections, validators and indexes (idempotent, never drops). Run it after pulling a change that adds a migration, and once on a local database seeded before migrations existed: until then the API logs "database schema is behind" at every start. `MIGRATE_ARGS=--status` only reports. The runner is `uv run python -m database.migrate`; how to write a migration is in `database/migrations/__init__.py`. The data model and how it maps from the old relational schema is documented at the top of `database/schema.py`
