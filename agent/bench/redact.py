"""Secret scrubbing for everything the benchmark writes to disk.

Agents run with permissions bypassed inside sandboxes that hold API keys in their environment, so a
trajectory (harness log, final message, patch, error text) can contain a key verbatim, for example
after the agent ran ``env``. Everything persisted goes through a ``Redactor`` first:

* the exact values of every credential-looking variable in the agent sandbox environment (read
  from the sandbox itself, so it covers every key in the Modal Secret, not just the required ones),
  and of the same variables in the host environment;
* strings that look like common provider keys (``sk-...``, ``sk-ant-...``, ``ghp_...``, ...).

Grading always uses the in-memory original; only stored copies are redacted.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_MIN_VALUE_LEN = 8

# Host variables that hold credentials relevant to this project.
HOST_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "HF_TOKEN",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
)

_PATTERNS = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{16,}",
        r"sk-[A-Za-z0-9_\-]{20,}",
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{30,}",
        r"AIza[0-9A-Za-z_\-]{30,}",
        r"AKIA[0-9A-Z]{16}",
        r"xox[abprs]-[A-Za-z0-9\-]{10,}",
        r"hf_[A-Za-z0-9]{30,}",
        r"\b(?:ak|as)-[A-Za-z0-9]{20,}\b",
    )
]


class Redactor:
    def __init__(self, values: Iterable[str] = (), *, host_env: bool = True) -> None:
        self._lock = threading.Lock()
        self._values: list[str] = []
        self.count = 0
        if host_env:
            for name in HOST_VARS:
                self.add(os.environ.get(name))
        self.add_many(values)

    def add(self, value: str | None) -> None:
        if not value or len(value) < _MIN_VALUE_LEN:
            return
        with self._lock:
            if value not in self._values:
                self._values.append(value)
                # Longest first, so a value that contains another is replaced whole.
                self._values.sort(key=len, reverse=True)

    def add_many(self, values: Iterable[str]) -> None:
        for value in values:
            self.add(value)

    def add_env_dump(self, dump: str, *, ignore: Iterable[str] = ()) -> int:
        """Register the credential-looking variables from ``env`` output; returns how many."""
        skip = set(ignore)
        added = 0
        for line in dump.splitlines():
            name, sep, value = line.partition("=")
            if sep and _NAME.search(name) and value not in skip and len(value) >= _MIN_VALUE_LEN:
                self.add(value)
                added += 1
        return added

    def text(self, value: str) -> str:
        out = value
        with self._lock:
            known = list(self._values)
        for secret in known:
            if secret in out:
                out = out.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            out = pattern.sub(REDACTED, out)
        if out != value:
            self.count += 1
        return out

    def obj(self, value: Any) -> Any:
        """``text`` applied to every string (and dict key) inside nested JSON-like data."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, tuple):
            return [self.obj(v) for v in value]
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in value.items()}
        return value
