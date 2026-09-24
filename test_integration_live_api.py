"""HTTP integration tests against a live uvicorn process and PostgreSQL.

These complement in-process TestClient tests in ``test_agent_relay.py`` by
exercising the same paths a operator or worker CLI uses over the network.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(base_url: str, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/ready", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"relay did not become ready at {base_url}") from last_error


@pytest.fixture
def live_relay() -> Iterator[str]:
    database_url = os.environ.get(
        "RELAY_DATABASE_URL",
        "postgresql+psycopg://agent_relay:agent_relay@localhost:5432/agent_relay_test",
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["RELAY_DATABASE_URL"] = database_url
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_acceptance_scenario_1_register_send_claim_complete_read(live_relay: str) -> None:
    """SPEC acceptance scenario 1 over live HTTP and PostgreSQL."""

    base = live_relay
    with httpx.Client(base_url=base, timeout=60.0) as client:
        alice = client.post("/api/v1/agents", json={"name": "alice-sender", "description": "integration sender"})
        bob = client.post("/api/v1/agents", json={"name": "bob-uppercase", "description": "integration worker"})
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
            base,
            "--agent-id",
            bob_body["agent_id"],
            "--token",
            bob_body["token"],
            "--worker-id",
            "integration-worker",
            "--wait-seconds",
            "30",
            "--stop-after",
            "1",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert worker.returncode == 0, worker.stdout + worker.stderr

    with httpx.Client(base_url=base, timeout=30.0) as client:
        result = client.get(f"/api/v1/tasks/{task_body['task_id']}", headers=sender_headers)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "completed"
        assert body["output"] == "REVIEW THIS PYTHON FUNCTION: ..."
        assert body["from"] == alice_body["agent_id"]
        assert body["to"] == bob_body["agent_id"]
        assert body["attempt_count"] == 1
