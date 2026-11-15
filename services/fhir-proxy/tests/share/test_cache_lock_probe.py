"""The access-token cache, the per-instance share lock, and the status strip's OpenEMR probe."""

from __future__ import annotations

import threading
import time

from fhir_proxy.share.document import ClientDocument
from fhir_proxy.share.oauth import TOKEN_MARGIN_SECONDS, TokenAnswer, TokenCache
from fhir_proxy.share.testing import BP_ID, HR_ID, raise_timeout
from fhir_proxy.share.transport import Answer
from fhir_proxy.testing import Clock


def token_requests(env) -> int:  # noqa: ANN001
    return len(env.openemr.calls("POST", r"/token$"))


def test_the_token_is_reused_within_its_lifetime_and_refreshed_after(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    env.clock.advance(300 - TOKEN_MARGIN_SECONDS - 1)
    body = env.share(observation_id=HR_ID).get_json()
    assert token_requests(env) == 1
    token = body["steps"][0]
    assert token["step"] == "token" and token["result"] == "reused"
    assert token["response"]["status"] == 0 and token["request"]["body"] is None
    assert "no token request was sent" in token["label"]
    env.clock.advance(2)  # now within the margin: a new token is fetched
    assert env.share(observation_id=HR_ID).status_code == 200
    assert token_requests(env) == 2


def doc(client_id: str, key) -> ClientDocument:  # noqa: ANN001
    return ClientDocument(
        client_id=client_id,
        kid="k",
        scope="s",
        token_url="https://10.0.0.1/t",  # noqa: S106 - a URL
        certificate_pem="",
        private_key=key,
    )


def test_tokens_are_kept_per_client_id(client_key) -> None:
    clock = Clock()
    cache = TokenCache(clock.monotonic)
    cache.put(doc("client-one-0000000", client_key), TokenAnswer("synthetic-token-one", 300))
    assert cache.get(doc("client-two-0000000", client_key)) is None
    assert cache.get(doc("client-one-0000000", client_key))[0] == "synthetic-token-one"
    cache.forget("client-one-0000000")
    assert cache.get(doc("client-one-0000000", client_key)) is None


def test_a_token_too_short_lived_to_reuse_is_not_cached(client_key) -> None:
    cache = TokenCache(Clock().monotonic)
    cache.put(doc("client-one-0000000", client_key), TokenAnswer("t", TOKEN_MARGIN_SECONDS))
    assert cache.get(doc("client-one-0000000", client_key)) is None


def test_the_token_is_never_in_a_repr(client_key) -> None:
    cache = TokenCache(Clock().monotonic)
    cache.put(doc("client-one-0000000", client_key), TokenAnswer("synthetic-secret-token", 300))
    assert "synthetic-secret-token" not in repr(cache._tokens)  # noqa: SLF001
    assert "PRIVATE" not in repr(doc("c" * 16, client_key))


def test_the_cache_is_thread_safe(client_key) -> None:
    import sys

    clock = Clock()
    cache = TokenCache(clock.monotonic)
    errors = []
    # Expired entries are removed by get(): many threads racing on the same expired key would
    # remove it twice without the lock.
    expiring = doc("client-race-00000000", client_key)

    def race() -> None:
        try:
            for _ in range(2000):
                cache.put(expiring, TokenAnswer("t", TOKEN_MARGIN_SECONDS + 1))
                clock.mono += 2
                cache.get(expiring)
                clock.mono -= 2
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        racers = [threading.Thread(target=race) for _ in range(8)]
        for t in racers:
            t.start()
        for t in racers:
            t.join()
    finally:
        sys.setswitchinterval(old)

    def work(n: int) -> None:
        try:
            for i in range(300):
                d = doc(f"client-{n % 3}-000000000", client_key)
                cache.put(d, TokenAnswer(f"t-{n}-{i}", 300))
                got = cache.get(d)
                assert got is not None and got[0].startswith("t-")
                if i % 50 == 0:
                    cache.forget(d.client_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_shares_are_serialised_on_one_instance(env) -> None:
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    original = env.openemr._route  # noqa: SLF001

    def slow(method, path, query, headers, body):  # noqa: ANN001, ANN202
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        try:
            return original(method, path, dict(query), headers, body)
        finally:
            with lock:
                active["now"] -= 1

    env.openemr.override("POST", r"/Observation$", slow)
    env.openemr.override("GET", r"/Patient$", slow)
    token = env.token()
    results = []

    def share(observation_id: str) -> None:
        results.append(env.share(observation_id=observation_id, token=token).status_code)

    threads = [threading.Thread(target=share, args=(i,)) for i in (BP_ID, HR_ID, BP_ID, HR_ID)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 200, 200, 200]
    assert active["max"] == 1  # never two shares talking to OpenEMR at once
    # Serialised, so the first share's Patient is found by the others: exactly one Patient.
    assert len(env.openemr.patients) == 1 and len(env.openemr.encounters) == 1


def test_a_share_that_cannot_get_the_lock_in_time_is_busy_and_sends_nothing(
    env, monkeypatch
) -> None:
    from fhir_proxy.share import service

    monkeypatch.setattr(service, "LOCK_WAIT_SECONDS", 0.2)
    env.service.lock.acquire()  # another share holds it
    try:
        began = time.monotonic()
        response = env.share()
        assert time.monotonic() - began < 2
    finally:
        env.service.lock.release()
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "share-busy"
    assert env.openemr.requests == []


def test_the_lock_is_released_after_a_failure(env) -> None:
    env.openemr.override("POST", r"/Observation$", raise_timeout, times=1)
    assert env.share().status_code == 504
    assert env.service.lock.acquire(blocking=False)
    env.service.lock.release()
    assert env.share().status_code == 200


# ---- the probe -------------------------------------------------------------------------------


def status(env) -> dict:  # noqa: ANN001
    response = env.harness.get("/status")
    assert response.status_code == 200
    return response.get_json()["openemr"]


def test_the_probe_asks_readiness_without_a_token_and_is_cached(env) -> None:
    assert status(env) == {"reachable": True, "checkedAt": "2026-01-02T12:00:00Z", "detail": None}
    (call,) = env.openemr.requests
    assert (call["method"], call["path"]) == ("GET", "/meta/health/readyz")
    assert "authorization" not in call["headers"]
    status(env)
    assert len(env.openemr.requests) == 1  # cached
    env.clock.advance(31)
    status(env)
    assert len(env.openemr.requests) == 2
    assert env.transport.contexts[-1].check_hostname is True


def test_the_probe_says_not_ready_for_another_answer(env) -> None:
    env.openemr.override("GET", r"readyz", Answer(503, {}, b"starting"))
    assert status(env) == {
        "reachable": False,
        "checkedAt": "2026-01-02T12:00:00Z",
        "detail": "not-ready",
    }


def test_the_probe_timing_out_is_timeout_never_a_5xx(env) -> None:
    env.openemr.override("GET", r"readyz", raise_timeout)
    assert status(env)["detail"] == "timeout"


def test_the_probe_with_a_bad_document(env) -> None:
    env.doc_path.write_text("{}")
    assert status(env)["detail"] == "client-document-invalid"


def test_status_never_5xx_when_the_probe_raises_or_hangs(env, monkeypatch) -> None:
    status_service = env.harness.app.extensions["pulsebridge_status"]

    def broken() -> dict:
        raise RuntimeError("probe bug")

    status_service.openemr_probe = broken
    assert status(env) == {
        "reachable": False,
        "checkedAt": "2026-01-02T12:00:00Z",
        "detail": "error",
    }
    status_service.openemr_probe = lambda: {"reachable": "yes"}
    assert status(env)["detail"] == "error"

    def hangs() -> dict:
        time.sleep(3)
        return {"reachable": True, "checkedAt": "x", "detail": None}

    status_service.openemr_probe = hangs
    from fhir_proxy import deadline

    real_start = deadline.start
    monkeypatch.setattr(deadline, "start", lambda seconds, *a: real_start(1.0, *a))
    began = time.monotonic()
    result = status(env)
    assert time.monotonic() - began < 2.5
    assert result == {"reachable": False, "checkedAt": "2026-01-02T12:00:00Z", "detail": "timeout"}
