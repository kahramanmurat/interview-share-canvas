FROM node:22-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.13-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

ENV PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL="sqlite+pysqlite:////data/interview-share-canvas.db" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/ ./backend/
COPY --from=frontend-build /app/frontend/dist/ ./frontend/

RUN useradd --create-home --uid 10001 app \
    && mkdir /data \
    && chown app:app /app /data
USER app

EXPOSE 8091
VOLUME ["/data"]

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8091"]
