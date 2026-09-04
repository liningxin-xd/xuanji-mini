from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class QueryObservation:
    stage: str
    step_id: str
    attempt_no: int
    run_id: str | None


_CURRENT_QUERY: ContextVar[QueryObservation | None] = ContextVar(
    "xuanji_query_observation", default=None
)


def current_query_observation() -> QueryObservation | None:
    return _CURRENT_QUERY.get()


@contextmanager
def observe_query(
    *,
    stage: str,
    step_id: str,
    attempt_no: int = 0,
    run_id: str | None = None,
) -> Iterator[QueryObservation]:
    observation = QueryObservation(
        stage=stage,
        step_id=step_id,
        attempt_no=attempt_no,
        run_id=run_id,
    )
    token = _CURRENT_QUERY.set(observation)
    try:
        yield observation
    finally:
        _CURRENT_QUERY.reset(token)
