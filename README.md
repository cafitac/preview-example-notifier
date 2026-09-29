# preview-example-notifier

An internal FastAPI notification receiver for preview-hub. It accepts events for
inspection; it does not send email or deliver notifications to external systems.

- `POST /api/notify`: `{"event":"note.created","payload":{"id":"example"}}`
  returns HTTP 202 with a UUID `id` and UTC ISO `received_at`.
- `GET /api/notifications`: returns notification objects (`id`, `event`, `payload`,
  `received_at`), newest first.
- `GET /healthz`: returns `{"status":"ok"}`.

Events must contain 1–100 characters and cannot contain NUL or lone surrogates.
Payloads must be JSON objects. A thread-safe in-memory store retains only the last
1,000 accepted events. Restarting clears the store; run one worker because stores
are not shared between processes.

## Preview-hub wiring

`preview.yaml` declares service `notifier` on port 8000 with HTTP health checking.
It has no `expose` or resources, so it is reachable only on the environment network
at `http://notifier:8000`, without a public proxy route or database.

The hub catalog can register `cafitac/preview-example-notifier` with
`include: on_request`. Include it with `--set notifier=main` when creating an
environment. The backend's optional `requires` entry for `notifier` enables
`NOTIFIER_URL: ${services.notifier.internal_url}` when notifier is included; when
absent, that variable is omitted. Consumers post to `${NOTIFIER_URL}/api/notify`.

## Local development

Use Python 3.12 and uv. The committed `uv.lock` is used by CI and Docker builds.
Run:

```sh
uv sync --locked
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -ra
docker build -t preview-example-notifier:local .
```

The image installs only locked runtime dependencies and runs as UID 10001.
