from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from tests.container_mcp_probe import (
    TASK_ID,
    exercise_task,
    unauthenticated_status,
)


ROOT = Path(__file__).resolve().parents[1]
HOST_TOKEN = "host-smoke-token-" + "h" * 32


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    image = f"xuanji-native-host-smoke:{suffix}"
    container = f"xuanji-native-host-smoke-{suffix}"
    volume = f"xuanji-native-host-smoke-{suffix}"
    host_port = _free_port()
    dview_port = _free_port()
    dview_log = tempfile.TemporaryFile(mode="w+")
    dview_env = dict(os.environ)
    dview_env["XUANJI_SMOKE_DVIEW_PORT"] = str(dview_port)
    dview = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "container_dview_stub.py")],
        cwd=ROOT,
        env=dview_env,
        stdout=dview_log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_port("127.0.0.1", dview_port, dview)
        _run(["docker", "build", "-t", image, "."])
        _run(["docker", "volume", "create", volume])
        _start_container(container, volume, image, host_port, dview_port)
        url = f"http://127.0.0.1:{host_port}/mcp"
        _wait_for_unauthorized(url, container)
        _assert_non_root_and_writable(container)
        asyncio.run(exercise_task(url, HOST_TOKEN, resumed=False))
        _assert_persisted_artifacts(container)

        _run(["docker", "stop", container])
        _run(["docker", "rm", container])
        _start_container(container, volume, image, host_port, dview_port)
        _wait_for_unauthorized(url, container)
        _assert_persisted_artifacts(container)
        asyncio.run(exercise_task(url, HOST_TOKEN, resumed=True))
    except Exception:
        _print_container_logs(container)
        dview_log.seek(0)
        print(dview_log.read(), file=sys.stderr)
        raise
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        dview.terminate()
        try:
            dview.wait(timeout=5)
        except subprocess.TimeoutExpired:
            dview.kill()
            dview.wait(timeout=5)
        dview_log.close()


def _start_container(
    container: str,
    volume: str,
    image: str,
    host_port: int,
    dview_port: int,
) -> None:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"127.0.0.1:{host_port}:8091",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/xuanji",
            "-e",
            f"XUANJI_HOST_PUBLIC_URL=http://127.0.0.1:{host_port}",
            "-e",
            "XUANJI_HOST=0.0.0.0",
            "-e",
            f"XUANJI_HOST_BEARER_TOKEN={HOST_TOKEN}",
            "-e",
            f"XUANJI_DVIEW_MCP_URL=http://host.docker.internal:{dview_port}/mcp/query",
            "-e",
            f"XUANJI_DVIEW_BEARER_TOKEN={'d' * 32}",
            "-e",
            "XUANJI_RECEIPT_KEY_ID=container-smoke",
            "-e",
            f"XUANJI_RECEIPT_SECRET={'r' * 32}",
            image,
        ]
    )


def _assert_non_root_and_writable(container: str) -> None:
    configured_user = _run(
        ["docker", "inspect", "--format={{.Config.User}}", container]
    ).stdout.strip()
    runtime_uid = _run(["docker", "exec", container, "id", "-u"]).stdout.strip()
    if configured_user in {"", "0", "root"} or runtime_uid == "0":
        raise RuntimeError("container is running as root")
    script = (
        "from pathlib import Path; "
        "p=Path('/var/lib/xuanji/runs/container-smoke-marker'); "
        "p.mkdir(parents=True, exist_ok=True); "
        "(p/'persisted').write_text('ok', encoding='utf-8')"
    )
    _run(["docker", "exec", container, "python", "-c", script])


def _assert_persisted_artifacts(container: str) -> None:
    script = (
        "from pathlib import Path; "
        "paths=["
        "Path('/var/lib/xuanji/runs/container-smoke-marker/persisted'),"
        f"Path('/var/lib/xuanji/tasks/{TASK_ID}/state.json'),"
        f"Path('/var/lib/xuanji/results/tasks/{TASK_ID}/validated-task-result.json')"
        "]; "
        "missing=[str(p) for p in paths if not p.is_file()]; "
        "assert not missing, missing"
    )
    _run(["docker", "exec", container, "python", "-c", script])


def _wait_for_unauthorized(url: str, container: str) -> None:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = asyncio.run(unauthenticated_status(url))
            if status == 401:
                return
            last_error = RuntimeError(f"unexpected unauthenticated status: {status}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if _run(["docker", "inspect", "--format={{.State.Running}}", container]).stdout.strip() != "true":
            raise RuntimeError("container exited before MCP became ready")
        time.sleep(0.25)
    raise RuntimeError("container MCP did not return 401") from last_error


def _wait_for_port(host: str, port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("DView smoke stub exited before becoming ready")
        with socket.socket() as connection:
            connection.settimeout(0.25)
            if connection.connect_ex((host, port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("DView smoke stub did not become ready")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _print_container_logs(container: str) -> None:
    result = subprocess.run(
        ["docker", "logs", container],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(result.stdout, file=sys.stderr)


if __name__ == "__main__":
    main()
