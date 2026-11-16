from app.cache import TTLCache


def test_a_value_is_loaded_once_while_it_is_fresh():
    cache, calls = TTLCache(ttl_seconds=60), []

    def load() -> int:
        calls.append(1)
        return len(calls)

    assert [cache.get_or_load("events", load) for _ in range(3)] == [1, 1, 1]
    assert cache.get_or_load("other", load) == 2  # keys are independent


def test_a_stale_value_is_loaded_again(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.cache.time.monotonic", lambda: clock[0])
    cache, calls = TTLCache(ttl_seconds=1), []

    def load() -> int:
        calls.append(1)
        return len(calls)

    assert cache.get_or_load("events", load) == 1
    clock[0] += 0.5
    assert cache.get_or_load("events", load) == 1
    clock[0] += 1
    assert cache.get_or_load("events", load) == 2


def test_a_zero_ttl_disables_the_cache():
    cache, calls = TTLCache(ttl_seconds=0), []

    assert [cache.get_or_load("events", lambda: calls.append(1) or len(calls)) for _ in range(2)] == [1, 2]


def test_a_failed_load_is_not_cached():
    cache = TTLCache(ttl_seconds=60)

    def fail() -> int:
        raise LookupError("no such event")

    for _ in range(2):
        try:
            cache.get_or_load("missing", fail)
        except LookupError:
            pass
    assert cache.get_or_load("missing", lambda: 7) == 7


def test_the_cache_stays_bounded():
    cache = TTLCache(ttl_seconds=60, max_entries=3)

    for key in range(10):
        cache.get_or_load(key, lambda key=key: key)

    assert len(cache._entries) <= 3
