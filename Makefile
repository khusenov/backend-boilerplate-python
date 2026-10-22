.PHONY: check

check:
	uv lock --check
	uv run ruff format --check
	uv run ruff check
	uv run basedpyright
	uv run lint-imports --no-logo
	uv run pytest --cov
