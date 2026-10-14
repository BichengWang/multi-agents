"""Fresh-task dataset: SWE-bench-Live instances mapped to ``BenchTask``.

Two public, MIT-licensed Hugging Face datasets are supported, both pinned to an exact revision so a
run is reproducible even though upstream keeps adding tasks:

* ``multilang`` - ``SWE-bench-Live/MultiLang``: C, C++, C#, Go, Java, JS, Rust, TS (one split per
  language, each row carries its own ``docker_image``, rebuild/test/print commands and parser).
* ``python`` - ``SWE-bench-Live/SWE-bench-Live`` (``full`` split): Python only, image name derived
  from the instance id.

Parquet shards are downloaded straight from the Hub (``huggingface_hub`` + ``pyarrow``); no
``datasets`` dependency. A local ``.jsonl`` file of dataset rows (the format the official
evaluation script also accepts) can be passed instead of a dataset key.

Only ``problem_statement`` is ever shown to an agent. Everything needed for grading lives in
``BenchTask.grading`` and is never part of the ``CodingTask`` handed to a team.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from agent.teams.types import CodingTask

ISSUE_PROMPT = """\
Resolve the GitHub issue below in the repository checked out in the current working directory.

Edit the source files directly so the issue is fixed. The project's own tests may be used to check
your work, but do not rely on any test that is not already in the repository, and do not commit.
When you are done, stop; your edits to the working tree are your answer.

# Issue

{problem_statement}
"""


@dataclass(frozen=True)
class DatasetSource:
    key: str
    repo_id: str
    # Exact Hub commit; bump deliberately to pick up newly added tasks.
    revision: str
    # language label -> parquet paths inside the dataset repository
    files: dict[str, tuple[str, ...]]
    license: str = "mit"

    @property
    def url(self) -> str:
        return f"https://huggingface.co/datasets/{self.repo_id}/tree/{self.revision}"


_MULTILANG_REVISION = "3638632e8153a10ca422c1022bed79023084b5c9"  # 2026-09-20
_PYTHON_REVISION = "b51a86422e10cfd403beb4773e5a2947953e36ec"  # 2026-09-04

SOURCES: dict[str, DatasetSource] = {
    "multilang": DatasetSource(
        key="multilang",
        repo_id="SWE-bench-Live/MultiLang",
        revision=_MULTILANG_REVISION,
        files={
            lang: (f"data/{lang}-00000-of-00001.parquet",)
            for lang in ("c", "cpp", "cs", "go", "java", "js", "rust", "ts")
        },
    ),
    "python": DatasetSource(
        key="python",
        repo_id="SWE-bench-Live/SWE-bench-Live",
        revision=_PYTHON_REVISION,
        files={
            "python": (
                "data/full-00000-of-00002.parquet",
                "data/full-00001-of-00002.parquet",
            ),
        },
    ),
}
DEFAULT_DATASET = "multilang"

_META_COLUMNS = ["instance_id", "repo", "created_at"]


@dataclass(frozen=True)
class GradingSpec:
    """Hidden fields used only by ``evaluate``; never shown to an agent."""

    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    # The reference fix; used only by `gold` to check an instance is still valid on this machine.
    gold_patch: str = ""
    rebuild_cmds: tuple[str, ...] = ()
    test_cmds: tuple[str, ...] = ()
    print_cmds: tuple[str, ...] = ()
    # "pytest" selects the built-in pytest parser; anything else is a parser script from the dataset.
    log_parser: str = "pytest"


@dataclass(frozen=True)
class BenchTask(CodingTask):
    repo: str = ""
    base_commit: str = ""
    image: str = ""
    created_at: Optional[datetime] = None
    language: str = ""
    dataset: str = ""
    grading: GradingSpec = field(
        default_factory=lambda: GradingSpec("", (), ()), repr=False, compare=False
    )

    def coding_task(self) -> CodingTask:
        """The agent-visible task: id and instruction only."""
        return CodingTask(task_id=self.task_id, instruction=self.instruction)


def default_image_name(instance_id: str) -> str:
    """Docker Hub image of a SWE-bench-Live instance (official ``get_default_image_name``, linux)."""
    return "starryzhang/sweb.eval.x86_64." + instance_id.replace("__", "_1776_").lower()


def parse_created_at(value: Any) -> datetime:
    """ISO strings (``...Z`` included) and pyarrow timestamps -> timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_datetime(value: Union[str, date, datetime, None]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return parse_created_at(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return parse_created_at(value if "T" in value or " " in value else f"{value}T00:00:00")


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # Some exports store JSON-encoded lists as strings.
        try:
            decoded = json.loads(value)
        except ValueError:
            return (value,)
        return tuple(str(v) for v in decoded) if isinstance(decoded, list) else (value,)
    return tuple(str(v) for v in value)


def row_to_task(row: dict[str, Any], *, language: str, dataset: str) -> BenchTask:
    instance_id = str(row["instance_id"])
    image = row.get("docker_image") or row.get("image_key") or default_image_name(instance_id)
    return BenchTask(
        task_id=instance_id,
        instruction=ISSUE_PROMPT.format(problem_statement=str(row["problem_statement"]).strip()),
        repo=str(row["repo"]),
        base_commit=str(row["base_commit"]),
        image=str(image),
        created_at=parse_created_at(row["created_at"]),
        language=str(row.get("language") or language),
        dataset=dataset,
        grading=GradingSpec(
            test_patch=str(row.get("test_patch") or ""),
            fail_to_pass=_strings(row.get("FAIL_TO_PASS")),
            pass_to_pass=_strings(row.get("PASS_TO_PASS")),
            gold_patch=str(row.get("patch") or ""),
            rebuild_cmds=_strings(row.get("rebuild_cmds")),
            test_cmds=_strings(row.get("test_cmds")),
            print_cmds=_strings(row.get("print_cmds")),
            log_parser=str(row.get("log_parser") or "pytest"),
        ),
    )


@dataclass(frozen=True)
class _Candidate:
    instance_id: str
    repo: str
    created_at: datetime
    language: str
    location: str  # parquet file path or jsonl path


def _require(module: str, extra: str = "bench"):
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            f"{module} is required to read the dataset: uv sync --extra {extra}"
        ) from exc


def _download(source: DatasetSource, filename: str) -> str:
    hub = _require("huggingface_hub")
    return hub.hf_hub_download(
        repo_id=source.repo_id, filename=filename, repo_type="dataset", revision=source.revision
    )


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _candidates(
    source: Union[DatasetSource, Path], languages: Optional[Sequence[str]]
) -> Iterator[_Candidate]:
    wanted = {lang.lower() for lang in languages} if languages else None
    if isinstance(source, Path):
        for row in _read_jsonl(source):
            lang = str(row.get("language") or "unknown")
            if wanted is None or lang.lower() in wanted:
                yield _Candidate(
                    str(row["instance_id"]),
                    str(row["repo"]),
                    parse_created_at(row["created_at"]),
                    lang,
                    str(source),
                )
        return
    pq = _require("pyarrow.parquet")
    for lang, filenames in source.files.items():
        if wanted is not None and lang.lower() not in wanted:
            continue
        for filename in filenames:
            local = _download(source, filename)
            table = pq.read_table(local, columns=_META_COLUMNS)
            for row in table.to_pylist():
                yield _Candidate(
                    str(row["instance_id"]),
                    str(row["repo"]),
                    parse_created_at(row["created_at"]),
                    lang,
                    local,
                )


def _full_rows(
    source: Union[DatasetSource, Path], chosen: Sequence[_Candidate]
) -> Iterator[tuple[_Candidate, dict[str, Any]]]:
    by_location: dict[str, list[_Candidate]] = {}
    for cand in chosen:
        by_location.setdefault(cand.location, []).append(cand)
    for location, cands in by_location.items():
        ids = {c.instance_id: c for c in cands}
        if isinstance(source, Path):
            for row in _read_jsonl(Path(location)):
                if str(row["instance_id"]) in ids:
                    yield ids[str(row["instance_id"])], row
            continue
        pq = _require("pyarrow.parquet")
        table = pq.read_table(location, filters=[("instance_id", "in", sorted(ids))])
        for row in table.to_pylist():
            yield ids[str(row["instance_id"])], row


def load_tasks(
    dataset: str = DEFAULT_DATASET,
    *,
    since: Union[str, date, datetime, None] = None,
    until: Union[str, date, datetime, None] = None,
    repos: Optional[Iterable[str]] = None,
    languages: Optional[Sequence[str]] = None,
    instance_ids: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    seed: int = 0,
) -> list[BenchTask]:
    """Load, filter and deterministically sample tasks.

    ``dataset`` is a key of ``SOURCES`` or a path to a local ``.jsonl`` file of dataset rows.
    ``since`` is inclusive and ``until`` exclusive, both on ``created_at`` (UTC; a plain date means
    midnight). Sampling sorts the matching instance ids and draws with ``random.Random(seed)``, so
    the same (dataset revision, filters, seed, limit) always yields the same tasks. The result is
    ordered by creation time, then instance id.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    source: Union[DatasetSource, Path]
    if dataset in SOURCES:
        source = SOURCES[dataset]
    elif Path(dataset).is_file():
        source = Path(dataset)
    else:
        raise ValueError(
            f"unknown dataset {dataset!r}: use one of {sorted(SOURCES)} or a .jsonl file path"
        )
    key = source.key if isinstance(source, DatasetSource) else Path(dataset).stem

    since_dt, until_dt = _as_datetime(since), _as_datetime(until)
    repo_set = {r.lower() for r in repos} if repos else None
    id_set = set(instance_ids) if instance_ids else None

    seen: set[str] = set()
    matching: list[_Candidate] = []
    for cand in _candidates(source, languages):
        if cand.instance_id in seen:
            continue
        if since_dt is not None and cand.created_at < since_dt:
            continue
        if until_dt is not None and cand.created_at >= until_dt:
            continue
        if repo_set is not None and cand.repo.lower() not in repo_set:
            continue
        if id_set is not None and cand.instance_id not in id_set:
            continue
        seen.add(cand.instance_id)
        matching.append(cand)

    matching.sort(key=lambda c: c.instance_id)
    if limit is not None and limit < len(matching):
        matching = random.Random(seed).sample(matching, limit)

    tasks = [
        row_to_task(row, language=cand.language, dataset=key)
        for cand, row in _full_rows(source, matching)
    ]
    tasks.sort(key=lambda t: (t.created_at, t.task_id))
    return tasks
