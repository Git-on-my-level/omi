"""Privacy-safe product health metrics for real user traffic journeys."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from time import monotonic, time
from typing import Callable, Protocol, TypeVar, cast

from prometheus_client import REGISTRY, Counter, Histogram


class ProductJourney(str, Enum):
    chat_response = 'chat_response'
    realtime_pusher_session = 'realtime_pusher_session'
    capture_finalization = 'capture_finalization'


class ProductJourneyOutcome(str, Enum):
    succeeded = 'succeeded'
    failed = 'failed'


Metric = Counter | Histogram
MetricT = TypeVar('MetricT', bound=Metric)

_METRIC_CACHE_ATTRIBUTE = 'omi_product_health_metrics'
_METRIC_CACHE_LOCK_ATTRIBUTE = 'omi_product_health_metrics_lock'


class _MetricCacheLock(Protocol):
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...

    def release(self) -> None: ...


def _metric_cache() -> tuple[dict[str, object], _MetricCacheLock]:
    """Return state whose lifetime matches the public Prometheus registry."""

    metrics = getattr(REGISTRY, _METRIC_CACHE_ATTRIBUTE, None)
    if metrics is None:
        metrics = {}
        setattr(REGISTRY, _METRIC_CACHE_ATTRIBUTE, metrics)
    if not isinstance(metrics, dict):
        raise RuntimeError('Prometheus registry product-health metric cache is invalid')

    lock = getattr(REGISTRY, _METRIC_CACHE_LOCK_ATTRIBUTE, None)
    if lock is None:
        lock = RLock()
        setattr(REGISTRY, _METRIC_CACHE_LOCK_ATTRIBUTE, lock)

    return cast(dict[str, object], metrics), cast(_MetricCacheLock, lock)


def _registered_metric_or_create(name: str, factory: Callable[[], MetricT]) -> MetricT:
    """Create each product-health collector once, including after module re-imports.

    Isolated router tests unload this module but intentionally retain Prometheus's
    global registry. Keeping the handles on that registry lets a later import
    reuse the already registered collector through public APIs only.
    """

    metrics, lock = _metric_cache()
    lock.acquire()
    try:
        existing = metrics.get(name)
        if existing is not None:
            if not isinstance(existing, (Counter, Histogram)):
                raise RuntimeError(f'Prometheus registry product-health metric {name!r} has an invalid type')
            return cast(MetricT, existing)

        metric = factory()
        metrics[name] = metric
        return metric
    finally:
        lock.release()


PRODUCT_JOURNEY_ACCEPTED_TOTAL = _registered_metric_or_create(
    'omi_product_journey_accepted_total',
    lambda: Counter(
        'omi_product_journey_accepted_total',
        'Accepted real-traffic product journeys by closed journey name',
        ['journey'],
    ),
)

PRODUCT_JOURNEY_TERMINAL_TOTAL = _registered_metric_or_create(
    'omi_product_journey_terminal_total',
    lambda: Counter(
        'omi_product_journey_terminal_total',
        'Terminal real-traffic product journeys by closed journey name and outcome',
        ['journey', 'outcome'],
    ),
)

PRODUCT_JOURNEY_LATENCY_SECONDS = _registered_metric_or_create(
    'omi_product_journey_latency_seconds',
    lambda: Histogram(
        'omi_product_journey_latency_seconds',
        'End-to-end latency for terminal real-traffic product journeys',
        ['journey', 'outcome'],
        buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900, 3600),
    ),
)


def _require_journey(journey: object) -> ProductJourney:
    if not isinstance(journey, ProductJourney):
        raise ValueError('journey must be a ProductJourney')
    return journey


def _require_outcome(outcome: object) -> ProductJourneyOutcome:
    if not isinstance(outcome, ProductJourneyOutcome):
        raise ValueError('outcome must be a ProductJourneyOutcome')
    return outcome


def _record_terminal(journey: ProductJourney, outcome: ProductJourneyOutcome, latency_seconds: float) -> None:
    labels = {'journey': journey.value, 'outcome': outcome.value}
    PRODUCT_JOURNEY_TERMINAL_TOTAL.labels(**labels).inc()
    PRODUCT_JOURNEY_LATENCY_SECONDS.labels(**labels).observe(max(0.0, latency_seconds))


@dataclass
class ProductJourneyAttempt:
    """An in-memory attempt for a request or WebSocket session lifecycle."""

    journey: ProductJourney
    accepted_at_monotonic: float
    _finished: bool = False

    @classmethod
    def accepted(cls, journey: ProductJourney) -> 'ProductJourneyAttempt':
        journey = _require_journey(journey)
        PRODUCT_JOURNEY_ACCEPTED_TOTAL.labels(journey=journey.value).inc()
        return cls(journey=journey, accepted_at_monotonic=monotonic())

    def finish(self, outcome: ProductJourneyOutcome) -> None:
        outcome = _require_outcome(outcome)
        if self._finished:
            return
        self._finished = True
        _record_terminal(self.journey, outcome, monotonic() - self.accepted_at_monotonic)


def record_durable_journey_terminal(
    journey: ProductJourney,
    outcome: ProductJourneyOutcome,
    *,
    accepted_at_epoch_seconds: float,
    now_epoch_seconds: float | None = None,
) -> None:
    """Record a terminal event for a journey whose acceptance is persisted."""

    journey = _require_journey(journey)
    outcome = _require_outcome(outcome)
    terminal_at = time() if now_epoch_seconds is None else now_epoch_seconds
    _record_terminal(journey, outcome, terminal_at - accepted_at_epoch_seconds)


def finish_pusher_session_attempt(attempt: ProductJourneyAttempt, *, client_disconnect_code: int | None) -> None:
    """Finish a pusher session using the peer's close frame, when present.

    A normal close is only successful when it came from the client. Server
    timeouts and error closes have no client close frame and are failures even
    if the server happens to use close code 1000 while cleaning up.
    """

    outcome = (
        ProductJourneyOutcome.succeeded if client_disconnect_code in {1000, 1001} else ProductJourneyOutcome.failed
    )
    attempt.finish(outcome)
