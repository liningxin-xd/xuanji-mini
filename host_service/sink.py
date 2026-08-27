from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class FileValidatedResultSink:
    """Persist authoritative results outside every model-facing response."""

    def __init__(self, results_root: Path | str):
        self.results_root = Path(results_root)

    def __call__(
        self,
        run_id: str,
        analysis: dict[str, Any],
        validation_receipt: dict[str, Any],
    ) -> None:
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("run_id is not safe for Host artifact storage")
        payload = {
            "run_id": run_id,
            "analysis": analysis,
            "validation_receipt": validation_receipt,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        run_root = self.results_root / run_id
        run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = run_root / "validated-result.json"
        if target.exists():
            if target.read_bytes() != encoded:
                raise RuntimeError("validated result sink rejected conflicting content")
            return

        temporary = run_root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, run_id: str) -> dict[str, Any]:
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("run_id is not safe for Host artifact storage")
        target = self.results_root / run_id / "validated-result.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("validated result sink artifact cannot be loaded") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise RuntimeError("validated result sink artifact is invalid")
        return payload


class FileTaskResultSink:
    """Persist the complete multi-investigation task result for machine consumers."""

    def __init__(self, results_root: Path | str):
        self.results_root = Path(results_root)

    def __call__(
        self,
        task_id: str,
        analysis: dict[str, Any],
        validation_receipt: dict[str, Any],
    ) -> None:
        if _RUN_ID.fullmatch(task_id) is None:
            raise ValueError("task_id is not safe for Host artifact storage")
        payload = {
            "task_id": task_id,
            "analysis": analysis,
            "validation_receipt": validation_receipt,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        task_root = self.results_root / task_id
        task_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = task_root / "validated-task-result.json"
        if target.exists():
            if target.read_bytes() != encoded:
                raise RuntimeError("task result sink rejected conflicting content")
            return
        temporary = task_root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, task_id: str) -> dict[str, Any]:
        if _RUN_ID.fullmatch(task_id) is None:
            raise ValueError("task_id is not safe for Host artifact storage")
        target = self.results_root / task_id / "validated-task-result.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("task result sink artifact cannot be loaded") from exc
        if not isinstance(payload, dict) or payload.get("task_id") != task_id:
            raise RuntimeError("task result sink artifact is invalid")
        return payload
