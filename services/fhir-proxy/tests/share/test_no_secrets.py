"""The private key, the signed assertion, the access token and the patient's details never
appear in a log line (JSON or with a traceback), and the credentials never in an answer."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

from fhir_proxy.share import service as share_service
from fhir_proxy.share.testing import (
    BP_ID,
    CLIENT_ID,
    HR_ID,
    KID,
    raise_timeout,
    raise_unreachable,
)


def secrets_of(env) -> list[str]:  # noqa: ANN001
    doc = json.loads(env.doc_path.read_text())
    key_lines = [line for line in doc["private_key"].splitlines() if "-----" not in line]
    assertions = [
        dict(parse_qsl(r["body"].decode()))["client_assertion"]
        for r in env.openemr.calls("POST", r"/token$")
    ]
    tokens = list(env.openemr.tokens)
    assert key_lines and assertions and tokens
    # Pieces too: a log line that quoted part of a JWT is still a leak.
    pieces = [a.split(".")[1] for a in assertions] + [a.split(".")[2] for a in assertions]
    return key_lines + assertions + pieces + tokens


def test_nothing_secret_or_personal_in_logs_or_answers_on_any_path(env, logs, monkeypatch) -> None:
    answers = []
    answers.append(env.share(observation_id=BP_ID))  # token fetched, Patient created
    answers.append(env.share(observation_id=HR_ID))  # cached token
    answers.append(env.share(observation_id=BP_ID))  # already shared
    env.openemr.override("POST", r"/Observation$", raise_timeout, times=1)
    answers.append(env.share(observation_id=HR_ID))
    env.openemr.override("GET", r"/Patient$", raise_unreachable, times=1)
    answers.append(env.share(observation_id=HR_ID))
    env.clock.advance(1000)  # token expired: a new assertion and token
    env.openemr.override(
        "GET",
        r"/Encounter$",
        lambda *a: (_ for _ in ()).throw(
            ValueError(f"a bug quoting {env.openemr.tokens and next(iter(env.openemr.tokens))}")
        ),
        times=1,
    )
    answers.append(env.share(observation_id=HR_ID))
    env.openemr.clients.clear()  # invalid_client, document re-read
    answers.append(env.share(observation_id=HR_ID))

    # A bug inside the share's own code, with the key in its message, logged with a traceback.
    def leaky(*_: object) -> dict:
        raise RuntimeError(json.loads(env.doc_path.read_text())["private_key"])

    monkeypatch.setattr(share_service.resources, "encounter_for_openemr", leaky)
    env.openemr.register(CLIENT_ID, env.key, KID, " ".join(env.settings.scopes))
    answers.append(env.share(observation_id="pb-internal-obs-bp-1"))

    assert {a.status_code for a in answers} >= {200, 502, 504}
    log_text = logs.text()
    answer_text = "\n".join(a.get_data(as_text=True) for a in answers)
    for secret in secrets_of(env):
        assert secret not in log_text
        assert secret not in answer_text
    assert "PRIVATE KEY" not in answer_text and "PRIVATE KEY" not in log_text
    assert "CERTIFICATE" not in answer_text  # the document is never in a trace
    for personal in ("PB-SYN-0001", "Avery", "1980-04-12", "synthetic-health-user-0001"):
        assert personal not in log_text
    # The events that were written carry codes only.
    events = {e.get("event") for e in logs.events()}
    assert {"share-finished", "share-failed"} <= events
