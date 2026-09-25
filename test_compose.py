"""Deployment tests against compose.yaml.

These check the image, the published port, the postgres hostname, and data that
survives an API restart. Protocol cases stay in the faster pytest modules.

Host ports are overridden so the suite can run while another process holds
8000 or 5432. The API container still listens on 8000 and uses the database
URL from compose.yaml. The worker process imports the app, so it receives the
published Postgres URL. Set COMPOSE_TESTS=1 to run this module.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent
PROJECT = "agent-relay-compose-test"

pytestmark = pytest.mark.skipif(os.getenv("COMPOSE_TESTS") != "1", reason="set COMPOSE_TESTS=1 to run compose.yaml tests")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _compose(override: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "-f", str(ROOT / "compose.yaml"), "-f", str(override), *args]


def _run(cmd: list[str], timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)


def _wait_until_ready(base_url: str, timeout_seconds: float = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/ready", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"{response.status_code} {response.text}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"relay did not become ready at {base_url}: {last_error}")


@pytest.fixture(scope="module")
def compose_relay() -> Iterator[tuple[str, Path, str]]:
    host_port = _free_port()
    db_port = _free_port()
    with tempfile.TemporaryDirectory() as tmp:
        override = Path(tmp) / "override.yaml"
        override.write_text(
            "\n".join(
                [
                    "services:",
                    "  postgres:",
                    "    ports: !override",
                    f'      - "127.0.0.1:{db_port}:5432"',
                    "  agent-relay:",
                    "    ports: !override",
                    f'      - "127.0.0.1:{host_port}:8000"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        up = _run(_compose(override, "up", "--build", "-d", "--wait"))
        try:
            if up.returncode != 0:
                raise RuntimeError(up.stdout + up.stderr)
            base_url = f"http://127.0.0.1:{host_port}"
            database_url = f"postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:{db_port}/agent_relay"
            _wait_until_ready(base_url)
            yield base_url, override, database_url
        finally:
            _run(_compose(override, "down", "-v", "--remove-orphans"), timeout=120)


def test_stack_health_and_readiness(compose_relay: tuple[str, Path, str]) -> None:
    base_url, _override, _database_url = compose_relay
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ready"}


def test_acceptance_scenario_1_through_published_port(compose_relay: tuple[str, Path, str]) -> None:
    base_url, _override, database_url = compose_relay
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        alice = client.post("/api/v1/agents", json={"name": "alice-sender", "description": "compose sender"})
        bob = client.post("/api/v1/agents", json={"name": "bob-uppercase", "description": "compose worker"})
        assert alice.status_code == 201, alice.text
        assert bob.status_code == 201, bob.text
        alice_body = alice.json()
        bob_body = bob.json()
        sender_headers = {"Authorization": f"Bearer {alice_body['token']}"}
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": bob_body["agent_id"], "input": "Review this Python function: ..."},
        )
        assert task.status_code == 201, task.text
        task_body = task.json()
        assert task_body["status"] == "queued"

    worker = subprocess.run(
        [
            sys.executable,
            "main.py",
            "worker",
            "--base-url",
            base_url,
            "--agent-id",
            bob_body["agent_id"],
            "--token",
            bob_body["token"],
            "--worker-id",
            "compose-worker",
            "--wait-seconds",
            "30",
            "--stop-after",
            "1",
        ],
        cwd=ROOT,
        env={**os.environ, "RELAY_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert worker.returncode == 0, worker.stdout + worker.stderr

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        result = client.get(f"/api/v1/tasks/{task_body['task_id']}", headers=sender_headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "completed"
    assert body["output"] == "REVIEW THIS PYTHON FUNCTION: ..."
    assert body["from"] == alice_body["agent_id"]
    assert body["to"] == bob_body["agent_id"]
    assert body["attempt_count"] == 1


def test_restart_keeps_queued_work_and_recovers_expired_lease(compose_relay: tuple[str, Path, str]) -> None:
    base_url, override, _database_url = compose_relay
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sender = client.post("/api/v1/agents", json={"name": "restart-sender"})
        recipient = client.post("/api/v1/agents", json={"name": "restart-recipient"})
        assert sender.status_code == 201, sender.text
        assert recipient.status_code == 201, recipient.text
        sender_headers = {"Authorization": f"Bearer {sender.json()['token']}"}
        recipient_headers = {"Authorization": f"Bearer {recipient.json()['token']}"}
        recipient_id = recipient.json()["agent_id"]
        processing = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient_id, "input": "expire me"},
        )
        assert processing.status_code == 201, processing.text
        processing_id = processing.json()["task_id"]
        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "compose-restart", "wait_seconds": 0},
        )
        assert claim.status_code == 200, claim.text
        assert claim.json()["task_id"] == processing_id
        queued = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient_id, "input": "stay queued"},
        )
        assert queued.status_code == 201, queued.text
        queued_id = queued.json()["task_id"]

    if not processing_id.startswith("task_") or not processing_id.replace("_", "").isalnum():
        raise AssertionError(f"unexpected task id {processing_id}")
    expired = _run(
        _compose(
            override,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "agent_relay",
            "-d",
            "agent_relay",
            "-c",
            "UPDATE attempts SET lease_expires_at = (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 minute' "
            f"WHERE task_id = '{processing_id}' AND outcome = 'processing';",
        )
    )
    assert expired.returncode == 0, expired.stdout + expired.stderr

    restarted = _run(_compose(override, "restart", "agent-relay"), timeout=120)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    _wait_until_ready(base_url)

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        deadline = time.monotonic() + 20
        recovered = None
        while time.monotonic() < deadline:
            recovered = client.get(f"/api/v1/tasks/{processing_id}", headers=sender_headers)
            if recovered.status_code == 200 and recovered.json()["status"] == "queued":
                break
            time.sleep(0.5)
        assert recovered is not None and recovered.status_code == 200, recovered.text if recovered else ""
        assert recovered.json()["status"] == "queued"
        assert recovered.json()["attempt_count"] == 1

        still_queued = client.get(f"/api/v1/tasks/{queued_id}", headers=sender_headers)
        assert still_queued.status_code == 200, still_queued.text
        assert still_queued.json()["status"] == "queued"

        released = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "compose-after-restart", "wait_seconds": 0},
        )
        assert released.status_code == 200, released.text
        assert released.json()["task_id"] == processing_id
        assert released.json()["attempt"] == 2
        untouched = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "compose-after-restart", "wait_seconds": 0},
        )
        assert untouched.status_code == 200, untouched.text
        assert untouched.json()["task_id"] == queued_id
        assert untouched.json()["attempt"] == 1
