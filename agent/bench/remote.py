"""Run ``gold`` or ``run`` as a detached Modal function instead of on this machine.

The benchmark client holds every sandbox exec open for its whole duration; Modal keeps a finished
exec's output for 10 minutes only. A laptop that sleeps or drops its connection for longer turns
finished work into errored pairs (``exec not found``). Detached, the client runs in Modal's cloud
and survives anything that happens to this machine.

* The function's working directory is the root of the Modal Volume ``VOLUME``, so relative
  ``--out`` and ``--gold-file`` paths live there. Upload inputs first with ``modal volume put``
  and fetch results with ``modal volume get``.
* The volume is committed every ``COMMIT_EVERY_S`` seconds and on exit, so a run can be watched
  and fetched while it is going.
* Sandboxes and their Modal Secret are the same as for a local client.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from pathlib import Path

import modal

RUNNER_APP = "multi-agents-bench-runner"
VOLUME = "multi-agents-bench-runs"
MOUNT = "/vol"
COMMIT_EVERY_S = 60
# Modal's maximum function timeout.
TIMEOUT_S = 24 * 3600

_ROOT = Path(__file__).resolve().parents[2]

app = modal.App(RUNNER_APP)
volume = modal.Volume.from_name(VOLUME, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(str(_ROOT), extras=["bench"])
    .add_local_python_source("agent")
)


@app.function(image=image, volumes={MOUNT: volume}, timeout=TIMEOUT_S)
def bench(argv: list[str]) -> int:
    from agent.bench.cli import main

    os.chdir(MOUNT)
    stop = threading.Event()

    def commit_periodically() -> None:
        while not stop.wait(COMMIT_EVERY_S):
            volume.commit()

    committer = threading.Thread(target=commit_periodically, daemon=True)
    committer.start()
    try:
        return main(argv)
    finally:
        stop.set()
        committer.join()
        volume.commit()


def launch(argv: Sequence[str]) -> tuple[str, str]:
    """Start ``python -m agent.bench <argv>`` remotely; returns (app id, function call id)."""
    with modal.enable_output(), app.run(detach=True):
        call = bench.spawn(list(argv))
        return app.app_id, call.object_id
