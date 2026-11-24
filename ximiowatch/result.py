"""Result of a single monitor execution."""

from __future__ import annotations

from dataclasses import dataclass


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

    @property
    def silent_failure(self) -> bool:
        """HTTP succeeded while content/business assertions failed.

        Latency breaches are tracked separately as performance failures and never
        count as silent application failures by themselves.
        """
        return self.http_ok and bool(self.business_failed_types)

    @property
    def healthy(self) -> bool:
        """True only when HTTP, functional and configured performance checks pass."""
        return (
            self.http_ok
            and self.functional_ok
            and (self.performance_ok is not False)
        )
