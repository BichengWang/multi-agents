"""Team and harness spec strings.

::

    solo:worker=H
    review:coder=H,reviewer=H[,rounds=N]        # rounds defaults to 3
    best:workers=H+H[+H...][,judge=H]           # judge defaults to the first worker's harness
    H := harness[@model]                        # harness in {claude, codex, sdk}

The kind is split off at the first ``:`` only; model ids may contain ``/``, ``:``, ``.`` and ``-``
(but not ``,`` or ``+``). ``Team.name`` is the canonical form, which spells out every default, so
``build_team(team.name).name == team.name``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .harnesses import ClaudeHarness, CodexHarness, SdkHarness
from .harnesses.sdk import LITELLM_PREFIX
from .harnesses.base import Permissions
from .pricing import lookup_price
from .topologies import BestTeam, ReviewTeam, SoloTeam
from .types import Harness, Team

HARNESSES = ("claude", "codex", "sdk")
DEFAULT_ROUNDS = 3

_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # kind -> (required keys, optional keys)
    "solo": (("worker",), ()),
    "review": (("coder", "reviewer"), ("rounds",)),
    "best": (("workers",), ("judge",)),
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
    if name not in HARNESSES:
        raise SpecError(
            f"unknown harness {name!r} in {spec!r}; expected one of {', '.join(HARNESSES)} "
            "(optionally followed by @model)"
        )
    model = model.strip()
    if sep and not model:
        raise SpecError(f"empty model after '@' in harness spec {spec!r}")
    return HarnessSpec(name, model or None)


@dataclass(frozen=True)
class TeamSpec:
    kind: str
    worker: Optional[HarnessSpec] = None
    coder: Optional[HarnessSpec] = None
    reviewer: Optional[HarnessSpec] = None
    rounds: int = DEFAULT_ROUNDS
    workers: tuple[HarnessSpec, ...] = ()
    judge: Optional[HarnessSpec] = None

    @property
    def harness_specs(self) -> tuple[HarnessSpec, ...]:
        singles = (self.worker, self.coder, self.reviewer, self.judge)
        return (*(h for h in singles if h is not None), *self.workers)

    @property
    def canonical(self) -> str:
        if self.kind == "solo":
            return f"solo:worker={self.worker}"
        if self.kind == "review":
            return f"review:coder={self.coder},reviewer={self.reviewer},rounds={self.rounds}"
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
        return TeamSpec("solo", worker=parse_harness_spec(fields["worker"]))
    if kind == "review":
        rounds = DEFAULT_ROUNDS
        if "rounds" in fields:
            try:
                rounds = int(fields["rounds"])
            except ValueError:
                rounds = 0
            if rounds < 1:
                raise SpecError(f"rounds must be a positive integer, got {fields['rounds']!r} in {spec!r}")
        return TeamSpec(
            "review",
            coder=parse_harness_spec(fields["coder"]),
            reviewer=parse_harness_spec(fields["reviewer"]),
            rounds=rounds,
        )
    workers = tuple(parse_harness_spec(w) for w in fields["workers"].split("+"))
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
    spec: str, *, permissions: Permissions = "safe", env: Optional[Mapping[str, str]] = None
) -> Harness:
    """Harness from ``harness[@model]``.

    ``permissions="bypass"`` removes the CLI's own permission checks and sandbox; use it only on
    workspaces that are themselves sandboxed. ``env`` is merged into every command the harness
    runs through ``Workspace.exec`` (e.g. ``IS_SANDBOX=1`` for Claude Code as root).
    """
    parsed = parse_harness_spec(spec)
    env_dict = dict(env or {})
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
) -> Team:
    """Team from a spec string.

    ``permissions``/``env`` apply to every harness (see ``build_harness``); ``timeout`` bounds
    each individual harness run in seconds.
    """
    parsed = parse_team_spec(spec)

    def make(h: HarnessSpec) -> Harness:
        return build_harness(str(h), permissions=permissions, env=env)

    if parsed.kind == "solo":
        assert parsed.worker is not None
        return SoloTeam(make(parsed.worker), name=parsed.canonical, timeout=timeout)
    if parsed.kind == "review":
        assert parsed.coder is not None and parsed.reviewer is not None
        return ReviewTeam(
            make(parsed.coder),
            make(parsed.reviewer),
            rounds=parsed.rounds,
            name=parsed.canonical,
            timeout=timeout,
        )
    assert parsed.judge is not None
    return BestTeam(
        [make(w) for w in parsed.workers], make(parsed.judge), name=parsed.canonical, timeout=timeout
    )
