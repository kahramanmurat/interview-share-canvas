FROM node:22-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.13-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/ ./backend/
COPY --from=frontend-build /app/frontend/dist/ ./frontend/

RUN useradd --create-home --uid 10001 app \
    && chown app:app /app
USER app

EXPOSE 8091

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8091"]
