"""Run budget: a hard stop on how much a team may spend.

A ``Budget`` is shared by every harness of one run (``build_team(spec, budget=...)`` wraps each of
them in a ``BudgetedHarness``). Before a harness run starts, ``admit`` checks the books; after it
returns, ``charge`` adds its cost. Once the limit is reached no new run is admitted: the refusal is
a ``BudgetExceeded`` raised from ``Harness.run``, which the topologies record in
``meta["failures"]`` like any other failed run (a refused run never reaches ``TeamResult.runs``).

Limits of the guarantee, stated honestly:

* Runs already in flight when the limit is reached finish: a harness run cannot be stopped midway
  without losing its work, and its cost is only known afterwards. Spend can therefore exceed the
  limit by the cost of the runs admitted before it was reached (at most the team's concurrency,
  e.g. the workers of ``best`` or ``plan``, plus the run that crossed it).
* An unpriced run (``Usage.cost_usd is None``) cannot be counted, so cost can no longer be
  enforced after one. By default that blocks every further run; ``allow_unpriced=True`` counts
  such runs as $0 instead, and the limit then only covers the priced part of the spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import Harness, HarnessRun, Workspace


class BudgetExceeded(RuntimeError):
    """A harness run was refused because the run budget is used up (or can no longer be enforced)."""


@dataclass
class Budget:
    """Spend limit in USD for one team run; see the module docstring for what it guarantees.

    ``warn_at`` is the fraction of ``max_cost_usd`` at which ``on_warn(budget)`` is called, once.
    The state fields are updated as runs are admitted and charged; ``to_dict`` snapshots them for
    run records.
    """

    max_cost_usd: float
    allow_unpriced: bool = False
    warn_at: float = 0.8
    on_warn: Optional[Callable[["Budget"], None]] = None
    spent_usd: float = 0.0
    unpriced_runs: int = 0
    refused: list[dict[str, Any]] = field(default_factory=list)
    warned: bool = False

    def __post_init__(self) -> None:
        if not self.max_cost_usd > 0:
            raise ValueError(f"max_cost_usd must be positive, got {self.max_cost_usd!r}")

    @property
    def exhausted(self) -> bool:
        """Whether no further run would be admitted."""
        return self.spent_usd >= self.max_cost_usd or (self.unpriced_runs > 0 and not self.allow_unpriced)

    def admit(self, harness: str, role: str) -> None:
        """Let a run of ``harness`` as ``role`` start, or raise ``BudgetExceeded`` and record it."""
        if self.spent_usd >= self.max_cost_usd:
            reason = f"budget of ${self.max_cost_usd:.4f} used up (${self.spent_usd:.4f} spent)"
        elif self.unpriced_runs > 0 and not self.allow_unpriced:
            reason = (
                f"{self.unpriced_runs} unpriced run(s) so far: the cost can no longer be enforced "
                f"against the budget of ${self.max_cost_usd:.4f}"
            )
        else:
            return
        self.refused.append({"role": role, "harness": harness, "reason": reason})
        raise BudgetExceeded(f"{harness} ({role}) not started: {reason}")

    def charge(self, run: HarnessRun) -> None:
        """Account for a finished run; fires ``on_warn`` the first time ``warn_at`` is crossed."""
        cost = run.usage.cost_usd
        if cost is None:
            self.unpriced_runs += 1
        else:
            self.spent_usd += cost
        if not self.warned and self.spent_usd >= self.warn_at * self.max_cost_usd:
            self.warned = True
            if self.on_warn is not None:
                self.on_warn(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost_usd": self.max_cost_usd,
            "spent_usd": self.spent_usd,
            "unpriced_runs": self.unpriced_runs,
            "warned": self.warned,
            "exhausted": self.exhausted,
            "refused": list(self.refused),
        }


class BudgetedHarness:
    """A harness whose runs are admitted and charged by a ``Budget``.

    Everything else is forwarded unchanged: ``name``, ``model``, ``supports_output_schema`` and
    every ``run`` keyword. A run that returns is charged even when its exit code is non-zero (it
    still cost money); an exception from the inner harness propagates and charges nothing.
    """

    def __init__(self, inner: Harness, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> Optional[str]:
        return self.inner.model

    @property
    def supports_output_schema(self) -> bool:
        return getattr(self.inner, "supports_output_schema", False)

    async def run(self, workspace: Workspace, prompt: str, **kwargs: Any) -> HarnessRun:
        label = f"{self.name}@{self.model}" if self.model else self.name
        self.budget.admit(label, kwargs.get("role", "worker"))
        run = await self.inner.run(workspace, prompt, **kwargs)
        self.budget.charge(run)
        return run
