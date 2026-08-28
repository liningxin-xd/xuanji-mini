from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

if __package__:
    from .container_mcp_probe import (
        SNAPSHOT_TASK_ID,
        TASK_ID,
        assert_profile_mismatch_rejected,
        exercise_snapshot_task,
        exercise_task,
        unauthenticated_status,
    )
else:
    from container_mcp_probe import (
        SNAPSHOT_TASK_ID,
        TASK_ID,
        assert_profile_mismatch_rejected,
        exercise_snapshot_task,
        exercise_task,
        unauthenticated_status,
    )


ROOT = Path(__file__).resolve().parents[1]
HOST_TOKEN = "host-smoke-token-" + "h" * 32


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--analysis-profile",
        choices=("primary_v1", "primary_v2"),
        default="primary_v1",
    )
    args = parser.parse_args(argv)
    analysis_profile = args.analysis_profile
    suffix = uuid.uuid4().hex[:12]
    image = f"xuanji-native-host-smoke-{analysis_profile}:{suffix}"
    container = f"xuanji-native-host-smoke-{analysis_profile}-{suffix}"
    volume = f"xuanji-native-host-smoke-{analysis_profile}-{suffix}"
    host_port = 8092 if analysis_profile == "primary_v2" else _free_port()
    if analysis_profile == "primary_v2":
        _require_free_port(host_port)
    dview_port = _free_port()
    dview_log = tempfile.TemporaryFile(mode="w+")
    count_fd, count_name = tempfile.mkstemp(prefix="xuanji-root-query-count-")
    os.close(count_fd)
    count_path = Path(count_name)
    count_path.write_text(
        json.dumps({"root": 0, "primary": 0, "post_primary": 0}),
        encoding="utf-8",
    )
    dview_env = dict(os.environ)
    dview_env["XUANJI_SMOKE_DVIEW_PORT"] = str(dview_port)
    dview_env["XUANJI_SMOKE_DVIEW_COUNT_FILE"] = str(count_path)
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
        _start_container(
            container,
            volume,
            image,
            host_port,
            dview_port,
            analysis_profile,
        )
        url = f"http://127.0.0.1:{host_port}/mcp"
        _wait_for_unauthorized(url, container)
        _assert_non_root_and_writable(container)
        first_task = asyncio.run(exercise_task(url, HOST_TOKEN, resumed=False))
        asyncio.run(exercise_snapshot_task(url, HOST_TOKEN, resumed=False))
        _assert_dview_query_counts(
            count_path, {"root": 8, "primary": 7, "post_primary": 0}
        )
        _assert_persisted_artifacts(container, analysis_profile)

        _run(["docker", "stop", container])
        _run(["docker", "rm", container])
        if analysis_profile == "primary_v2":
            _start_container(
                container,
                volume,
                image,
                host_port,
                dview_port,
                "primary_v1",
            )
            _wait_for_unauthorized(url, container)
            asyncio.run(assert_profile_mismatch_rejected(url, HOST_TOKEN))
            _run(["docker", "stop", container])
            _run(["docker", "rm", container])
        _start_container(
            container,
            volume,
            image,
            host_port,
            dview_port,
            analysis_profile,
        )
        _wait_for_unauthorized(url, container)
        _assert_persisted_artifacts(container, analysis_profile)
        resumed_task = asyncio.run(exercise_task(url, HOST_TOKEN, resumed=True))
        if (
            resumed_task.get("analysis_preview") != first_task.get("analysis_preview")
            or resumed_task.get("pipeline_handoff")
            != first_task.get("pipeline_handoff")
        ):
            raise RuntimeError("signed pipeline handoff changed across restart")
        asyncio.run(exercise_snapshot_task(url, HOST_TOKEN, resumed=True))
        _assert_dview_query_counts(
            count_path, {"root": 8, "primary": 7, "post_primary": 0}
        )
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
        count_path.unlink(missing_ok=True)


def _start_container(
    container: str,
    volume: str,
    image: str,
    host_port: int,
    dview_port: int,
    analysis_profile: str,
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
            f"XUANJI_ANALYSIS_PROFILE={analysis_profile}",
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
    runtime_cwd = _run(["docker", "exec", container, "pwd"]).stdout.strip()
    if (
        configured_user != "xuanji"
        or runtime_uid != "10001"
        or runtime_cwd != "/app"
    ):
        raise RuntimeError("container is not running as the fixed xuanji UID")
    script = (
        "from pathlib import Path; "
        "p=Path('/var/lib/xuanji/runs/container-smoke-marker'); "
        "p.mkdir(parents=True, exist_ok=True); "
        "(p/'persisted').write_text('ok', encoding='utf-8')"
    )
    _run(["docker", "exec", container, "python", "-c", script])


def _assert_persisted_artifacts(container: str, analysis_profile: str) -> None:
    script = (
        "from pathlib import Path; "
        "paths=["
        "Path('/var/lib/xuanji/runs/container-smoke-marker/persisted'),"
        f"Path('/var/lib/xuanji/tasks/{TASK_ID}/state.json'),"
        f"Path('/var/lib/xuanji/tasks/{SNAPSHOT_TASK_ID}/state.json'),"
        f"Path('/var/lib/xuanji/results/tasks/{TASK_ID}/validated-task-result.json')"
        "]; "
        "missing=[str(p) for p in paths if not p.is_file()]; "
        "assert not missing, missing; "
        f"snapshots=list(Path('/var/lib/xuanji/tasks/{SNAPSHOT_TASK_ID}/root-snapshots').glob('*.json')); "
        "assert len(snapshots) == 1, snapshots; "
        f"state=__import__('json').loads(Path('/var/lib/xuanji/tasks/{SNAPSHOT_TASK_ID}/state.json').read_text()); "
        f"assert state['analysis_profile'] == '{analysis_profile}', state['analysis_profile']; "
        "runs=[item for item in state['investigations'] if item.get('run_id')]; "
        "assert len(runs) == 1, runs; "
        "run=__import__('json').loads(Path('/var/lib/xuanji/runs', runs[0]['run_id'], 'state.json').read_text()); "
        f"assert run['analysis_profile'] == '{analysis_profile}', run['analysis_profile']; "
        + (
            "post=run.get('post_primary'); "
            "assert isinstance(post, dict) and post.get('status') == 'completed', post; "
            "assert [step.get('id') for step in post['steps']] == ['counterfactual','secondary','game_background','breadth_check','error_code','cross_dimension_overlap']; "
            "assert all(step.get('status') in {'succeeded','failed','skipped_by_policy'} for step in post['steps'])"
            if analysis_profile == "primary_v2"
            else "assert run.get('post_primary') is None"
        )
    )
    _run(["docker", "exec", container, "python", "-c", script])


def _assert_dview_query_counts(path: Path, expected: dict[str, int]) -> None:
    actual = json.loads(path.read_text(encoding="utf-8"))
    if actual != expected:
        raise RuntimeError(
            f"expected query counts {expected} across restart, received {actual}"
        )


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


def _require_free_port(port: int) -> None:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"required v2 shadow port is unavailable: {port}") from exc


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
