"""Heavy native weights must not accumulate across OCR, embedding and ranking."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from galadril_scribe.model_residency import ModelResidency


def test_switching_models_releases_old_weights_and_reuses_current_weights() -> (
    None
):
    pool = ModelResidency()
    released: list[str] = []
    first, second = object(), object()
    for _ in range(2):
        with pool.use(first, lambda: released.append("first")):
            assert released == []
    with pool.use(second, lambda: released.append("second")):
        assert released == ["first"]
    pool.close()
    assert released == ["first", "second"]


def test_model_cannot_be_evicted_while_native_inference_is_running() -> None:
    pool = ModelResidency()
    entered, finish = Event(), Event()
    released: list[str] = []
    first, second = object(), object()

    def run() -> None:
        with pool.use(first, lambda: released.append("first")):
            entered.set()
            assert finish.wait(5)

    def switch() -> None:
        with pool.use(second, lambda: released.append("second")):
            assert released == ["first"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        inference = executor.submit(run)
        assert entered.wait(5)
        eviction = executor.submit(switch)
        assert not eviction.done()
        assert not released
        finish.set()
        inference.result(timeout=5)
        eviction.result(timeout=5)
    pool.close()


def test_failed_release_never_admits_another_model() -> None:
    pool = ModelResidency()

    def fail() -> None:
        raise RuntimeError("release failed")

    with pool.use(object(), fail):
        pass
    with pytest.raises(RuntimeError, match="release failed"):
        with pool.use(object(), lambda: None):
            pytest.fail("A second model must not be admitted")


def test_failed_close_still_clears_the_owner_and_closes_admission() -> None:
    pool = ModelResidency()

    def fail() -> None:
        raise RuntimeError("release failed")

    with pool.use(object(), fail):
        pass
    with pytest.raises(RuntimeError, match="release failed"):
        pool.close()
    assert pool._owner is None
    with pytest.raises(RuntimeError, match="closed"):
        with pool.use(object(), lambda: None):
            pytest.fail("Closed residency must reject work")


@pytest.mark.parametrize("interactive", [False, True])
def test_waiting_work_groups_by_model_but_interactive_search_takes_priority(
    interactive: bool,
) -> None:
    pool = ModelResidency()
    first, second = object(), object()
    order: list[str] = []

    def run(owner: object, name: str, priority: bool) -> None:
        with pool.use(owner, lambda: None, interactive=priority):
            order.append(name)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with pool.use(first, lambda: None):
            other = executor.submit(run, second, "other", interactive)
            same = executor.submit(run, first, "same", False)
            with pool._condition:
                assert pool._condition.wait_for(
                    lambda: len(pool._waiting) == 2, timeout=5
                )
        other.result(timeout=5)
        same.result(timeout=5)
    assert order == (["other", "same"] if interactive else ["same", "other"])
    pool.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
