"""Result of a single monitor execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HealthState = Literal["up", "down", "unknown", "not_configured"]


@dataclass(frozen=True)
class CheckResult:
    monitor: str
    application: str
    status_code: int | None
    elapsed_seconds: float
    http_ok: bool
    functional_ok: bool
    failed_types: tuple[str, ...]
    outcome: str
    error_type: str | None = None
    business_failed_types: tuple[str, ...] = ()
    performance_configured: bool = False
    performance_ok: bool | None = None
    consecutive_failures: int = 0
    functional_configured: bool = False
    functional_evaluated: bool = False
    functional_state: HealthState = "not_configured"
    performance_evaluated: bool = False
    performance_state: HealthState = "not_configured"

    @property
    def http_state(self) -> HealthState:
        return "up" if self.http_ok else "down"

    @property
    def silent_failure(self) -> bool:
        """HTTP up plus a confirmed business/content failure.

        Latency breaches, skipped assertions and unavailable observations never
        count as silent application failures.
        """
        return self.http_ok and self.functional_state == "down"

    @property
    def healthy(self) -> bool:
        """True only when every configured applicable dimension is UP."""
        return (
            self.outcome == "success"
            and self.http_ok
            and self.functional_state in {"up", "not_configured"}
            and self.performance_state in {"up", "not_configured"}
        )
