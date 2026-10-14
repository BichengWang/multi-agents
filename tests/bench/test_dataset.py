import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.bench import dataset
from agent.bench.dataset import (
    SOURCES,
    BenchTask,
    DatasetSource,
    default_image_name,
    load_tasks,
    parse_created_at,
)
from agent.teams.types import CodingTask

FIXTURE = Path(__file__).parent / "fixtures" / "rows.jsonl"
# Rows recorded from SWE-bench-Live (MultiLang: oauth2-proxy 2024, gh-aw 2026-04, jj 2025-06,
# turso 2026-07, elsa 2026-07; Python: sqllineage 2024-01, haystack 2025-04), text fields trimmed.
ALL_IDS = {
    "oauth2-proxy__oauth2-proxy-2524",
    "github__gh-aw-27242",
    "jj-vcs__jj-6728",
    "tursodatabase__turso-8102",
    "elsa-workflows__elsa-core-7898",
    "reata__sqllineage-521",
    "deepset-ai__haystack-9284",
}


def ids(tasks):
    return {t.task_id for t in tasks}


def test_fixture_rows_map_to_bench_tasks():
    tasks = {t.task_id: t for t in load_tasks(str(FIXTURE))}
    assert set(tasks) == ALL_IDS
    go = tasks["github__gh-aw-27242"]
    assert isinstance(go, BenchTask) and isinstance(go, CodingTask)
    assert go.repo == "github/gh-aw"
    assert go.language == "go"
    assert go.created_at == datetime(2026, 4, 19, 23, 34, 14, tzinfo=timezone.utc)
    assert go.image == "starryzhang/sweb.eval.x86_64.github_1776_gh-aw-27242"
    assert go.base_commit and len(go.base_commit) == 40
    assert go.grading.test_cmds and go.grading.print_cmds and go.grading.rebuild_cmds
    assert go.grading.log_parser.startswith("def parser(")
    assert go.grading.fail_to_pass and go.grading.test_patch.startswith("diff --git")
    assert go.dataset == "rows"  # local jsonl sources are keyed by file stem


def test_python_rows_derive_the_image_name_and_default_parser():
    py = {t.task_id: t for t in load_tasks(str(FIXTURE))}["reata__sqllineage-521"]
    assert py.image == "starryzhang/sweb.eval.x86_64.reata_1776_sqllineage-521"
    assert py.grading.log_parser == "pytest"
    assert py.grading.print_cmds == () and py.grading.test_cmds
    assert py.created_at == datetime(2024, 1, 1, 2, 57, 59, tzinfo=timezone.utc)


def test_default_image_name_matches_upstream_rule():
    assert (
        default_image_name("open-telemetry__opentelemetry-operator-4245")
        == "starryzhang/sweb.eval.x86_64.open-telemetry_1776_opentelemetry-operator-4245"
    )


def test_agents_only_see_the_problem_statement():
    task = next(t for t in load_tasks(str(FIXTURE)) if t.task_id == "jj-vcs__jj-6728")
    visible = task.coding_task()
    assert type(visible) is CodingTask
    assert visible.task_id == task.task_id and visible.instruction == task.instruction
    assert "Resolve the GitHub issue" in visible.instruction
    for secret in (task.grading.test_patch, *task.grading.fail_to_pass, *task.grading.test_cmds):
        assert secret and secret not in visible.instruction
    assert "grading" not in vars(visible)


def test_parse_created_at_variants():
    expected = datetime(2025, 8, 3, 10, 47, 52, tzinfo=timezone.utc)
    assert parse_created_at("2025-08-03T10:47:52Z") == expected
    assert parse_created_at("2025-08-03T10:47:52") == expected
    assert parse_created_at(datetime(2025, 8, 3, 10, 47, 52)) == expected
    assert parse_created_at("2025-08-03T12:47:52+02:00") == expected


def test_since_is_inclusive_and_until_exclusive():
    assert ids(load_tasks(str(FIXTURE), since="2026-01-01")) == {
        "github__gh-aw-27242",
        "tursodatabase__turso-8102",
        "elsa-workflows__elsa-core-7898",
    }
    # gh-aw was created 2026-04-19T23:34:14Z: a same-day `until` excludes it, next day includes it.
    assert "github__gh-aw-27242" not in ids(load_tasks(str(FIXTURE), until="2026-04-19"))
    assert "github__gh-aw-27242" in ids(load_tasks(str(FIXTURE), until="2026-04-20"))
    assert ids(load_tasks(str(FIXTURE), since="2026-07-30", until="2026-07-31")) == {
        "elsa-workflows__elsa-core-7898"
    }
    assert ids(load_tasks(str(FIXTURE), since="2026-07-30T00:03:31")) == {
        "elsa-workflows__elsa-core-7898"
    }


def test_repo_language_and_instance_filters():
    assert ids(load_tasks(str(FIXTURE), repos=["JJ-VCS/jj"])) == {"jj-vcs__jj-6728"}
    assert ids(load_tasks(str(FIXTURE), languages=["rust"])) == {
        "jj-vcs__jj-6728",
        "tursodatabase__turso-8102",
    }
    assert ids(load_tasks(str(FIXTURE), languages=["go", "cs"], since="2026-01-01")) == {
        "github__gh-aw-27242",
        "elsa-workflows__elsa-core-7898",
    }
    assert ids(load_tasks(str(FIXTURE), instance_ids=["reata__sqllineage-521"])) == {
        "reata__sqllineage-521"
    }


def test_seeded_sampling_is_deterministic_and_seed_sensitive():
    first = [t.task_id for t in load_tasks(str(FIXTURE), limit=3, seed=11)]
    again = [t.task_id for t in load_tasks(str(FIXTURE), limit=3, seed=11)]
    assert first == again and len(set(first)) == 3
    others = {
        tuple(t.task_id for t in load_tasks(str(FIXTURE), limit=3, seed=s)) for s in range(12)
    }
    assert len(others) > 1  # different seeds draw different samples
    # Sampling happens after filtering, and the result is ordered by creation time.
    sample = load_tasks(str(FIXTURE), limit=2, seed=5, since="2025-01-01")
    assert len(sample) == 2
    assert [t.created_at for t in sample] == sorted(t.created_at for t in sample)
    assert all(t.created_at >= datetime(2025, 1, 1, tzinfo=timezone.utc) for t in sample)


def test_limit_larger_than_pool_returns_everything_and_zero_returns_nothing():
    assert ids(load_tasks(str(FIXTURE), limit=100)) == ALL_IDS
    assert load_tasks(str(FIXTURE), limit=0) == []
    with pytest.raises(ValueError):
        load_tasks(str(FIXTURE), limit=-1)


def test_unknown_dataset_is_rejected():
    with pytest.raises(ValueError):
        load_tasks("nope")


def test_sources_are_pinned_to_full_commit_hashes():
    for source in SOURCES.values():
        assert len(source.revision) == 40 and int(source.revision, 16) >= 0
        assert source.license == "mit"


def test_parquet_shards_are_read_by_language_with_projection(tmp_path, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    by_id = {r["instance_id"]: r for r in rows}
    shards = {
        "go": ["oauth2-proxy__oauth2-proxy-2524", "github__gh-aw-27242"],
        "rust": ["jj-vcs__jj-6728", "tursodatabase__turso-8102"],
    }
    files = {}
    for lang, instance_ids in shards.items():
        chosen = [{k: v for k, v in by_id[i].items() if k != "language"} for i in instance_ids]
        path = tmp_path / f"{lang}.parquet"
        pq.write_table(pa.Table.from_pylist(chosen), path)
        files[lang] = (f"data/{lang}.parquet",)
    source = DatasetSource("fake", "org/fake", "0" * 40, files)
    monkeypatch.setitem(SOURCES, "fake", source)
    downloaded = []

    def fake_download(src, filename):
        downloaded.append(filename)
        return str(tmp_path / Path(filename).name)

    monkeypatch.setattr(dataset, "_download", fake_download)

    tasks = load_tasks("fake", since="2026-01-01")
    assert {(t.task_id, t.language) for t in tasks} == {
        ("github__gh-aw-27242", "go"),
        ("tursodatabase__turso-8102", "rust"),
    }
    assert all(t.dataset == "fake" for t in tasks)

    downloaded.clear()
    only_rust = load_tasks("fake", languages=["rust"])
    assert {t.language for t in only_rust} == {"rust"}
    assert set(downloaded) == {"data/rust.parquet"}  # other language shards are never fetched

    sample = load_tasks("fake", limit=2, seed=3)
    assert load_tasks("fake", limit=2, seed=3) == sample and len(sample) == 2
