"""Start-up validation of the configuration, and the token bucket."""

import os
import subprocess
import sys
import threading
import time
import traceback

import pytest
from cuff_sim.app import check_configuration, create_app
from cuff_sim.config import TEXT_LIMITS, VARIABLES, ConfigError, ServiceConfig
from cuff_sim.ratelimit import TokenBucket

REQUIRED = [name for name, var in VARIABLES.items() if var.default is None]


def test_config_from_env_with_defaults(h):
    config = ServiceConfig.from_env(h.ENV)
    assert config.mllp_port == 2575
    assert config.mllp_timeout == 5.0
    assert config.https_timeout == 10.0
    assert (config.sending_application, config.sending_facility) == ("CUFFSIM", "PULSEBRIDGE")
    assert (config.rate_limit_per_minute, config.rate_limit_burst) == (6, 3)
    check_configuration(config)


def test_required_variables_are_the_deployment_specific_ones():
    assert sorted(REQUIRED) == sorted(
        [
            "GCP_PROJECT_ID",
            "HEALTHCARE_LOCATION",
            "HEALTHCARE_DATASET_ID",
            "HL7V2_STORE_ID",
            "AUDIT_FHIR_STORE_ID",
            "MLLP_HOST",
            "PATIENT_ID",
            "PATIENT_ASSIGNING_AUTHORITY",
            "PATIENT_FAMILY_NAME",
            "PATIENT_GIVEN_NAME",
        ]
    )


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_variable_is_checked(h, name):
    env = dict(h.ENV)
    env[name] = "  "
    with pytest.raises(ConfigError, match=name):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MLLP_PORT", "0"),
        ("MLLP_PORT", "65536"),
        ("MLLP_PORT", "25x"),
        ("MLLP_TIMEOUT_SECONDS", "0"),
        ("MLLP_TIMEOUT_SECONDS", "61"),
        ("MLLP_TIMEOUT_SECONDS", "inf"),
        ("MLLP_TIMEOUT_SECONDS", "nan"),
        ("HTTPS_TIMEOUT_SECONDS", "1e3"),
        ("HTTPS_TIMEOUT_SECONDS", "1e1"),  # float() reads 10.0: in range, but not plain decimal
        ("MLLP_TIMEOUT_SECONDS", "1_0"),  # float() reads 10.0 too
        ("RATE_LIMIT_PER_MINUTE", "0"),
        ("RATE_LIMIT_PER_MINUTE", "601"),
        ("RATE_LIMIT_BURST", "-1"),
        ("MLLP_HOST", "adapter host"),
        ("MLLP_HOST", "http://10.0.0.2"),
        ("MLLP_HOST", "-bad.example"),
    ],
)
def test_bad_values_fail_and_the_value_is_not_echoed(h, name, value):
    env = dict(h.ENV, **{name: value})
    with pytest.raises(ConfigError) as caught:
        ServiceConfig.from_env(env)
    assert name in str(caught.value)
    if len(value) > 2:  # "0" would match the message's own "above 0"
        assert value not in str(caught.value)


@pytest.mark.parametrize("host", ["10.0.0.2", "fd00::2", "edge-vm.internal", "localhost"])
def test_hosts_that_are_accepted(h, host):
    assert ServiceConfig.from_env(dict(h.ENV, MLLP_HOST=host)).mllp_host == host


def test_audit_store_must_differ_from_hl7v2_store(h):
    with pytest.raises(ConfigError, match="AUDIT_FHIR_STORE_ID"):
        ServiceConfig.from_env(dict(h.ENV, AUDIT_FHIR_STORE_ID="main"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PATIENT_FAMILY_NAME", "O\\BRIEN"),  # HL7's escape character
        ("PATIENT_GIVEN_NAME", "Zo\u00eb"),  # not ASCII (MSH-18 is empty)
        ("SENDING_APPLICATION", "CUFF\tSIM"),
    ],
)
def test_hl7_text_rules_are_checked_when_reading_the_environment(h, name, value):
    with pytest.raises(ConfigError) as caught:
        ServiceConfig.from_env(dict(h.ENV, **{name: value}))
    assert name in str(caught.value) and value not in str(caught.value)


@pytest.mark.parametrize(("name", "limit"), sorted(TEXT_LIMITS.items()))
def test_text_length_limits(h, name, limit):
    at_limit = ServiceConfig.from_env(dict(h.ENV, **{name: "A" * limit}))
    check_configuration(at_limit)  # T07, hl7apy and T31 accept the longest allowed value
    with pytest.raises(ConfigError) as caught:
        ServiceConfig.from_env(dict(h.ENV, **{name: "B" * (limit + 1)}))
    assert name in str(caught.value) and "BBBB" not in str(caught.value)


# Values that pass the environment checks but that a library refuses at start-up. The library's
# own error quotes the value; none of it may reach the ConfigError or its traceback.
LIBRARY_REFUSALS = [
    ("HL7V2_STORE_ID", "main/../other-zq7"),
    ("GCP_PROJECT_ID", "Demo-Zq7"),
    ("GCP_PROJECT_ID", "dzq7"),  # under 6 characters: T31 refuses it
    ("AUDIT_FHIR_STORE_ID", "a b zq7"),
    ("HEALTHCARE_DATASET_ID", "d" * 300 + "zq7"),
]


@pytest.mark.parametrize(("name", "value"), LIBRARY_REFUSALS)
def test_start_up_errors_and_tracebacks_hold_no_configured_value(h, name, value):
    config = ServiceConfig.from_env(dict(h.ENV, **{name: value}))
    with pytest.raises(ConfigError) as caught:
        check_configuration(config)
    printed = "".join(traceback.format_exception(caught.value))
    assert name in printed
    assert "zq7" not in printed  # the marker in every bad value
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_process_start_up_output_holds_no_configured_value(h, tmp_path):
    # The real start-up, as gunicorn would run it: a fresh interpreter, environment only.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE", "GCP", "CLOUDSDK"))}
    env.update(h.ENV, HEALTHCARE_DATASET_ID="demo-zq7-" + "x" * 300, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-B", "-c", "from cuff_sim.app import create_app; create_app()"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "ConfigError" in proc.stderr
    assert "zq7" not in proc.stderr + proc.stdout


def test_create_app_fails_on_bad_configuration(h, monkeypatch, client):
    for key, value in dict(h.ENV, GCP_PROJECT_ID="Demo").items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError):
        create_app(client=client)


def test_create_app_reads_the_environment(h, monkeypatch, client):
    for key, value in h.ENV.items():
        monkeypatch.setenv(key, value)
    assert create_app(client=client).test_client().get("/cuff/samples").status_code == 200


# ---- Token bucket ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


def test_burst_then_refill():
    clock = Clock()
    bucket = TokenBucket(per_minute=6, burst=3, clock=clock)
    assert [bucket.take() for _ in range(3)] == [None, None, None]
    assert bucket.take() == 10  # one token per 10 s
    clock.now += 4
    assert bucket.take() == 6
    clock.now += 6
    assert bucket.take() is None
    assert bucket.take() == 10


def test_refill_stops_at_the_burst_size():
    clock = Clock()
    bucket = TokenBucket(per_minute=60, burst=2, clock=clock)
    clock.now += 3600
    assert [bucket.take() for _ in range(3)] == [None, None, 1]


def test_retry_after_is_at_least_one_second():
    clock = Clock()
    bucket = TokenBucket(per_minute=600, burst=1, clock=clock)
    bucket.take()
    clock.now += 0.09
    assert bucket.take() == 1


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (1, 0)])
def test_bucket_needs_positive_limits(rate, burst):
    with pytest.raises(ValueError):
        TokenBucket(per_minute=rate, burst=burst)


class _SlowNow(float):
    """A clock reading whose subtraction yields to other threads. ``take`` computes the refill
    as ``tokens + (now - updated) * rate``: the pause falls inside that read-modify-write of the
    token count, so without the lock another thread is sure to run in the middle of it."""

    def __sub__(self, other):
        time.sleep(0.0005)
        return float(self) - float(other)


def test_bucket_is_safe_across_threads():
    bucket = TokenBucket(per_minute=1, burst=40, clock=lambda: _SlowNow(1000.0))
    allowed = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            if bucket.take() is None:
                with lock:
                    allowed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 40  # 160 tries, exactly the burst allowed


def test_any_library_error_in_the_message_dry_run_is_a_clean_config_error(h, monkeypatch):
    # The configuration limits make this unreachable today; it guards against a library that
    # becomes stricter. hl7apy's own errors quote the value, as this stand-in does.
    from cuff_sim import app as app_module

    def strict(*_a, **_k):
        raise LookupError("value 'TESTPATIENT-zq7' is too long")

    monkeypatch.setattr(app_module, "build_reading", strict)
    with pytest.raises(ConfigError) as caught:
        check_configuration(ServiceConfig.from_env(h.ENV))
    printed = "".join(traceback.format_exception(caught.value))
    assert "PATIENT_FAMILY_NAME" in printed and "zq7" not in printed
