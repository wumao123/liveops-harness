"""Protocol tests use a separate simulator ledger, never a real commerce API."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from liveops.api import create_app
from liveops.remote import RemoteOperations, SimulatedPlatform
from liveops.store import BusinessError, Store


LIVE = "LIVE-DEMO-001"


def make_app(tmp_path):
    return create_app(str(tmp_path / "commerce.db"), str(tmp_path / "graph.db"),
                      coupon_backend="simulated_remote", remote_path=str(tmp_path / "platform.db"))


def prepare(client, mode="normal", visibility_queries=2):
    assert client.post("/api/simulator/remote-fault", json={"mode": mode, "visibility_queries": visibility_queries}).status_code == 200
    command_id = str(uuid.uuid4())
    result = client.post("/api/commands", json={
        "live_id": LIVE, "instruction": "给面霜发 10 元券 20 张", "command_id": command_id,
    }).json()
    assert result["status"] == "pending_approval"
    return command_id


def approve(client, command_id):
    return client.post(f"/api/commands/{command_id}/approval", json={"approved": True}).json()


def reconcile(client, command_id):
    return client.post(f"/api/commands/{command_id}/reconcile").json()


def test_remote_normal_path_requires_approval_and_records_receipt(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        assert app.state.remote_operations.platform.coupon_count() == 0
        result = approve(client, command_id)
        assert result["status"] == "ok"
        assert result["result"]["remote_status"] == "succeeded"
        assert app.state.remote_operations.platform.coupon_count() == 1
        assert len(app.state.store.snapshot(LIVE)["coupons"]) == 1


def test_lost_response_queries_without_resubmitting(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "lost_response")
        first = approve(client, command_id)
        assert first["status"] == "unknown"
        assert first["result"]["tool_error"]["retryable"] is False
        operation_id = first["result"]["operation_id"]
        assert app.state.remote_operations.platform.coupon_count() == 1
        assert app.state.store.snapshot(LIVE)["coupons"] == []
        repeated = client.post("/api/commands", json={"instruction": "给面霜发 10 元券 20 张", "command_id": command_id}).json()
        assert repeated == first
        recovered = reconcile(client, command_id)
        assert recovered["status"] == "ok"
        assert reconcile(client, command_id) == recovered
        assert app.state.remote_operations.platform.submission_count(operation_id) == 1
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM action_ledger WHERE command_id=?", (command_id,)).fetchone()[0] == 1
        assert len(app.state.store.snapshot(LIVE)["coupons"]) == 1


def test_delayed_receipt_survives_restart_and_expired_approval(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "delayed_visibility")
        assert approve(client, command_id)["status"] == "unknown"
    restarted = make_app(tmp_path)
    with TestClient(restarted) as client:
        monkeypatch.setenv("LIVEOPS_APPROVAL_TTL_SECONDS", "0")
        client.post("/api/simulator/stock", json={"sku": "SK-102", "stock": 0})
        assert reconcile(client, command_id)["status"] == "unknown"
        assert reconcile(client, command_id)["status"] == "unknown"
        assert reconcile(client, command_id)["status"] == "ok"
        assert restarted.state.remote_operations.platform.submission_count(f"remote-{command_id}") == 1
        assert restarted.state.remote_operations.platform.coupon_count() == 1
        assert len(restarted.state.store.snapshot(LIVE)["coupons"]) == 1


def test_not_found_is_not_proof_of_failed_submission(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "before_commit")
        assert approve(client, command_id)["status"] == "unknown"
        assert reconcile(client, command_id)["status"] == "unknown"
        assert reconcile(client, command_id)["status"] == "unknown"
        assert reconcile(client, command_id)["status"] == "manual_review"
        assert app.state.remote_operations.platform.submission_count(f"remote-{command_id}") == 1
        assert app.state.remote_operations.platform.coupon_count() == 0
        assert app.state.store.snapshot(LIVE)["coupons"] == []


def test_unavailable_query_goes_manual_and_explicit_query_can_later_recover(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "query_unavailable")
        assert approve(client, command_id)["status"] == "unknown"
        result = reconcile(client, command_id)
        assert result["status"] == "manual_review"
        assert result["result"]["tool_error"]["next_step"] == "manual_review"
        client.post("/api/simulator/remote-fault", json={"mode": "normal"})
        assert reconcile(client, command_id)["status"] == "ok"
        assert app.state.remote_operations.platform.submission_count(f"remote-{command_id}") == 1


def test_explicit_remote_rejection_is_not_retried(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "reject")
        result = approve(client, command_id)
        assert result["status"] == "blocked"
        assert result["result"]["code"] == "REMOTE_REJECTED"
        assert reconcile(client, command_id)["status"] == "blocked"
        assert app.state.remote_operations.platform.coupon_count() == 0
        assert app.state.remote_operations.platform.submission_count(f"remote-{command_id}") == 1


def test_changed_business_state_never_submits_to_platform(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        client.post("/api/simulator/stock", json={"sku": "SK-102", "stock": 0})
        assert approve(client, command_id)["status"] == "blocked"
        assert app.state.remote_operations.platform.submission_count(f"remote-{command_id}") == 0


def test_bad_receipt_cannot_modify_local_state(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    platform = app.state.remote_operations.platform
    original = platform.create_coupon

    def wrong_receipt(*args):
        return {**original(*args), "quantity": 99}

    monkeypatch.setattr(platform, "create_coupon", wrong_receipt)
    with TestClient(app) as client:
        command_id = prepare(client)
        assert approve(client, command_id)["status"] == "manual_review"
        assert app.state.store.snapshot(LIVE)["coupons"] == []
        assert reconcile(client, command_id)["status"] == "ok"
        assert app.state.store.snapshot(LIVE)["coupons"][0]["quantity"] == 20


def test_independent_reconcilers_complete_one_local_transaction(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client, "lost_response")
        assert approve(client, command_id)["status"] == "unknown"
        other = RemoteOperations(Store(str(tmp_path / "commerce.db")), SimulatedPlatform(str(tmp_path / "platform.db")))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.reconcile, f"remote-{command_id}") for service in [app.state.remote_operations, other]]
            assert all(future.result()["status"] == "ok" for future in futures)
        assert len(app.state.store.snapshot(LIVE)["coupons"]) == 1
        assert other.platform.coupon_count() == 1


def test_queued_action_is_immutable_and_rechecked_before_first_send(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        action = app.state.store.command(command_id)["proposal"]
        operation_id = app.state.store.queue_remote_coupon(LIVE, command_id, action, "fixture-key", time.time() + 300)
        with pytest.raises(BusinessError, match="操作 ID"):
            app.state.store.queue_remote_coupon(LIVE, command_id, {**action, "quantity": 30}, "fixture-key", time.time() + 300)
        app.state.store.simulate_stock_change(LIVE, "SK-102", 0)
        result = app.state.remote_operations.reconcile(operation_id)
        assert result["status"] == "blocked"
        assert app.state.remote_operations.platform.submission_count(operation_id) == 0


def test_expired_ready_operation_cannot_be_dispatched(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        action = app.state.store.command(command_id)["proposal"]
        operation_id = app.state.store.queue_remote_coupon(LIVE, command_id, action, "fixture-expired", time.time() + 300)
        with app.state.store.connection() as db:
            db.execute("UPDATE remote_operations SET expires_at=0 WHERE operation_id=?", (operation_id,))
            db.commit()
        result = app.state.remote_operations.reconcile(operation_id)
        assert result["status"] == "blocked"
        assert result["code"] == "APPROVAL_EXPIRED"
        assert app.state.remote_operations.platform.submission_count(operation_id) == 0


def test_sent_record_before_remote_commit_is_not_blindly_resubmitted(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        action = app.state.store.command(command_id)["proposal"]
        operation_id = app.state.store.queue_remote_coupon(LIVE, command_id, action, "fixture-sent", time.time() + 300)
        assert app.state.store.begin_remote_dispatch(operation_id)
        recovered = RemoteOperations(Store(str(tmp_path / "commerce.db")), SimulatedPlatform(str(tmp_path / "platform.db")))
        assert recovered.reconcile(operation_id)["status"] == "unknown"
        assert recovered.platform.submission_count(operation_id) == 0
        assert recovered.platform.coupon_count() == 0


def test_remote_commit_before_local_receipt_storage_can_recover(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        action = app.state.store.command(command_id)["proposal"]
        operation_id = app.state.store.queue_remote_coupon(LIVE, command_id, action, "fixture-committed", time.time() + 300)
        assert app.state.store.begin_remote_dispatch(operation_id)
        platform = app.state.remote_operations.platform
        platform.create_coupon(operation_id, action, app.state.store.action_hash(action))
        assert app.state.store.snapshot(LIVE)["coupons"] == []
        recovered = RemoteOperations(Store(str(tmp_path / "commerce.db")), SimulatedPlatform(str(tmp_path / "platform.db")))
        assert recovered.reconcile(operation_id)["status"] == "ok"
        assert recovered.platform.submission_count(operation_id) == 1
        assert len(recovered.store.snapshot(LIVE)["coupons"]) == 1


def test_reconcile_cannot_create_an_unapproved_operation(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = prepare(client)
        response = client.post(f"/api/commands/{command_id}/reconcile")
        assert response.status_code == 409
        assert response.json()["code"] == "OPERATION_NOT_FOUND"
        assert app.state.remote_operations.platform.coupon_count() == 0
