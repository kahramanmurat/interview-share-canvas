# Interview Share Canvas backend

Run the API from the repository root with:

```bash
uv sync
DATABASE_URL=sqlite+pysqlite:///./data/interview-share-canvas.db \
  uv run uvicorn backend.main:app --reload
```

`DATABASE_URL` accepts any SQLAlchemy database URL. SQLite is the default and
stores data in `data/interview-share-canvas.db`, and requires no extra service.
The schema uses SQLAlchemy's portable column types,
so a future Postgres deployment can use the same persistence layer after adding
the appropriate DBAPI driver. Tables are created automatically on startup, and
an empty database is seeded with four demo sessions.

The demo interviewer is `dana@northwind.dev`; the local password-login helper
uses `northwind-demo-password`. The contract's magic-link endpoint also sets a
`session` cookie and returns an `X-Session-Token` header for API clients that
prefer `Authorization: Bearer <token>`.

Run tests with:

```bash
uv run pytest
```
