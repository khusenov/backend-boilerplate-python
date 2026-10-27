NEXT_REVISION = $(shell printf '%04d' $$(( $$(find migrations -name '[0-9][0-9][0-9][0-9]_*.py' | wc -l) + 1 )))
SMOKE = docker compose --project-name backend-boilerplate-smoke --file compose.yaml --file compose.smoke.yaml --profile full

.PHONY: check db migrate migration snapshots dev api worker image smoke smoke-run

check:
	uv lock --check
	uv run ruff format --check
	uv run ruff check
	uv run basedpyright
	uv run lint-imports --no-logo
	uv run pytest --cov

db:
	docker compose up --wait postgres

migrate: .env db
	uv run --env-file .env alembic upgrade head

migration: .env
	@test -n "$(name)" || { echo 'usage: make migration name="add users table"'; exit 1; }
	uv run --env-file .env alembic revision --autogenerate -m "$(name)" --rev-id $(NEXT_REVISION)

snapshots:
	uv run pytest tests/architecture/test_snapshots.py --update-snapshots

dev: migrate
	$(MAKE) --no-print-directory -j 2 api worker

api: .env
	uv run --env-file .env uvicorn app.main:create_app --factory --reload --reload-dir src \
		--no-proxy-headers --no-access-log --timeout-keep-alive 120 --timeout-graceful-shutdown 20

worker: .env
	uv run --env-file .env python -m app.worker

image:
	docker build --build-arg GIT_SHA=$$(git rev-parse --verify --quiet HEAD 2>/dev/null || echo unknown) --tag backend-boilerplate .

smoke: image
	status=0; $(MAKE) --no-print-directory smoke-run || status=$$?; \
	if [ $$status -ne 0 ]; then $(SMOKE) logs; fi; \
	$(SMOKE) down --volumes; exit $$status

smoke-run:
	$(SMOKE) up --wait
	curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' "http://$$($(SMOKE) port api 8000)/health/ready" | grep -x 200
	i=0; until $(SMOKE) exec -T postgres psql --username=app --tuples-only --no-align \
		--command="SELECT 1 FROM procrastinate_jobs WHERE task_name = 'heartbeat' AND status = 'succeeded'" | grep -qx 1; do \
		i=$$((i + 1)); [ $$i -lt 30 ] || { echo 'no heartbeat' >&2; exit 1; }; sleep 1; \
	done

.env:
	cp .env.example .env
