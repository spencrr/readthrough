"""The two stochastic steps, retry backoff and refresh jitter, checked as distributions with a fixed seed.

Each has its own ``random.Random`` so a seed pins the sequence. The assertions are statistical: bounds
hold for every sample, and the mean lands within a margin of the expected value for the sample size.
"""

import random
import statistics
from pathlib import Path

import pytest

from readthrough.config import Config
from readthrough.faults import NetworkError, RateLimited
from readthrough.forges import JITTER_MAX, backoff
from readthrough.serve import Dashboard
from readthrough.tuning import default_tuning, load_tuning

SEED = 20261013
SAMPLES = 2000
# Uniform on [a, b] has std (b - a) / sqrt(12); five standard errors of the mean leaves a vanishing false-alarm rate.
MARGIN_SIGMAS = 5


def _mean_margin(width: float) -> float:
    return MARGIN_SIGMAS * (width / 12**0.5) / SAMPLES**0.5


def test_backoff_grows_exponentially_with_bounded_jitter():
    tuning = load_tuning({"forge": {"retry_backoff_seconds": 2.0, "retry_max_backoff_seconds": 30.0}}).forge
    rng = random.Random(SEED)
    exc = NetworkError("reset", stderr="connection reset")
    for attempt, base in ((1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 30.0), (6, 30.0)):
        waits = [backoff(attempt, tuning, exc, rng) for _ in range(SAMPLES)]
        assert min(waits) >= base and max(waits) <= base * (1 + JITTER_MAX)
        expected = base * (1 + JITTER_MAX / 2)
        assert statistics.fmean(waits) == pytest.approx(expected, abs=_mean_margin(base * JITTER_MAX))
    # Rate limits wait the configured time exactly, with no jitter.
    assert backoff(1, tuning, RateLimited("429", stderr="HTTP 429"), rng) == tuning.rate_limit_wait_seconds


def test_backoff_is_reproducible_from_a_seed():
    tuning = default_tuning().forge
    exc = NetworkError("reset", stderr="connection reset")
    first = [backoff(1, tuning, exc, random.Random(SEED)) for _ in range(3)]
    second = [backoff(1, tuning, exc, random.Random(SEED)) for _ in range(3)]
    assert first == second


def test_refresh_jitter_is_symmetric_and_bounded(tmp_path: Path):
    cfg = Config(home=tmp_path, repo="o/r", refresh_minutes=10)
    dashboard = Dashboard(cfg, with_diffs=False, seed=SEED)
    interval = cfg.refresh_minutes * 60
    spread = cfg.tuning.server.refresh_jitter
    waits = [dashboard.next_wait() for _ in range(SAMPLES)]
    assert min(waits) >= interval * (1 - spread) and max(waits) <= interval * (1 + spread)
    assert statistics.fmean(waits) == pytest.approx(interval, abs=_mean_margin(2 * spread * interval))
    # Roughly half the refreshes come early and half late: the jitter isn't biased one way.
    early = sum(w < interval for w in waits) / SAMPLES
    assert 0.45 <= early <= 0.55

    again = Dashboard(cfg, with_diffs=False, seed=SEED)
    assert [again.next_wait() for _ in range(5)] == waits[:5]
