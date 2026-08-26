from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import canonical_sha256
from .receipts import TrustedReceiptVerifier
from .runner import AttributionRunner, RunnerError


@dataclass(frozen=True)
class HostQueryResponse:
    query_id: str
    receipt_id: str
    raw_result: dict[str, Any] | None = None
    error_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DViewQueryExecutor(Protocol):
    def execute_read_only(self, sql: str) -> HostQueryResponse: ...


class HostDViewAdapter:
    """Executes the issued SQL without exposing SQL copy/paste to the model."""

    def __init__(
        self,
        *,
        runner: AttributionRunner,
        executor: DViewQueryExecutor,
        receipt_signer: TrustedReceiptVerifier,
    ):
        if runner.trusted_receipt_verifier is not receipt_signer:
            raise RunnerError("runner and Host adapter must share one receipt authority")
        self.runner = runner
        self.executor = executor
        self.receipt_signer = receipt_signer

    def execute_current(self, run_id: str) -> dict[str, Any]:
        ticket = self.runner.next_action(run_id)
        if ticket.get("action") != "execute_query":
            return ticket
        response = self.executor.execute_read_only(ticket["rendered_sql"])
        common = {
            "step_id": ticket["step_id"],
            "attempt_no": ticket["attempt_no"],
            "receipt_type": "trusted_host_receipt",
            "receipt_key_id": self.receipt_signer.key_id,
            "receipt_id": response.receipt_id,
            "submitted_sql_sha256": ticket["rendered_sql_sha256"],
            "query_id": response.query_id,
        }
        if response.raw_result is not None:
            event = {
                "event": "query_returned",
                **common,
                "raw_result": response.raw_result,
                "raw_result_sha256": canonical_sha256(response.raw_result),
            }
        else:
            error_fields = (
                response.error_class,
                response.error_code,
                response.error_message,
            )
            if not all(isinstance(value, str) and value.strip() for value in error_fields):
                raise RunnerError(
                    "Host query response must contain raw_result or a complete raw error"
                )
            event = {
                "event": "query_error",
                **common,
                "error_class": response.error_class,
                "error_code": response.error_code,
                "error_message": response.error_message,
            }
        event["receipt_signature"] = self.receipt_signer.sign(run_id, event)
        return self.runner.record(run_id, event)
