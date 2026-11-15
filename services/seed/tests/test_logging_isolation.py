"""The seed tests leave the root logger as they found it (T35).

Before, a CLI test left pulsebridge-seed's JSON handler on the root logger, bound to that
test's captured stderr, and ``pytest services/seed services/health-sync`` failed a
health-sync test that checks its logs carry no exception text.
"""

import logging

from pulsebridge_seed.cli import EXIT_OK, main
from pulsebridge_seed.testing import logging_restored


def _state():
    root = logging.getLogger()
    return list(root.handlers), root.level, logging.getLogger("urllib3").level


def test_a_cli_run_adds_a_root_handler_and_logging_restored_removes_it(monkeypatch, capsys):
    monkeypatch.setenv("PATIENT_ID", "SYN-TEST-0001")
    monkeypatch.setenv("TIME_ZONE", "America/New_York")
    logging.getLogger("urllib3").setLevel(logging.NOTSET)
    before = _state()
    with logging_restored():
        assert main(["--dry-run", "--days", "1"]) == EXIT_OK
        # The command did configure logging: this is the state that must not leak.
        assert len(logging.getLogger().handlers) == len(before[0]) + 1
        assert logging.getLogger("urllib3").level == logging.WARNING
    assert _state() == before
    capsys.readouterr()


def test_no_root_handler_is_left_by_the_tests_before_this_one():
    # The autouse fixture (tests/conftest.py) restores after every test, so no handler of
    # pulsebridge-seed's formatter is on the root logger between tests.
    from pulsebridge_seed.logs import JsonFormatter

    assert not [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
