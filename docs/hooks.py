"""MkDocs hooks: publish the repository's Markdown guides as one site without copying them.

The README and the package guides live next to the code (and the README is the PyPI page), so the
site pulls them in as generated pages and rewrites their links: links to another published guide
stay on the site, links to anything else in the repository go to GitHub.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

REPO_URL = "https://github.com/BichengWang/multi-agents"
ROOT = Path(__file__).resolve().parent.parent

# Repository path -> site page. Files under docs/ are published as themselves.
PULLED_IN = {
    "README.md": "index.md",
    "agent/teams/README.md": "teams.md",
    "agent/bench/README.md": "bench.md",
}

_LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
_BLOB = re.compile(rf"^{re.escape(REPO_URL)}/(?:blob|tree)/main/(?P<path>[^#?]+)(?P<rest>.*)$")


def _site_page(repo_path: str) -> str | None:
    if repo_path in PULLED_IN:
        return PULLED_IN[repo_path]
    if repo_path.startswith("docs/") and repo_path.endswith(".md"):
        return repo_path[len("docs/") :]
    return None


def _repo_path(src_uri: str) -> str:
    for repo_path, page in PULLED_IN.items():
        if page == src_uri:
            return repo_path
    return f"docs/{src_uri}"


def on_files(files, config):
    for repo_path, page in PULLED_IN.items():
        files.append(
            File.generated(config, page, abs_src_path=str(ROOT / repo_path))
        )
    return files


def on_page_markdown(markdown, page, config, files):
    here = posixpath.dirname(_repo_path(page.file.src_uri))

    def rewrite(match: re.Match[str]) -> str:
        target = match.group("target")
        blob = _BLOB.match(target)
        if blob:
            path, rest = blob.group("path").rstrip("/"), blob.group("rest")
        elif re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/", target):
            return match.group(0)
        else:
            path, sep, anchor = target.partition("#")
            path, rest = posixpath.normpath(posixpath.join(here, path)), sep + anchor
        page_uri = _site_page(path)
        if page_uri is not None:
            return f"]({page_uri}{rest})"
        kind = "tree" if (ROOT / path).is_dir() else "blob"
        return f"]({REPO_URL}/{kind}/main/{path}{rest})"

    return _LINK.sub(rewrite, markdown)
