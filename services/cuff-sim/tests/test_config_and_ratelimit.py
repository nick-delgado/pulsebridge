"""Start-up validation of the configuration, and the token bucket."""

import pytest
from cuff_sim.app import check_configuration, create_app
from cuff_sim.config import VARIABLES, ConfigError, ServiceConfig
from cuff_sim.ratelimit import TokenBucket

REQUIRED = [name for name, var in VARIABLES.items() if var.default is None]


def test_config_from_env_with_defaults(h):
    config = ServiceConfig.from_env(h.ENV)
    assert config.mllp_port == 2575
    assert (config.mllp_connect_timeout, config.mllp_ack_timeout) == (3.0, 5.0)
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
        ("MLLP_ACK_TIMEOUT_SECONDS", "0"),
        ("MLLP_ACK_TIMEOUT_SECONDS", "61"),
        ("MLLP_ACK_TIMEOUT_SECONDS", "inf"),
        ("MLLP_CONNECT_TIMEOUT_SECONDS", "nan"),
        ("HTTPS_TIMEOUT_SECONDS", "1e3"),
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
    ("name", "value", "names"),
    [
        ("PATIENT_FAMILY_NAME", "O\\BRIEN", "PATIENT_FAMILY_NAME"),  # HL7's escape character
        ("PATIENT_GIVEN_NAME", "Zoë", "PATIENT_GIVEN_NAME"),  # not ASCII (MSH-18 empty)
        ("SENDING_APPLICATION", "CUFF\tSIM", "SENDING_APPLICATION"),
        ("HL7V2_STORE_ID", "main/../other", "HL7V2_STORE_ID"),
        ("GCP_PROJECT_ID", "Demo", "GCP_PROJECT_ID"),
        ("GCP_PROJECT_ID", "demo", "GCP_PROJECT_ID"),  # under 6 characters: T31 refuses it
        ("AUDIT_FHIR_STORE_ID", "a b", "AUDIT_FHIR_STORE_ID"),
    ],
)
def test_start_up_check_refuses_values_that_cannot_work(h, name, value, names):
    config = ServiceConfig.from_env(dict(h.ENV, **{name: value}))
    with pytest.raises(ConfigError) as caught:
        check_configuration(config)
    assert names in str(caught.value)
    assert value not in str(caught.value)


def test_create_app_fails_on_bad_configuration(h, monkeypatch, client):
    for key, value in dict(h.ENV, PATIENT_FAMILY_NAME="O\\BRIEN").items():
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
