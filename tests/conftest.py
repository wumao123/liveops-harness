import pytest

from liveops.model_budget import breaker


@pytest.fixture(autouse=True)
def fresh_breaker():
    # The circuit breaker is process-wide; a test's failures must not open it for the next test.
    breaker.reset()
    yield
    breaker.reset()
