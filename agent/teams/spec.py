"""Team and harness spec strings.

::

    solo:worker=H
    review:coder=H,reviewer=H[,rounds=N]        # rounds defaults to 3
    best:workers=H+H[+H...][,judge=H]           # judge defaults to the first worker's harness
    plan:planner=H,workers=H[+H...][,integrator=H][,max_tasks=N][,replans=N][,reviewer=H][,rounds=N]
                                                # integrator defaults to the first worker's harness,
                                                # max_tasks to 4, replans to 1 (0 = never re-plan);
                                                # reviewer= reviews every subtask (not ``check``);
                                                # rounds= (default 3) needs reviewer=
    H := harness[@model]                        # harness in {claude, codex, sdk}
    reviewer= and judge= (only) may also be ``check``: the --check command as reviewer/judge; the
    reviewer= of a plan team may not (the check runs on the integrated patch, see --check)

The kind is split off at the first ``:`` only; model ids may contain ``/``, ``:``, ``.`` and ``-``
(but not ``,`` or ``+``). ``Team.name`` is the canonical form, which spells out every default, so
``build_team(team.name).name == team.name``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .budget import Budget, BudgetedHarness
from .harnesses import CheckHarness, ClaudeHarness, CodexHarness, SdkHarness
from .harnesses.check import Check
from .harnesses.sdk import LITELLM_PREFIX
from .harnesses.base import Permissions
from .pricing import lookup_price
from .plan import DEFAULT_MAX_TASKS, DEFAULT_REPLANS, PlanTeam
from .topologies import DEFAULT_ROUNDS, BestTeam, ReviewTeam, SoloTeam
from .types import Harness, Team
from .validation import validate_positive_finite

HARNESSES = ("claude", "codex", "sdk")
CHECK = "check"  # pseudo-harness: the --check command acting as reviewer or judge

_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # kind -> (required keys, optional keys)
    "solo": (("worker",), ()),
    "review": (("coder", "reviewer"), ("rounds",)),
    "best": (("workers",), ("judge",)),
    "plan": (("planner", "workers"), ("integrator", "max_tasks", "replans", "reviewer", "rounds")),
}


class SpecError(ValueError):
    """An invalid team or harness spec; the message says what is wrong and what is allowed."""


@dataclass(frozen=True)
class HarnessSpec:
    harness: str
    model: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.harness}@{self.model}" if self.model else self.harness


def parse_harness_spec(spec: str) -> HarnessSpec:
    text = spec.strip()
    name, sep, model = text.partition("@")
    name = name.strip().lower()
    if name == CHECK:
        if sep:
            raise SpecError(f"{CHECK!r} takes no model, got {spec!r}")
        return HarnessSpec(CHECK)
    if name not in HARNESSES:
        raise SpecError(
            f"unknown harness {name!r} in {spec!r}; expected one of {', '.join(HARNESSES)} "
            f"(optionally followed by @model), or {CHECK!r} as reviewer/judge"
        )
    model = model.strip()
    if sep and not model:
        raise SpecError(f"empty model after '@' in harness spec {spec!r}")
    return HarnessSpec(name, model or None)


def _coding_harness(value: str, key: str, spec: str) -> HarnessSpec:
    parsed = parse_harness_spec(value)
    if parsed.harness == CHECK:
        raise SpecError(f"{CHECK!r} cannot be the {key} in {spec!r}; it is valid only as reviewer= or judge=")
    return parsed


def _rounds(fields: dict[str, str], spec: str) -> int:
    """``rounds`` from the parsed fields: a positive integer, ``DEFAULT_ROUNDS`` when absent."""
    if "rounds" not in fields:
        return DEFAULT_ROUNDS
    try:
        rounds = int(fields["rounds"])
    except ValueError:
        rounds = 0
    if rounds < 1:
        raise SpecError(f"rounds must be a positive integer, got {fields['rounds']!r} in {spec!r}")
    return rounds


@dataclass(frozen=True)
class TeamSpec:
    kind: str
    worker: Optional[HarnessSpec] = None
    coder: Optional[HarnessSpec] = None
    reviewer: Optional[HarnessSpec] = None
    rounds: int = DEFAULT_ROUNDS
    workers: tuple[HarnessSpec, ...] = ()
    judge: Optional[HarnessSpec] = None
    planner: Optional[HarnessSpec] = None
    integrator: Optional[HarnessSpec] = None
    max_tasks: int = DEFAULT_MAX_TASKS
    replans: int = DEFAULT_REPLANS

    @property
    def harness_specs(self) -> tuple[HarnessSpec, ...]:
        singles = (self.worker, self.coder, self.reviewer, self.judge, self.planner, self.integrator)
        return (*(h for h in singles if h is not None), *self.workers)

    @property
    def canonical(self) -> str:
        if self.kind == "solo":
            return f"solo:worker={self.worker}"
        if self.kind == "review":
            return f"review:coder={self.coder},reviewer={self.reviewer},rounds={self.rounds}"
        if self.kind == "plan":
            review = f",reviewer={self.reviewer},rounds={self.rounds}" if self.reviewer is not None else ""
            return (
                f"plan:planner={self.planner},workers={'+'.join(map(str, self.workers))},"
                f"integrator={self.integrator},max_tasks={self.max_tasks},replans={self.replans}{review}"
            )
        return f"best:workers={'+'.join(map(str, self.workers))},judge={self.judge}"


def _parse_fields(kind: str, body: str, spec: str) -> dict[str, str]:
    required, optional = _KEYS[kind]
    fields: dict[str, str] = {}
    for part in body.split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise SpecError(f"malformed field {part.strip()!r} in {spec!r}; expected key=value")
        if key not in required + optional:
            raise SpecError(
                f"unknown key {key!r} for {kind!r} team in {spec!r}; "
                f"allowed: {', '.join(required + optional)}"
            )
        if key in fields:
            raise SpecError(f"duplicate key {key!r} in {spec!r}")
        fields[key] = value
    missing = [k for k in required if k not in fields]
    if missing:
        raise SpecError(f"missing required key(s) {', '.join(missing)} for {kind!r} team in {spec!r}")
    return fields


def parse_team_spec(spec: str) -> TeamSpec:
    text = spec.strip()
    kind, sep, body = text.partition(":")
    kind = kind.strip().lower()
    if not sep:
        raise SpecError(
            f"invalid team spec {spec!r}: expected '<kind>:<key>=<value>,...' with kind in "
            f"{', '.join(_KEYS)}"
        )
    if kind not in _KEYS:
        raise SpecError(f"unknown team kind {kind!r} in {spec!r}; expected one of {', '.join(_KEYS)}")
    fields = _parse_fields(kind, body, text)
    if kind == "solo":
        return TeamSpec("solo", worker=_coding_harness(fields["worker"], "worker", text))
    if kind == "review":
        rounds = _rounds(fields, spec)
        return TeamSpec(
            "review",
            coder=_coding_harness(fields["coder"], "coder", text),
            reviewer=parse_harness_spec(fields["reviewer"]),
            rounds=rounds,
        )
    if kind == "plan":
        planner = _coding_harness(fields["planner"], "planner", text)
        workers = tuple(_coding_harness(w, "worker", text) for w in fields["workers"].split("+"))
        integrator = (
            _coding_harness(fields["integrator"], "integrator", text) if "integrator" in fields else workers[0]
        )
        max_tasks = DEFAULT_MAX_TASKS
        if "max_tasks" in fields:
            try:
                max_tasks = int(fields["max_tasks"])
            except ValueError:
                max_tasks = 0
            if max_tasks < 1:
                raise SpecError(
                    f"max_tasks must be a positive integer, got {fields['max_tasks']!r} in {spec!r}"
                )
        replans = DEFAULT_REPLANS
        if "replans" in fields:
            try:
                replans = int(fields["replans"])
            except ValueError:
                replans = -1
            if replans < 0:
                raise SpecError(
                    f"replans must be a non-negative integer, got {fields['replans']!r} in {spec!r}"
                )
        reviewer = None
        if "reviewer" in fields:
            reviewer = parse_harness_spec(fields["reviewer"])
            if reviewer.harness == CHECK:
                raise SpecError(
                    f"{CHECK!r} cannot be the reviewer of a plan team in {spec!r}: it would run on the "
                    "integrated patch, not on each subtask; pass the command with --check instead"
                )
        if "rounds" in fields and reviewer is None:
            raise SpecError(f"rounds is only valid together with reviewer in {spec!r}")
        return TeamSpec(
            "plan",
            workers=workers,
            planner=planner,
            integrator=integrator,
            max_tasks=max_tasks,
            replans=replans,
            reviewer=reviewer,
            rounds=_rounds(fields, spec),
        )
    workers = tuple(_coding_harness(w, "worker", text) for w in fields["workers"].split("+"))
    if len(workers) < 2:
        raise SpecError(f"best needs at least two workers joined with '+' (H+H), got {fields['workers']!r}")
    judge = parse_harness_spec(fields["judge"]) if "judge" in fields else workers[0]
    return TeamSpec("best", workers=workers, judge=judge)


def is_priced(spec: str) -> bool:
    """Whether runs of harness ``spec`` can be expected to report a dollar cost.

    claude: always (the CLI reports ``total_cost_usd``). codex and sdk@<openai-model>: only for an
    explicit model listed in ``pricing.py`` (codex without ``@model`` resolves its model at run
    time, so it is not assumed priced). sdk@litellm/...: only if litellm is installed and knows
    a price for the model. A False answer means cost would be None; it is a preflight guide, the run's
    ``usage.cost_usd`` is authoritative.
    """
    parsed = parse_harness_spec(spec)
    if parsed.harness == CHECK:
        return True  # a shell command: no tokens, cost is exactly 0
    if parsed.harness == "claude":
        return True
    if parsed.model is None:
        return False
    if parsed.harness == "sdk" and parsed.model.startswith(LITELLM_PREFIX):
        name = parsed.model[len(LITELLM_PREFIX):]
        try:
            import litellm

            litellm.cost_per_token(model=name, prompt_tokens=1, completion_tokens=1)
            return True
        except Exception:
            return False
    return lookup_price(parsed.model) is not None


def build_harness(
    spec: str,
    *,
    permissions: Permissions = "safe",
    env: Optional[Mapping[str, str]] = None,
    check: Optional[str] = None,
) -> Harness:
    """Harness from ``harness[@model]``.

    ``permissions="bypass"`` removes the CLI's own permission checks and sandbox; use it only on
    workspaces that are themselves sandboxed. ``env`` is merged into every command the harness
    runs through ``Workspace.exec`` (e.g. ``IS_SANDBOX=1`` for Claude Code as root). ``check`` is
    the shell command the ``check`` pseudo-harness runs; it is required for that harness.
    """
    parsed = parse_harness_spec(spec)
    env_dict = dict(env or {})
    if parsed.harness == CHECK:
        if check is None:
            raise SpecError(f"{spec!r} needs a check command (--check CMD)")
        return CheckHarness(check)
    if parsed.harness == "claude":
        return ClaudeHarness(model=parsed.model, permissions=permissions, env=env_dict)
    if parsed.harness == "codex":
        return CodexHarness(model=parsed.model, permissions=permissions, env=env_dict)
    return SdkHarness(model=parsed.model, env=env_dict)


def build_team(
    spec: str,
    *,
    permissions: Permissions = "safe",
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    check: Optional[str] = None,
    budget: Optional[Budget] = None,
) -> Team:
    """Team from a spec string.

    ``permissions``/``env`` apply to every harness (see ``build_harness``); ``timeout`` bounds
    each individual harness run (and the check command) in seconds. ``check`` is a shell command
    run in a fork of the workspace with the team's final patch applied; its result is recorded in
    ``TeamResult.meta["check"]``. It is also what the ``check`` harness runs as ``reviewer=check``
    or ``judge=check``, which is an error without it. ``budget`` caps the team's spend: every
    harness except ``check`` (a shell command that costs $0) is wrapped in a ``BudgetedHarness``
    sharing it, so runs are refused with ``BudgetExceeded`` once it is used up.
    """
    if timeout is not None:
        validate_positive_finite(timeout, "timeout")
    parsed = parse_team_spec(spec)
    if check is not None and not check.strip():
        raise SpecError("the check command is empty")
    checker = Check(check, timeout) if check is not None else None

    def make(h: HarnessSpec) -> Harness:
        harness = build_harness(str(h), permissions=permissions, env=env, check=check)
        return harness if budget is None or isinstance(harness, CheckHarness) else BudgetedHarness(harness, budget)

    if parsed.kind == "solo":
        assert parsed.worker is not None
        return SoloTeam(make(parsed.worker), name=parsed.canonical, timeout=timeout, check=checker)
    if parsed.kind == "review":
        assert parsed.coder is not None and parsed.reviewer is not None
        return ReviewTeam(
            make(parsed.coder),
            make(parsed.reviewer),
            rounds=parsed.rounds,
            name=parsed.canonical,
            timeout=timeout,
            check=checker,
        )
    if parsed.kind == "plan":
        assert parsed.planner is not None and parsed.integrator is not None
        return PlanTeam(
            make(parsed.planner),
            [make(w) for w in parsed.workers],
            make(parsed.integrator),
            max_tasks=parsed.max_tasks,
            replans=parsed.replans,
            reviewer=make(parsed.reviewer) if parsed.reviewer is not None else None,
            rounds=parsed.rounds,
            name=parsed.canonical,
            timeout=timeout,
            check=checker,
        )
    assert parsed.judge is not None
    return BestTeam(
        [make(w) for w in parsed.workers],
        make(parsed.judge),
        name=parsed.canonical,
        timeout=timeout,
        check=checker,
    )
