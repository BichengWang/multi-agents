"""Template repositories used as instance images by the fake Modal client."""

from __future__ import annotations

from pathlib import Path

from .fakes import git, make_repo

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"

CHECK_SH = (
    'if grep -q "a + b" calc.py; then echo "PASSED calc::test_add"; '
    'else echo "FAILED calc::test_add"; fi\n'
    'echo "PASSED calc::test_keep"\n'
)
# Multilang style: needs the rebuild step (marker) before the fixed code counts, prints libtest lines.
CHECK_RS_SH = (
    'if [ -f rebuilt.marker ] && grep -q "a + b" calc.py; then echo "test m::t_add ... ok"; '
    'else echo "test m::t_add ... FAILED"; fi\n'
    'echo "test m::t_keep ... ok"\n'
)


def make_template(tmp_path: Path, *, nested: str | None = None) -> tuple[Path, str, str]:
    """A repo at its base commit, with a future fix commit on another branch and a tag, plus
    untracked leftovers like a real instance image (build output)."""
    repo = make_repo(
        tmp_path / "tpl",
        {"calc.py": BUGGY, "notes.txt": "keep me\n", ".gitignore": "*.log\n", "pkg/mod.py": "X = 1\n"},
        nested=nested,
    )
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "fix")
    (repo / "calc.py").write_text(FIXED)
    git(repo, "commit", "-qam", "the fix")
    fix = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "tag", "v2")
    git(repo, "checkout", "-q", "main")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"\x00prebuilt")
    (repo / "leftover.txt").write_text("from image build\n")
    return repo, base, fix


def make_patch(template: Path, files: dict[str, str | None]) -> str:
    """Patch (as `git diff --binary`) from applying ``files`` to a scratch copy of ``template``."""
    scratch = template.parent / f"scratch-{abs(hash(tuple(sorted(files.items()))))}"
    git(template.parent, "clone", "-q", "--branch", "main", str(template), str(scratch))
    for name, content in files.items():
        target = scratch / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    git(scratch, "add", "-A")
    return git(scratch, "diff", "--cached", "--binary", "--full-index", "HEAD")
