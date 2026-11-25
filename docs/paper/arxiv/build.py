"""Build the arXiv version of the workshop paper from its Markdown page, docs/paper/index.md.

The Markdown page is the only source: this script converts it, so the two versions cannot drift.

Outputs:
    docs/paper/arxiv/main.tex                  LaTeX source (pandoc with filter.lua and template.tex)
    docs/paper/arxiv/figures/*.pdf             docs/paper/figures/*.svg converted with cairosvg
    docs/paper/arxiv/figures/manifest.txt      sha256 of each figure's SVG source and of its PDF
    docs/paper/arxiv/metadata.txt              title, authors, abstract (ASCII) and comments for
                                               the arXiv submission form
    docs/paper/paper.pdf                       the typeset paper (two pdflatex runs)
    PATH, with --tarball PATH only             main.tex and figures/*.pdf, the files arXiv compiles,
                                               as a .tar.gz; PATH must be outside the repository

Usage, from the repository root:
    python3 -B docs/paper/arxiv/build.py           convert, compile when pdflatex is found, write
                                                   every output above but the tarball
    python3 -B docs/paper/arxiv/build.py --tarball PATH
                                                   the same, and write the arXiv tarball to PATH
    python3 -B docs/paper/arxiv/build.py --check   regenerate main.tex and metadata.txt in memory and
                                                   compare them with the files on disk, check each
                                                   figure PDF against its SVG; writes nothing,
                                                   exits 1 on any difference

Requirements:
    Python 3.10 or later (standard library only, in this script);
    pandoc 3.14 (PANDOC or PATH); another version warns, since its output may differ;
    cairosvg with the cairo library, for the figures only (not for --check): on macOS with
        Homebrew's cairo, run the full build as
        DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run --no-project --quiet --with cairosvg \\
            python3 -B docs/paper/arxiv/build.py
        Without cairosvg the build reuses figure PDFs whose manifest matches the current SVGs;
    pdflatex (PDFLATEX or PATH) with the packages that template.tex loads, all in TeX Live 2023,
        for the PDF only; without it paper.pdf is not rebuilt and metadata.txt counts its pages.

Every output is reproducible: SOURCE_DATE_EPOCH (pdflatex) and the figure PDFs' creation date
(cairo) are the first day of the month in the author line, and the tarball's entries carry it too.

Checks that fail the build: a fenced code block that is not byte-identical in main.tex; citation
markers that differ from index.md's in count or order, a reference never cited, a bracketed number
left unconverted; a non-ASCII character that neither template.tex nor LaTeX maps; a pinned link to a path
missing at the pinned commit (when git is available); a figure or table count that differs; any
LaTeX error, undefined reference or citation, missing file, or unmapped or missing character.
Overfull and underfull boxes and other LaTeX warnings are printed, not fatal.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PAPER = ROOT / "docs" / "paper"
ARXIV = PAPER / "arxiv"
SOURCE = PAPER / "index.md"
TEMPLATE = ARXIV / "template.tex"
FILTER = ARXIV / "filter.lua"
MAIN_TEX = ARXIV / "main.tex"
FIG_SRC = PAPER / "figures"
FIG_OUT = ARXIV / "figures"
MANIFEST = FIG_OUT / "manifest.txt"
METADATA = ARXIV / "metadata.txt"
PDF = PAPER / "paper.pdf"

REPO_URL = "https://github.com/BichengWang/multi-agents"
# Relative links in the PDF point to a commit: files of docs/paper/ to the commit that adds the
# paper, every other file to the commit the paper cites code and data at.
PIN = "25e82c5"
CODE_PIN = "3f60bba"
PANDOC_VERSION = "3.14"
ABSTRACT_LIMIT = 1920  # characters, arXiv's limit for the abstract field

# Pandoc's Markdown reader, cut down to what Python-Markdown (MkDocs) also reads, so that the
# page parses the same way on the site and here.
READER = "markdown" + "".join(
    "-" + ext
    for ext in (
        "citations", "tex_math_dollars", "raw_tex", "latex_macros", "definition_lists",
        "line_blocks", "superscript", "subscript", "strikeout", "implicit_figures",
        "pandoc_title_block", "yaml_metadata_block", "example_lists", "fancy_lists",
        "implicit_header_references", "table_captions", "multiline_tables", "grid_tables",
        "simple_tables", "task_lists", "inline_notes", "footnotes", "bracketed_spans",
        "native_spans", "fenced_divs", "link_attributes", "inline_code_attributes",
        "raw_attribute", "escaped_line_breaks",
    )
)

# Non-ASCII characters that LaTeX's own UTF-8 support maps to T1 glyphs (accents, the en dash,
# the section sign); template.tex maps the others with \newunicodechar.
KERNEL_CHARS = set("\u00a7\u00e9\u00d6\u00fc\u2013")

# For the abstract field of the submission form, which must be plain ASCII.
ASCII = {
    "\u2212": "-", "\u2013": "-", "\u2014": "--", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u00d7": "x", "\u2264": "<=", "\u2265": ">=",
    "\u2026": "...", "\u00a0": " ",
}

CAIROSVG_HINT = (
    "cairosvg is not importable. Run the build with it, for example (macOS, Homebrew cairo):\n"
    "  DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run --no-project --quiet --with cairosvg "
    "python3 -B docs/paper/arxiv/build.py"
)


class BuildError(Exception):
    pass


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------------------------
# The Markdown source


def strip_fences(md: str) -> str:
    """The page with fenced code blocks blanked out (line count kept)."""
    out, fence = [], None
    for line in md.split("\n"):
        if fence is None:
            m = re.match(r"(`{3,}|~{3,})", line)
            if m:
                fence = m.group(1)
                out.append("")
                continue
            out.append(line)
        else:
            if re.fullmatch(re.escape(fence[0]) + "{%d,}\\s*" % len(fence), line):
                fence = None
            out.append("")
    if fence is not None:
        raise BuildError("index.md: unclosed code fence")
    return "\n".join(out)


def fenced_blocks(md: str) -> list[str]:
    """The content of every fenced code block, as the Markdown holds it."""
    blocks, fence, body = [], None, []
    for line in md.split("\n"):
        if fence is None:
            m = re.match(r"(`{3,}|~{3,})", line)
            if m:
                fence, body = m.group(1), []
        elif re.fullmatch(re.escape(fence[0]) + "{%d,}\\s*" % len(fence), line):
            blocks.append("\n".join(body))
            fence = None
        else:
            body.append(line)
    return blocks


def prose(md: str) -> str:
    """The page without code blocks, inline code and link targets."""
    text = strip_fences(md)
    text = re.sub(r"(`+)(?:(?!\1).)+?\1", " ", text)
    return re.sub(r"\]\([^)\s]*\)", "]", text)


def citation_markers(md: str) -> list[str]:
    return re.findall(r"\[(\d+(?:, \d+)*)\]", prose(md))


def section(md: str, heading: str) -> str:
    m = re.search(r"(?m)^## %s\n(.*?)(?=^## |\Z)" % re.escape(heading), md, flags=re.S)
    if not m:
        raise BuildError(f"index.md: no '## {heading}' section")
    return m.group(1).strip()


def front(md: str) -> dict[str, str]:
    lines = md.split("\n")
    if not lines[0].startswith("# "):
        raise BuildError("index.md must start with its H1 title")
    fields = [f.strip() for f in lines[2].split(" · ")]
    return {"title": lines[0][2:].strip(), "author": fields[0], "date": fields[2]}


def source_date_epoch(md: str) -> int:
    date = datetime.strptime(front(md)["date"], "%B %Y").replace(tzinfo=timezone.utc)
    return int(date.timestamp())


# ------------------------------------------------------------------------------------------
# main.tex


def find_pandoc() -> str:
    pandoc = os.environ.get("PANDOC") or shutil.which("pandoc")
    if not pandoc:
        raise BuildError("pandoc not found (set PANDOC or put it on PATH)")
    return pandoc


def pandoc_version(pandoc: str) -> str:
    out = subprocess.run([pandoc, "--version"], capture_output=True, text=True, check=True).stdout
    return out.split("\n", 1)[0].split()[-1]


def run_pandoc(pandoc: str) -> str:
    cmd = [
        pandoc, rel(SOURCE), "--from", READER, "--to", "latex", "--standalone",
        "--template", rel(TEMPLATE), "--lua-filter", rel(FILTER), "--wrap=none", "--preserve-tabs",
        "--metadata", f"arxiv-reader={READER}", "--metadata", f"arxiv-repo={REPO_URL}",
        "--metadata", f"arxiv-pin={PIN}", "--metadata", f"arxiv-code-pin={CODE_PIN}",
        "--metadata", f"arxiv-src-dir={rel(PAPER)}",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True)
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise BuildError(f"pandoc failed:\n{stderr}")
    if stderr:
        print(stderr, file=sys.stderr)
    return proc.stdout.decode("utf-8")


def check_tex(md: str, tex: str) -> None:
    """Assert that main.tex carries the page's code, citations and characters unchanged."""
    problems = []

    # fenced code, byte for byte
    fences = fenced_blocks(md)
    verbatims = re.findall(r"\\begin\{Verbatim\}(?:\[[^\n]*\])?\n(.*?)\n\\end\{Verbatim\}", tex, flags=re.S)
    if fences != verbatims:
        problems.append(
            f"code blocks differ: {len(fences)} fences in index.md, {len(verbatims)} Verbatim in main.tex"
            + "".join(f"\n  block {i + 1} differs" for i, (a, b) in enumerate(zip(fences, verbatims)) if a != b)
        )
    preamble_end = tex.index("\\begin{document}")
    body = re.sub(r"\\begin\{Verbatim\}.*?\\end\{Verbatim\}", " ", tex[preamble_end:], flags=re.S)

    # citations: same markers in the same order, every reference cited, nothing else converted
    markers = citation_markers(md)
    cites = [", ".join(k.strip()[3:] for k in m.split(",")) for m in re.findall(r"\\cite\{([^}]*)\}", body)]
    if any(not re.fullmatch(r"ref\d+", k.strip()) for m in re.findall(r"\\cite\{([^}]*)\}", body) for k in m.split(",")):
        problems.append("a \\cite key is not of the form refN")
    if markers != cites:
        first = next((i for i, (a, b) in enumerate(zip(markers, cites)) if a != b), min(len(markers), len(cites)))
        problems.append(
            f"citations differ: {len(markers)} markers in index.md, {len(cites)} \\cite in main.tex; "
            f"first difference at #{first + 1}"
        )
    items = re.findall(r"\\bibitem\{ref(\d+)\}", body)
    refs = re.findall(r"(?m)^(\d+)\. ", section(md, "References"))
    if items != refs or items != [str(i) for i in range(1, len(items) + 1)]:
        problems.append(f"bibliography: {len(items)} \\bibitem, {len(refs)} references in index.md")
    cited = {n for c in cites for n in c.split(", ")}
    uncited = [n for n in items if n not in cited]
    if uncited:
        problems.append("references never cited: " + ", ".join(uncited))
    leftover = re.findall(r"\[\d+(?:, \d+)*\]", body)
    if leftover:
        problems.append("bracketed numbers left in main.tex: " + ", ".join(leftover))

    # characters: every non-ASCII character is mapped in the template or by LaTeX itself
    mapped = set(re.findall(r"\\newunicodechar\{(.)\}", TEMPLATE.read_text(encoding="utf-8"))) | KERNEL_CHARS
    for name, text in (("index.md", md), ("main.tex", tex[preamble_end:])):
        missing = sorted({c for c in text if ord(c) > 127} - mapped)
        if missing:
            problems.append(f"{name}: characters without \\newunicodechar: " + " ".join(f"{c} (U+{ord(c):04X})" for c in missing))

    # figures and tables: one LaTeX float or table per Markdown one
    md_tables = re.findall(r"(?m)^\*\*Table ([A-Z]?\d+)\.\*\*", md)
    tex_tables = re.findall(r"(?m)^% Table ([A-Z]?\d+)$", tex)
    if md_tables != tex_tables or len(tex_tables) != len(re.findall(r"\\begin\{(?:tabular|longtable)\}", tex)):
        problems.append(f"tables: {len(md_tables)} in index.md, {len(tex_tables)} in main.tex")
    md_figures = re.findall(r"<figcaption><strong>Figure ([A-Z]?\d+)\.</strong>", md)
    tex_figures = re.findall(r"\\caption\*\{\\textbf\{Figure ([A-Z]?\d+)\.\}", tex)
    if md_figures != tex_figures or len(md_figures) != tex.count("\\begin{figure}"):
        problems.append(f"figures: {len(md_figures)} in index.md, {len(tex_figures)} in main.tex")

    # pinned links name paths that exist at the pinned commit (checked when git has the commit)
    git = shutil.which("git")
    pins = "|".join(re.escape(p) for p in (PIN, CODE_PIN))
    pinned = sorted(set(re.findall(r"%s/(blob|tree)/(%s)/([^}#]+)" % (re.escape(REPO_URL), pins), tex)))
    for pin in sorted({pin for _, pin, _ in pinned}):
        has_pin = git and subprocess.run(
            [git, "cat-file", "-e", f"{pin}^{{commit}}"], cwd=ROOT, capture_output=True
        ).returncode == 0
        if not has_pin:
            print(f"warning: git or commit {pin} not available; links pinned to it not checked", file=sys.stderr)
            continue
        for kind, path in [(k, p) for k, c, p in pinned if c == pin]:
            path = path.replace("\\%", "%")
            proc = subprocess.run([git, "cat-file", "-t", f"{pin}:{path}"], cwd=ROOT, capture_output=True, text=True)
            if proc.returncode != 0:
                problems.append(f"pinned link to {path}: not in commit {pin}")
            elif proc.stdout.strip() != kind:
                problems.append(f"pinned link to {path}: a {proc.stdout.strip()}, linked as {kind}")

    if problems:
        raise BuildError("main.tex does not match index.md:\n  " + "\n  ".join(problems))


def render_tex(md: str) -> tuple[str, str]:
    pandoc = find_pandoc()
    version = pandoc_version(pandoc)
    if version != PANDOC_VERSION:
        print(f"warning: pandoc {version}, not {PANDOC_VERSION}; main.tex may differ", file=sys.stderr)
    tex = run_pandoc(pandoc)
    if not tex.startswith("\\pdfoutput=1\n"):
        raise BuildError("main.tex must start with \\pdfoutput=1")
    check_tex(md, tex)
    return tex, version


def converter_line(tex: str) -> str:
    m = re.search(r"(?m)^% Converter: pandoc (\S+)$", tex)
    return m.group(1) if m else "unknown"


# ------------------------------------------------------------------------------------------
# Figures


def svg_sources() -> list[Path]:
    svgs = sorted(FIG_SRC.glob("*.svg"))
    if not svgs:
        raise BuildError(f"no SVG figures in {rel(FIG_SRC)}")
    return svgs


def read_manifest() -> dict[str, tuple[str, str]]:
    entries = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                svg_hash, pdf_hash, name = line.split()
                entries[name] = (svg_hash, pdf_hash)
    return entries


def manifest_problems() -> list[str]:
    problems = []
    entries = read_manifest()
    svgs = svg_sources()
    for svg in svgs:
        pdf = FIG_OUT / (svg.stem + ".pdf")
        if svg.name not in entries:
            problems.append(f"{rel(svg)}: no entry in {rel(MANIFEST)}")
        elif entries[svg.name][0] != sha256(svg.read_bytes()):
            problems.append(f"{rel(svg)} changed since {rel(pdf)} was made")
        elif not pdf.exists():
            problems.append(f"{rel(pdf)} is missing")
        elif entries[svg.name][1] != sha256(pdf.read_bytes()):
            problems.append(f"{rel(pdf)} differs from the PDF recorded in {rel(MANIFEST)}")
    names = {svg.name for svg in svgs}
    problems += [f"{rel(MANIFEST)}: entry for a missing SVG, {n}" for n in entries if n not in names]
    stems = {svg.stem for svg in svgs}
    problems += [f"{rel(p)} has no SVG source" for p in sorted(FIG_OUT.glob("*.pdf")) if p.stem not in stems]
    return problems


def convert_figures(epoch: int) -> None:
    try:
        # imported here: --check and the LaTeX steps need no cairo
        import cairocffi
        from cairosvg.parser import Tree
        from cairosvg.surface import PDFSurface
    except (ImportError, OSError) as exc:
        problems = manifest_problems()
        if problems:
            raise BuildError(f"{CAIROSVG_HINT}\n({exc})\nThe figure PDFs are out of date:\n  " + "\n  ".join(problems))
        print(f"warning: cairosvg not importable ({exc}); reusing figure PDFs that match their SVGs", file=sys.stderr)
        return
    FIG_OUT.mkdir(parents=True, exist_ok=True)
    stems = {svg.stem for svg in svg_sources()}
    for stale in FIG_OUT.glob("*.pdf"):
        if stale.stem not in stems:
            stale.unlink()
    lines = [
        "# SVG source and PDF of each figure: sha256 of the SVG, sha256 of the PDF, SVG name.",
        "# Written by docs/paper/arxiv/build.py; build.py --check compares the SVG hashes with docs/paper/figures/.",
    ]
    # cairo stamps the current time into the PDF; the paper's date keeps the file reproducible
    stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for svg in svg_sources():
        pdf = FIG_OUT / (svg.stem + ".pdf")
        out = io.BytesIO()
        surface = PDFSurface(Tree(url=str(svg)), out, 96)  # what cairosvg.svg2pdf does, at 96 dpi
        surface.cairo.set_metadata(cairocffi.PDF_METADATA_CREATE_DATE, stamp)
        surface.finish()
        data = out.getvalue()
        pdf.write_bytes(data)
        lines.append(f"{sha256(svg.read_bytes())}  {sha256(data)}  {svg.name}")
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"figures: {len(lines) - 2} SVG converted to {rel(FIG_OUT)}/")


# ------------------------------------------------------------------------------------------
# PDF


def find_pdflatex() -> str | None:
    return os.environ.get("PDFLATEX") or shutil.which("pdflatex")


def log_report(log: str) -> tuple[list[str], list[str]]:
    """(fatal problems, warnings) in a pdflatex log."""
    fatal, warnings = [], []
    lines = log.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^(?:!|\S*\.tex:\d+:) ", line):
            fatal.append(line.strip() + " " + " ".join(lines[i + 1:i + 3]).strip())
        if re.search(r"(Reference|Citation) `[^']*' on page \d+ undefined", line) or "There were undefined" in line:
            fatal.append(line.strip())
        if re.search(r"File `[^']*' not found|No file \S+\.(tex|sty|cls|pdf)", line):
            fatal.append(line.strip())
        if re.search(r"Unicode character .* not set up|Missing character", line):
            fatal.append(line.strip())
        m = re.match(r"^(Overfull|Underfull) \\[hv]box \((?:badness (\d+)|([\d.]+)pt too \w+)\)(.*)$", line)
        if m:
            if m.group(1) == "Overfull":
                warnings.append(f"Overfull box {m.group(3)}pt{m.group(4)}")
            else:
                warnings.append(f"Underfull box badness {m.group(2)}{m.group(4)}")
        elif re.match(r"^(LaTeX|Package \w+|Class \w+) (Font )?Warning", line):
            text = line.strip()
            j = i + 1
            while j < len(lines) and lines[j].startswith("(") and not lines[j].startswith("(./"):
                text += " " + lines[j].strip()
                j += 1
            warnings.append(text)
    return fatal, warnings


def page_count(pdf: bytes) -> int:
    """Pages of a PDF: the largest /Count of a /Pages node, object streams included."""
    texts = [pdf]
    for m in re.finditer(rb"stream\r?\n", pdf):
        start = m.end()
        end = pdf.find(b"endstream", start)
        try:
            texts.append(zlib.decompressobj().decompress(pdf[start:end]))
        except zlib.error:
            continue
    counts = []
    for text in texts:
        for d in re.findall(rb"<<(?:(?!<<|>>).)*?/Type\s*/Pages\b(?:(?!<<|>>).)*?>>", text, flags=re.S):
            counts += [int(c) for c in re.findall(rb"/Count\s+(\d+)", d)]
    if not counts:
        raise BuildError("cannot read the page count of the PDF")
    return max(counts)


def compile_pdf(pdflatex: str, tex: str, epoch: int) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.endswith("INPUTS")}
    env.update(SOURCE_DATE_EPOCH=str(epoch), FORCE_SOURCE_DATE="1")
    with tempfile.TemporaryDirectory(prefix="arxiv-build-") as tmp:
        work = Path(tmp)
        (work / "main.tex").write_text(tex, encoding="utf-8")
        (work / "figures").mkdir()
        for pdf in sorted(FIG_OUT.glob("*.pdf")):
            shutil.copyfile(pdf, work / "figures" / pdf.name)
        for run in (1, 2):
            proc = subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-shell-restricted", "main.tex"],
                cwd=work, env=env, capture_output=True,
            )
            log = (work / "main.log").read_text(encoding="latin-1") if (work / "main.log").exists() else ""
            if proc.returncode != 0:
                fatal, _ = log_report(log)
                raise BuildError(f"pdflatex run {run} failed:\n  " + "\n  ".join(fatal or [log[-2000:]]))
        fatal, warnings = log_report(log)
        if fatal:
            raise BuildError("pdflatex:\n  " + "\n  ".join(fatal))
        big = [w for w in warnings if w.startswith("Overfull") and float(w.split()[2][:-2]) > 1.0]
        print(f"pdflatex: {len(warnings)} warnings, {len(big)} overfull boxes wider than 1pt")
        for w in warnings:
            print("  " + w)
        return (work / "main.pdf").read_bytes()


# ------------------------------------------------------------------------------------------
# metadata.txt and the tarball


def metadata(md: str, tex: str, pages: int) -> str:
    info = front(md)
    abstract = section(md, "Abstract")
    if "\n\n" in abstract:
        raise BuildError("the abstract must be one paragraph")
    abstract = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", abstract)
    abstract = re.sub(r"`([^`]*)`", r"\1", abstract)
    abstract = re.sub(r"\*\*?([^*]+)\*\*?", r"\1", abstract)
    abstract = "".join(ASCII.get(c, c) for c in " ".join(abstract.split()))
    bad = sorted({c for c in abstract if ord(c) > 126})
    if bad:
        raise BuildError("abstract: no ASCII form for " + " ".join(f"U+{ord(c):04X}" for c in bad))
    if len(abstract) > ABSTRACT_LIMIT:
        raise BuildError(f"abstract: {len(abstract)} characters, arXiv allows {ABSTRACT_LIMIT}")
    figures = tex.count("\\begin{figure}")
    tables = len(re.findall(r"(?m)^% Table ", tex))
    comments = (
        f"{pages} pages, {figures} figures, {tables} tables. "
        f"Code and data: {REPO_URL} (commit {CODE_PIN}; paper files at {PIN})"
    )
    return (
        "# Fields of the arXiv submission form, from docs/paper/index.md and docs/paper/paper.pdf.\n"
        "# Written by docs/paper/arxiv/build.py; do not edit by hand.\n\n"
        f"Title: {info['title']}\n\n"
        f"Authors: {info['author']}\n\n"
        f"Abstract:\n{abstract}\n\n"
        f"Comments: {comments}\n"
    )


def write_tarball(tex: str, epoch: int, tarball: Path) -> None:
    members = [("main.tex", tex.encode("utf-8"))]
    members += [(f"figures/{p.name}", p.read_bytes()) for p in sorted(FIG_OUT.glob("*.pdf"))]
    tarball.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=epoch) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), epoch, 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    tarball.write_bytes(buf.getvalue())
    print(f"tarball: {tarball} ({tarball.stat().st_size:,} bytes; {len(members)} files)")


def tarball_path(arg: str) -> Path:
    path = Path(arg).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise BuildError(f"--tarball {arg}: write the tarball outside the repository")
    return path


# ------------------------------------------------------------------------------------------


def build(tarball: Path | None) -> int:
    md = SOURCE.read_text(encoding="utf-8")
    tex, _ = render_tex(md)
    MAIN_TEX.write_text(tex, encoding="utf-8")
    print(f"main.tex: {rel(MAIN_TEX)} ({len(tex):,} characters)")
    epoch = source_date_epoch(md)
    convert_figures(epoch)
    pdflatex = find_pdflatex()
    if pdflatex:
        pdf = compile_pdf(pdflatex, tex, epoch)
        PDF.write_bytes(pdf)
        pages = page_count(pdf)
        print(f"pdf: {rel(PDF)} ({pages} pages, {len(pdf):,} bytes)")
    elif PDF.exists():
        pages = page_count(PDF.read_bytes())
        print(f"warning: pdflatex not found (set PDFLATEX); {rel(PDF)} not rebuilt, its {pages} pages go into metadata", file=sys.stderr)
    else:
        raise BuildError("pdflatex not found (set PDFLATEX or put it on PATH) and no paper.pdf to count pages from")
    METADATA.write_text(metadata(md, tex, pages), encoding="utf-8")
    print(f"metadata: {rel(METADATA)}")
    if tarball is not None:
        write_tarball(tex, epoch, tarball)
    return 0


def check() -> int:
    md = SOURCE.read_text(encoding="utf-8")
    problems = []
    tex, version = render_tex(md)
    on_disk = MAIN_TEX.read_text(encoding="utf-8") if MAIN_TEX.exists() else ""
    recorded = converter_line(on_disk)
    if recorded != version:
        print(f"warning: main.tex was built with pandoc {recorded}, this is pandoc {version}", file=sys.stderr)
    strip = re.compile(r"(?m)^% Converter: pandoc \S+$")
    if strip.sub("", tex) != strip.sub("", on_disk):
        problems.append(f"{rel(MAIN_TEX)} differs from a fresh conversion of {rel(SOURCE)}")
    problems += manifest_problems()
    if PDF.exists():
        meta = metadata(md, tex, page_count(PDF.read_bytes()))
        if not METADATA.exists() or METADATA.read_text(encoding="utf-8") != meta:
            problems.append(f"{rel(METADATA)} differs from the one index.md and paper.pdf give")
    else:
        problems.append(f"{rel(PDF)} is missing")
    for p in problems:
        print("differs: " + p)
    print("check: " + ("ok" if not problems else f"{len(problems)} difference(s)"))
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="compare main.tex, metadata.txt and the figure PDFs with a fresh conversion; write nothing")
    parser.add_argument("--tarball", metavar="PATH", help="also write main.tex and figures/*.pdf as the arXiv tarball at PATH, outside the repository")
    args = parser.parse_args()
    if args.check and args.tarball:
        parser.error("--check writes nothing; drop --tarball")
    try:
        if args.check:
            return check()
        return build(tarball_path(args.tarball) if args.tarball else None)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
