from datetime import timedelta

from galadril_vision.causal.runner import (  # type: ignore
    _make_cache_key,
    build_slice_spec_from_step_params,
)


class _Params:
    def __init__(self) -> None:
        self.lookback = "7d"
        self.bucket = "1h"
        self.max_events = 100
        self.max_states = 200
        self.target = "global"
        self.state_metrics = [
            {
                "metric_id": "m1",
                "expr_sql": "1::double precision",
                "agg_sql": "AVG",
            }
        ]


def test_cache_key_deterministic() -> None:
    payload = {"b": 2, "a": 1}
    assert _make_cache_key(payload) == _make_cache_key(payload)


def test_build_slice_spec_parses_mapping() -> None:
    p = _Params()
    spec = build_slice_spec_from_step_params(p)
    assert spec.lookback == timedelta(days=7)
    assert spec.bucket == "1h"
    assert spec.max_events == 100
    assert spec.max_states == 200
    assert spec.target == "global"
    assert len(spec.state_metrics) == 1
    assert spec.state_metrics[0].metric_id == "m1"
