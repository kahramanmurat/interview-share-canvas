# Interview Share Canvas backend

Run the API from the repository root with:

```bash
uv sync
uv run uvicorn backend.main:app --reload
```

The backend is intentionally in-memory and seeds four sessions on startup.
The demo interviewer is `dana@northwind.dev`; the local password-login helper
uses `northwind-demo-password`. The contract's magic-link endpoint also sets a
`session` cookie and returns an `X-Session-Token` header for API clients that
prefer `Authorization: Bearer <token>`.

Run tests with:

```bash
uv run pytest
```
