"""Background runner for queued campaigns, so a run no longer depends on a browser tab driving /advance."""

from __future__ import annotations

import threading

from .campaigns import FINISHED, CampaignHarness
from .store import BusinessError


class CampaignWorker:
    """One polling thread per process. Several processes may poll the same database:
    the lease taken inside CampaignHarness.run() decides which one advances a run."""

    def __init__(self, harness: CampaignHarness, *, poll_seconds: float = 0.25, max_passes: int = 12):
        self.harness = harness
        self.poll_seconds = poll_seconds
        self.max_passes = max_passes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process(self, live_id: str, run_id: str) -> None:
        error = None
        try:
            steps = -1
            for _ in range(self.max_passes):
                # Each pass is bounded by the harness deadline and step budget and re-takes the lease.
                state = self.harness.run(live_id, run_id)
                if (state["status"] in FINISHED or state["status"] == "blocked"
                        or state.get("budget_stop") == "step_limit" or state["steps"] == steps):
                    break
                steps = state["steps"]
        except BusinessError as exc:
            if exc.code == "PLAN_BUSY":
                # Another runner holds the lease; the job stays queued and is picked up once the lease is free.
                return
            error = exc.code
        except Exception:
            error = "WORKER_CRASHED"
        self.harness.finish_job(run_id, error)

    def run_once(self) -> bool:
        job = self.harness.next_job()
        if job is None:
            return False
        self.process(*job)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                # A database hiccup must not kill the thread; queued jobs stay queued.
                busy = False
            if not busy:
                self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="campaign-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 35) -> None:
        # A run in flight finishes its current pass (at most the harness deadline); its lease covers a hard kill.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
