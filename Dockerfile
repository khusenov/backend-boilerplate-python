FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.14-slim-trixie AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie
RUN python -m compileall -q -x site-packages /usr/local/lib
RUN useradd --uid 10001 --user-group --no-create-home app
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY pyproject.toml ./
COPY migrations migrations
ENV PATH=/app/.venv/bin:$PATH
USER app
EXPOSE 8000
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--timeout-keep-alive", "120", "--timeout-graceful-shutdown", "20"]
