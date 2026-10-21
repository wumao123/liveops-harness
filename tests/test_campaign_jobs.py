from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from liveops.api import create_app
from liveops.campaigns import CampaignHarness
from liveops.store import BusinessError, Store
from liveops.worker import CampaignWorker


LIVE = "LIVE-DEMO-001"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("LIVEOPS_SPECIALIST_MODEL", raising=False)
    monkeypatch.delenv("LIVEOPS_LLM_MODEL", raising=False)


@pytest.fixture
def harness(tmp_path):
    return CampaignHarness(Store(str(tmp_path / "business.db")))


def job_row(harness, run_id):
    with harness.store.connection() as db:
        return dict(db.execute("SELECT job_state,job_error,lease_owner,lease_until FROM campaign_runs WHERE run_id=?",
                               (run_id,)).fetchone())


def test_start_is_idempotent_and_worker_finishes_without_a_client(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=2)
    first = harness.start(LIVE, plan["run_id"])
    assert harness.start(LIVE, plan["run_id"]) == first
    assert first["job_state"] == "queued"
    queued = [e for e in harness.events(LIVE, plan["run_id"], 0)[0] if e["kind"] == "job_queued"]
    assert len(queued) == 1

    worker = CampaignWorker(harness)
    assert worker.run_once() is True
    assert worker.run_once() is False
    done = harness.get(LIVE, plan["run_id"])
    assert done["status"] == "succeeded"
    assert all(item["attempts"] == 1 for item in done["tasks"].values())
    assert job_row(harness, plan["run_id"])["job_state"] == "idle"
    # A finished run is not queued again.
    assert harness.start(LIVE, plan["run_id"])["job_state"] == "idle"


def test_worker_resumes_a_run_whose_runner_died_after_the_lease_expired(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=2)
    harness.run(LIVE, plan["run_id"], max_waves=1)
    state = harness.get(LIVE, plan["run_id"])
    state["tasks"]["brief_0"].update(status="running", attempts=1)
    with harness.store.connection() as db:
        # What a killed process leaves behind: queued job, stale lease, a task marked running.
        db.execute("UPDATE campaign_runs SET state=?,job_state='queued',lease_owner='dead-runner',lease_until=? WHERE run_id=?",
                   (harness._json(state), time.time() - 1, plan["run_id"]))
        db.commit()

    restarted = CampaignHarness(Store(harness.store.path))
    assert CampaignWorker(restarted).run_once() is True
    done = restarted.get(LIVE, plan["run_id"])
    assert done["status"] == "succeeded"
    assert done["tasks"]["selection"]["attempts"] == 1
    assert done["tasks"]["brief_0"]["attempts"] == 2
    assert done["tasks"]["brief_1"]["attempts"] == 1


def test_live_lease_keeps_a_second_worker_out_and_the_job_queued(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    harness.start(LIVE, plan["run_id"])
    with harness.store.connection() as db:
        db.execute("UPDATE campaign_runs SET lease_owner='other-process',lease_until=? WHERE run_id=?",
                   (time.time() + 60, plan["run_id"]))
        db.commit()
    worker = CampaignWorker(harness)
    assert harness.next_job() is None
    worker.process(LIVE, plan["run_id"])
    assert job_row(harness, plan["run_id"])["job_state"] == "queued"
    assert harness.get(LIVE, plan["run_id"])["steps"] == 0


def test_cancel_an_idle_run_stops_it_now_and_it_cannot_be_restarted(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=2)
    harness.run(LIVE, plan["run_id"], max_waves=1)
    result = harness.cancel(LIVE, plan["run_id"])
    assert result["cancel"] == "stopped"
    state = harness.get(LIVE, plan["run_id"])
    assert state["status"] == "cancelled"
    assert state["tasks"]["selection"]["status"] == "succeeded"
    assert "summary" in state["cancel"]["not_started"]
    assert harness.cancel(LIVE, plan["run_id"])["cancel"] == "stopped"
    assert harness.run(LIVE, plan["run_id"])["status"] == "cancelled"
    for call in (lambda: harness.start(LIVE, plan["run_id"]),
                 lambda: harness.replan(LIVE, plan["run_id"], {}, state["revision"])):
        with pytest.raises(BusinessError) as exc:
            call()
        assert exc.value.code == "PLAN_CANCELLED"


def test_cancel_during_a_run_stops_at_the_next_wave_boundary(harness, monkeypatch):
    plan = harness.create(LIVE, "生成手卡", top_k=2)
    entered, release = threading.Event(), threading.Event()
    original = harness._perform

    def slow_selection(task, context):
        if task.role == "merchandiser":
            entered.set()
            assert release.wait(5)
        return original(task, context)

    monkeypatch.setattr(harness, "_perform", slow_selection)
    outcome = {}
    runner = threading.Thread(target=lambda: outcome.update(state=harness.run(LIVE, plan["run_id"])))
    runner.start()
    assert entered.wait(5)
    assert harness.cancel(LIVE, plan["run_id"])["cancel"] == "requested"
    assert job_row(harness, plan["run_id"])["job_state"] == "cancel_requested"
    release.set()
    runner.join(5)

    state = outcome["state"]
    assert state["status"] == "cancelled"
    # The wave in flight finished and was saved; nothing after it started.
    assert state["tasks"]["selection"]["status"] == "succeeded"
    assert state["steps"] == 1
    assert {state["tasks"][t]["status"] for t in ("brief_0", "brief_1", "summary")} == {"pending"}
    assert job_row(harness, plan["run_id"])["job_state"] == "idle"
    kinds = [e["kind"] for e in harness.events(LIVE, plan["run_id"], 0)[0]]
    assert kinds.index("cancel_requested") < kinds.index("cancelled")


def test_cancel_after_success_leaves_the_result(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    done = harness.run(LIVE, plan["run_id"])
    assert harness.cancel(LIVE, plan["run_id"])["cancel"] == "already_finished"
    assert harness.get(LIVE, plan["run_id"]) == done


def test_cancel_that_arrives_after_the_last_wave_is_recorded_as_ignored(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    harness.start(LIVE, plan["run_id"])
    harness.run(LIVE, plan["run_id"])
    with harness.store.connection() as db:
        # The flag lands after the runner's last boundary check but before the worker hands the job back.
        db.execute("UPDATE campaign_runs SET job_state='cancel_requested' WHERE run_id=?", (plan["run_id"],))
        db.commit()
    assert CampaignWorker(harness).run_once() is True
    assert harness.get(LIVE, plan["run_id"])["status"] == "succeeded"
    assert job_row(harness, plan["run_id"])["job_state"] == "idle"
    assert harness.events(LIVE, plan["run_id"], 0)[0][-1]["kind"] == "cancel_ignored"


def test_events_replay_strictly_after_the_cursor(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=2)
    harness.run(LIVE, plan["run_id"])
    events, active = harness.events(LIVE, plan["run_id"], 0)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs)) and not active
    assert events[0]["kind"] == "created"
    assert events[-1]["body"]["status"] == "succeeded"
    middle = seqs[len(seqs) // 2]
    assert [e["seq"] for e in harness.events(LIVE, plan["run_id"], middle)[0]] == [s for s in seqs if s > middle]
    assert harness.events(LIVE, plan["run_id"], seqs[-1])[0] == []
    # Events describe progress; artifact bodies stay behind their endpoint.
    assert "approved_briefs" not in json.dumps(events, ensure_ascii=False)
    with pytest.raises(BusinessError):
        harness.events("OTHER-LIVE", plan["run_id"], 0)


def test_existing_database_gets_job_columns_in_place(tmp_path):
    path = str(tmp_path / "business.db")
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE campaign_runs (run_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, request_hash TEXT NOT NULL,"
                   " state TEXT NOT NULL, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0)")
    harness = CampaignHarness(Store(path))
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    assert harness.start(LIVE, plan["run_id"])["job_state"] == "queued"


def read_sse(response):
    events, current = [], {}
    for line in response.iter_lines():
        if not line:
            if current:
                events.append(current)
                if current.get("event") == "end":
                    break
            current = {}
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(": ")
        current[key] = value
    return [e for e in events if "event" in e]


def test_http_start_returns_202_and_the_stream_resumes_from_last_event_id(tmp_path):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))
    with TestClient(app) as client:
        plan = client.post("/api/campaigns", json={"instruction": "生成手卡", "top_k": 2}).json()
        started = client.post(f"/api/campaigns/{LIVE}/{plan['run_id']}/start")
        assert started.status_code == 202
        assert started.json()["events"].endswith("/events")

        with client.stream("GET", started.json()["events"]) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            events = read_sse(response)
        assert events[-1]["event"] == "end"
        data = [e for e in events if "id" in e]
        assert data[0]["event"] == "created"
        assert any(e["event"] == "job_finished" for e in data)
        assert client.get(f"/api/campaigns/{LIVE}/{plan['run_id']}").json()["status"] == "succeeded"

        middle = data[len(data) // 2]["id"]
        with client.stream("GET", started.json()["events"], headers={"Last-Event-ID": middle}) as response:
            replay = [e for e in read_sse(response) if "id" in e]
        assert [e["id"] for e in replay] == [e["id"] for e in data if int(e["id"]) > int(middle)]

        assert client.get(f"/api/campaigns/{LIVE}/missing/events").status_code == 409
        cancelled = client.post(f"/api/campaigns/{LIVE}/{plan['run_id']}/cancel").json()
        assert cancelled["cancel"] == "already_finished"
