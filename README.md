# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it

`compose.yaml` starts Agent Relay and PostgreSQL together. The database
service is named `postgres`.

```bash
docker compose up --build
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The app
reads `RELAY_DATABASE_URL` (Compose sets
`postgresql+psycopg://agent_relay:agent_relay@postgres:5432/agent_relay`).
From the host, the same credentials are published on `localhost:5432` and the
default URL is
`postgresql+psycopg://agent_relay:agent_relay@localhost:5432/agent_relay`.

To run the API on the host against that database:

```bash
uv sync
uv run uvicorn main:app --reload
```

`GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models and the PostgreSQL engine.
`storage.py` contains task/claim/recovery operations; claims use
`FOR UPDATE SKIP LOCKED` so concurrent workers do not take the same task.
Routes and request models are kept in `main.py` and `schemas.py`. The HTTP
protocol and lifecycle are defined in `SPEC.md`.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests default to the `agent_relay_test` database on `localhost:5432` (same
user and password as Compose) so they don't reset the dev server's
`agent_relay` database. The fixture drops and recreates all tables on
whatever `RELAY_DATABASE_URL` points at, so stop the dev server first or set
`RELAY_DATABASE_URL` to a scratch database before running tests against
another database. Start PostgreSQL with `docker compose up postgres -d`
before `pytest`.
