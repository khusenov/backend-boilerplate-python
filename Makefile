.PHONY: check

check:
	uv lock --check
	uv run ruff format --check
	uv run ruff check
	uv run basedpyright
	uv run pytest --cov
