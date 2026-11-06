# The agent API (#28), built for linux/arm64 in production (t4g server, GitHub's arm64 runner).
# Guidebooks are copyrighted and mounted at runtime, never copied in (see .dockerignore).
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them. --no-default-groups leaves out dev
# tools and PyMuPDF (AGPL, ingest only).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-default-groups --no-editable

FROM python:3.12-slim-bookworm
ENV PATH=/app/.venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/guidebooks \
    && chown -R app:app /app/data
WORKDIR /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
EXPOSE 8000
# Not `travel-agent`: that entry point reloads on code changes, which production must not do
CMD ["uvicorn", "travel_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
