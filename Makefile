NEXT_REVISION = $(shell printf '%04d' $$(( $$(find migrations -name '[0-9][0-9][0-9][0-9]_*.py' | wc -l) + 1 )))

.PHONY: check db migrate migration snapshots

check:
	uv lock --check
	uv run ruff format --check
	uv run ruff check
	uv run basedpyright
	uv run lint-imports --no-logo
	uv run pytest --cov

db:
	docker compose up --wait postgres

migrate: .env
	uv run --env-file .env alembic upgrade head

migration: .env
	@test -n "$(name)" || { echo 'usage: make migration name="add users table"'; exit 1; }
	uv run --env-file .env alembic revision --autogenerate -m "$(name)" --rev-id $(NEXT_REVISION)

snapshots:
	uv run pytest tests/architecture/test_snapshots.py --update-snapshots

.env:
	cp .env.example .env
