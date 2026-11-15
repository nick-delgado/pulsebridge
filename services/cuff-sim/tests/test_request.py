"""Request validation: bounds, types, transports and sample names. Nothing here sends anything."""

import pytest
from cuff_sim.request import RequestError, parse_request


def fields_of(exc: RequestError) -> list[str]:
    return [name for name, _ in exc.issues]


def test_valid_reading_defaults_to_mllp():
    request = parse_request({"systolic": 120, "diastolic": 80})
    assert (request.transport, request.systolic, request.diastolic) == ("mllp", 120, 80)
    assert request.sample_name is None


def test_https_and_explicit_null_sample():
    request = parse_request(
        {"systolic": 120, "diastolic": 80, "transport": "https", "sendMalformed": None}
    )
    assert request.transport == "https" and request.sample_name is None


@pytest.mark.parametrize(
    ("systolic", "diastolic"), [(1, 0), (401, 80), (120, 0), (0, 0), (-5, -10), (401, 400)]
)
def test_out_of_bounds_is_refused(systolic, diastolic):
    with pytest.raises(RequestError) as caught:
        parse_request({"systolic": systolic, "diastolic": diastolic})
    assert caught.value.status == 422


@pytest.mark.parametrize(("systolic", "diastolic"), [(2, 1), (400, 1), (400, 399)])
def test_bounds_are_inclusive(systolic, diastolic):
    assert parse_request({"systolic": systolic, "diastolic": diastolic}).systolic == systolic


@pytest.mark.parametrize("bad", [120.0, "120", True, False, [120], {"v": 120}, 1e3])
@pytest.mark.parametrize("name", ["systolic", "diastolic"])
def test_non_integers_are_refused(name, bad):
    body = {"systolic": 120, "diastolic": 80, name: bad}
    with pytest.raises(RequestError) as caught:
        parse_request(body)
    # The type error itself, not a side effect such as True (== 1) failing systolic > diastolic.
    assert caught.value.issues == [(name, "must be a whole number of mm[Hg]")]


@pytest.mark.parametrize(("systolic", "diastolic"), [(80, 80), (80, 120)])
def test_systolic_must_be_above_diastolic(systolic, diastolic):
    with pytest.raises(RequestError) as caught:
        parse_request({"systolic": systolic, "diastolic": diastolic})
    assert caught.value.issues == [("systolic", "must be greater than diastolic")]


def test_missing_values_are_required_without_a_sample():
    with pytest.raises(RequestError) as caught:
        parse_request({"transport": "mllp"})
    assert fields_of(caught.value) == ["systolic", "diastolic"]


@pytest.mark.parametrize("transport", ["tcp", "MLLP", "", None, 1])
def test_unknown_transport_is_refused(transport):
    with pytest.raises(RequestError) as caught:
        parse_request({"systolic": 120, "diastolic": 80, "transport": transport})
    assert "transport" in fields_of(caught.value)


def test_extra_fields_are_refused_so_no_free_text_gets_in():
    with pytest.raises(RequestError) as caught:
        parse_request({"systolic": 120, "diastolic": 80, "patientName": "Robert'); DROP"})
    assert fields_of(caught.value) == ["body"]
    assert "DROP" not in str(caught.value)


@pytest.mark.parametrize("body", [[], "x", 5, None])
def test_body_must_be_an_object(body):
    with pytest.raises(RequestError) as caught:
        parse_request(body)
    assert caught.value.status == 400


def test_sample_needs_no_values():
    request = parse_request({"sendMalformed": "unparseable_no_msh"})
    assert request.sample_name == "unparseable_no_msh" and request.systolic is None


def test_sample_values_are_still_checked_when_present():
    with pytest.raises(RequestError):
        parse_request({"sendMalformed": "unparseable_no_msh", "systolic": "high"})


@pytest.mark.parametrize("name", ["no_such_sample", "valid_bp_reading", 7, "", "MSH|^~\\&"])
def test_unknown_or_valid_samples_are_refused(name):
    with pytest.raises(RequestError) as caught:
        parse_request({"sendMalformed": name})
    assert fields_of(caught.value) == ["sendMalformed"]


@pytest.mark.parametrize("name", ["noack_missing_end_block", "noack_missing_final_cr"])
def test_framing_samples_do_not_apply_over_https(name):
    assert parse_request({"sendMalformed": name}).sample_name == name  # fine over MLLP
    with pytest.raises(RequestError) as caught:
        parse_request({"sendMalformed": name, "transport": "https"})
    assert caught.value.issues == [("sendMalformed", "this sample cannot be sent over https")]


def test_other_samples_apply_over_https():
    request = parse_request({"sendMalformed": "unparseable_lf_terminators", "transport": "https"})
    assert request.sample_name == "unparseable_lf_terminators"


@pytest.mark.parametrize(
    "name", ["UNPARSEABLE_NO_MSH", "Unparseable_No_Msh", "unparseable_no_msh "]
)
def test_sample_names_match_exactly_in_the_request(name):
    with pytest.raises(RequestError) as caught:
        parse_request({"sendMalformed": name})
    assert caught.value.issues == [
        ("sendMalformed", "must be the name of a malformed sample, or null")
    ]


def test_find_sample_matches_names_exactly():
    from cuff_sim.message import find_sample

    assert find_sample("unparseable_no_msh", "mllp").name == "unparseable_no_msh"
    for name in ("UNPARSEABLE_NO_MSH", "Unparseable_No_Msh", " unparseable_no_msh"):
        assert find_sample(name, "mllp") is None
