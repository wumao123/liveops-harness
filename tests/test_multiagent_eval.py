from liveops.multiagent_eval import FAULTS, corrupt, run_experiments


def test_corruption_does_not_mutate_original():
    original = {"claims": ["valid"], "price_cents": 10, "stock": 1, "source": "x",
                "sku": "s", "title": "t", "product_version": 0}
    for fault in FAULTS:
        assert corrupt(original, fault) != original
    assert original["claims"] == ["valid"]


def test_offline_experiment_contracts():
    result = run_experiments(repeats=1, delay=0)
    assert result["llm_calls"] == 0
    assert result["review"]["bad_fixtures"] == 45
    assert result["review"]["review_off_bad_admitted"] == 45
    assert result["review"]["review_on_bad_admitted"] == 0
    assert result["review"]["review_on_good_admitted"] == 5
    assert result["context"]["task_packets"] == 40
    assert result["context"]["scoped_cost_exposure_packets"] == 0
    assert result["context"]["product_scope_correct"] == 30
    assert result["boundaries"]["unauthorized_probes"] == 20
    assert result["boundaries"]["unauthorized_allowed"] == 0
    assert result["boundaries"]["permanent_failure"]["failed_task_attempts"] == 2
    assert result["boundaries"]["step_budget"]["steps"] == 2
    assert result["plans"]["rejected"] == 6
    for row in result["recovery"]["summary"]:
        assert row["recovered"] == 1
        assert row["completed_tasks_reexecuted"] == 0
