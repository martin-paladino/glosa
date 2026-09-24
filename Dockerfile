# Glosa: python:3.12-slim + ffmpeg (glosa/audio/ingest.py shells out to it
# for every source) + uv (dependency install and the runtime entrypoint).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.10 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first (pyproject.toml has no [build-system]: uv installs the
# pinned dependencies only, it does not build/install the `glosa` package
# itself, so this layer needs nothing else) so editing the app afterwards
# doesn't invalidate it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8000

# The official entrypoint (Ruling 28): `python -m glosa.web.app` reads .env
# and config.yaml from the working directory and shuts down gracefully
# (3s grace) so open audience SSE streams don't block a stop/restart.
CMD ["python", "-m", "glosa.web.app"]
