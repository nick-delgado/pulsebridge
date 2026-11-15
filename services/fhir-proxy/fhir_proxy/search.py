"""``GET /fhir/Observation``: the only FHIR search the proxy offers, and its strict validation.

The brief puts "full FHIR search" out of scope: the proxy supports only the parameters the
dashboard uses and declares them in its CapabilityStatement. Everything else is refused with a
``400`` OperationOutcome, never passed through to the store, because the store would happily
answer a much wider search (another patient, chained parameters, ``_include`` of other resource
types) with the proxy's own credentials.

Accepted (R4 search.html; Observation search parameters):

- ``code`` (token): exactly ``http://loinc.org|8867-4`` (heart rate) or
  ``http://loinc.org|85354-9`` (blood-pressure panel); one value, no ``,`` OR list;
- ``date`` (date): at most one lower bound (``gt`` or ``ge``) and one upper bound (``lt`` or
  ``le``), each a full instant with seconds and a time zone;
- ``_sort``: ``date`` (oldest first) or ``-date``; ``_count``: 1 to 1000;
- ``_page_token``: only as written by the proxy's own ``next`` link.

The patient is **not** a parameter: every search is pinned to the configured patient (README,
open question 1). ``patient``, ``subject``, a modifier (``code:text``), a chain
(``subject.name``), ``_include``/``_revinclude``, ``_elements`` and the rest are refused by name.

**The date rule for heart-rate windows** (open question 5). R4 defines a prefix on a Period as:
``gt``: "the range above the search value intersects (i.e. overlaps) with the range of the
target value" and ``lt`` likewise below, which for ``effectivePeriod`` means ``gt<v>`` matches a
window that ends after ``v`` and ``lt<v>`` one that starts before ``v``. So ``date=gt<start>&
date=lt<end>`` returns the windows that overlap the open interval (start, end), and the window
that ends exactly at ``start`` is not returned: an aligned 24 hours gives 288 windows.
:func:`matches_dates` applies exactly that rule to every Observation the store returns, so the
dashboard gets the R4 answer even if the store's own date matching on a Period differs
(unverified on Cloud Healthcare: README "Not verified").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from fhir_proxy.errors import Problem

LOINC = "http://loinc.org"
HEART_RATE = "8867-4"
BLOOD_PRESSURE_PANEL = "85354-9"
CODES = (HEART_RATE, BLOOD_PRESSURE_PANEL)
LOWER_PREFIXES = ("gt", "ge")
UPPER_PREFIXES = ("lt", "le")
MAX_COUNT = 1000
DEFAULT_COUNT = 100
SORTS = ("date", "-date")

# The names the search accepts, in the order the CapabilityStatement lists them.
SEARCH_PARAMETERS: dict[str, str] = {
    "code": "token",
    "date": "date",
    "_sort": "special",
    "_count": "number",
}
# Paging is a parameter of the proxy's own links, not a search criterion; declared separately.
PAGE_TOKEN = "_page_token"  # noqa: S105 - a parameter name, not a secret

# A full FHIR instant: date, time to the second, optional fraction, and a zone (R4 instant).
_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
# The store's page tokens are opaque; this is wide enough for base64 and URL-safe text.
_PAGE_TOKEN = re.compile(r"[A-Za-z0-9_\-=.~]{1,2048}", re.ASCII)
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}", re.ASCII)
MAX_QUERY_PAIRS = 12


@dataclass(frozen=True)
class DateBound:
    prefix: str
    value: datetime
    text: str  # as the caller wrote it (validated), passed to the store unchanged


@dataclass(frozen=True)
class ObservationQuery:
    code: str
    lower: DateBound | None
    upper: DateBound | None
    sort: str
    count: int
    page_token: str | None

    def store_params(self, subject: str) -> list[tuple[str, str]]:
        """The store's query: ours, plus the pinned ``subject``. Never anything from the caller
        that was not validated above."""
        params = [("subject", subject), ("code", f"{LOINC}|{self.code}")]
        for bound in (self.lower, self.upper):
            if bound is not None:
                params.append(("date", bound.prefix + bound.text))
        params += [("_sort", self.sort), ("_count", str(self.count))]
        if self.page_token is not None:
            params.append((PAGE_TOKEN, self.page_token))
        return params

    def own_params(self, page_token: str | None) -> list[tuple[str, str]]:
        """The query of a link back to the proxy (``self`` or ``next``)."""
        params = [("code", f"{LOINC}|{self.code}")]
        for bound in (self.lower, self.upper):
            if bound is not None:
                params.append(("date", bound.prefix + bound.text))
        params += [("_sort", self.sort), ("_count", str(self.count))]
        if page_token is not None:
            params.append((PAGE_TOKEN, page_token))
        return params


def _refuse(text: str, *, expression: list[str] | None = None, reason: str) -> Problem:
    return Problem(400, "not-supported", reason, text, expression=expression)


def _name_for_message(name: str) -> str:
    """A parameter name is repeated in the answer only when it is plainly a name."""
    return name if _SAFE_NAME.fullmatch(name) else "(a parameter)"


def parse_instant(text: str) -> datetime | None:
    if not _INSTANT.fullmatch(text):
        return None
    try:
        # Python 3.13 reads "Z" and up to 9 fraction digits are not accepted, so cut to 6.
        head, sep, tail = text.partition(".")
        if sep:
            digits = re.match(r"[0-9]+", tail).group(0)  # type: ignore[union-attr]
            text = f"{head}.{digits[:6]}{tail[len(digits) :]}"
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def parse_observation_query(pairs: Sequence[tuple[str, str]]) -> ObservationQuery:
    """Validate the caller's query string (as decoded name/value pairs, repeats kept)."""
    if len(pairs) > MAX_QUERY_PAIRS:
        raise _refuse("Too many search parameters.", reason="too-many-parameters")
    code = None
    lower = upper = None
    sort = "date"
    count = DEFAULT_COUNT
    page_token = None
    seen: set[str] = set()
    for name, value in pairs:
        if name not in SEARCH_PARAMETERS and name != PAGE_TOKEN:
            shown = _name_for_message(name)
            if ":" in name or "." in name:
                why = "modifiers and chained parameters are not supported"
            elif name in ("patient", "subject"):
                why = "the patient is fixed by the proxy"
            else:
                why = "it is not a supported search parameter"
            raise _refuse(
                f"Search parameter {shown} is refused: {why}. Supported: code, date, _sort, "
                "_count.",
                expression=[shown],
                reason="unsupported-parameter",
            )
        if name in seen and name != "date":
            raise _refuse(
                f"Search parameter {name} may appear only once.",
                expression=[name],
                reason="repeated-parameter",
            )
        seen.add(name)
        if name == "code":
            if "," in value:
                raise _refuse(
                    "code takes one value; OR lists are not supported.",
                    expression=["code"],
                    reason="unsupported-value",
                )
            system, bar, code_value = value.partition("|")
            if not bar or system != LOINC or code_value not in CODES:
                raise _refuse(
                    "code must be http://loinc.org|8867-4 or http://loinc.org|85354-9.",
                    expression=["code"],
                    reason="unsupported-value",
                )
            code = code_value
        elif name == "date":
            prefix, text = value[:2], value[2:]
            instant = parse_instant(text)
            if prefix not in LOWER_PREFIXES + UPPER_PREFIXES or instant is None:
                raise _refuse(
                    "date must be gt, ge, lt or le followed by an instant with seconds and a "
                    "time zone, for example gt2026-01-01T00:00:00Z (percent-encode a + offset).",
                    expression=["date"],
                    reason="unsupported-value",
                )
            bound = DateBound(prefix, instant, text)
            if prefix in LOWER_PREFIXES:
                if lower is not None:
                    raise _refuse(
                        "date takes at most one lower bound (gt or ge).",
                        expression=["date"],
                        reason="unsupported-value",
                    )
                lower = bound
            else:
                if upper is not None:
                    raise _refuse(
                        "date takes at most one upper bound (lt or le).",
                        expression=["date"],
                        reason="unsupported-value",
                    )
                upper = bound
        elif name == "_sort":
            if value not in SORTS:
                raise _refuse(
                    "_sort must be date or -date.", expression=["_sort"], reason="unsupported-value"
                )
            sort = value
        elif name == "_count":
            if not re.fullmatch(r"[0-9]{1,4}", value, re.ASCII) or not 1 <= int(value) <= MAX_COUNT:
                raise _refuse(
                    f"_count must be a whole number from 1 to {MAX_COUNT}.",
                    expression=["_count"],
                    reason="unsupported-value",
                )
            count = int(value)
        elif name == PAGE_TOKEN:
            if not _PAGE_TOKEN.fullmatch(value):
                raise _refuse(
                    "The paging link is not valid; start the search again.",
                    expression=[PAGE_TOKEN],
                    reason="unsupported-value",
                )
            page_token = value
    if code is None:
        raise _refuse(
            "code is required (http://loinc.org|8867-4 or http://loinc.org|85354-9).",
            expression=["code"],
            reason="missing-parameter",
        )
    if lower is not None and upper is not None and upper.value <= lower.value:
        raise _refuse(
            "The date range is empty: the upper bound is not after the lower bound.",
            expression=["date"],
            reason="unsupported-value",
        )
    return ObservationQuery(code, lower, upper, sort, count, page_token)


# ---- applying the R4 date rule ----------------------------------------------------------------


def _parse_fhir_datetime(value: object) -> datetime | None:
    """A FHIR dateTime with a time and zone (what PulseBridge writes). Partial dates (``2026``,
    ``2026-01``) are not used in this store; they read as None and the resource does not match."""
    return parse_instant(value) if isinstance(value, str) else None


def effective_range(resource: dict) -> tuple[datetime | None, datetime | None] | None:
    """``(start, end)`` of ``effective[x]``; None when it has neither form. An open end (a
    Period without ``end``) is None: R4 reads it as ongoing."""
    if isinstance(resource.get("effectivePeriod"), dict):
        period = resource["effectivePeriod"]
        start = _parse_fhir_datetime(period.get("start"))
        end = _parse_fhir_datetime(period.get("end"))
        if start is None:
            return None
        return start, end
    moment = _parse_fhir_datetime(resource.get("effectiveDateTime"))
    if moment is None:
        return None
    return moment, moment


def matches_dates(resource: dict, query: ObservationQuery) -> bool:
    """R4's prefix rules on a range: ``gt v`` the range ends after ``v``; ``ge v`` it ends at or
    after ``v``; ``lt v`` it starts before ``v``; ``le v`` it starts at or before ``v``."""
    if query.lower is None and query.upper is None:
        return True
    span = effective_range(resource)
    if span is None:
        return False
    start, end = span
    if query.lower is not None and end is not None:
        if query.lower.prefix == "gt" and not end > query.lower.value:
            return False
        if query.lower.prefix == "ge" and not end >= query.lower.value:
            return False
    if query.upper is not None:
        if query.upper.prefix == "lt" and not start < query.upper.value:
            return False
        if query.upper.prefix == "le" and not start <= query.upper.value:
            return False
    return True


# ---- paging -----------------------------------------------------------------------------------


def page_token_from(link_url: object) -> str | None:
    """The store's ``_page_token`` from its ``next`` link, or None. The link itself (which names
    the project, dataset and store) is never passed on."""
    if not isinstance(link_url, str):
        return None
    try:
        query = urlsplit(link_url).query
    except ValueError:
        return None
    tokens = [v for k, v in parse_qsl(query, keep_blank_values=True) if k == PAGE_TOKEN]
    if len(tokens) != 1 or not _PAGE_TOKEN.fullmatch(tokens[0]):
        return None
    return tokens[0]


def own_link(path: str, params: list[tuple[str, str]]) -> str:
    """A link back to the proxy, as an absolute path (``/api/fhir/Observation?...``).

    Never built from the request's ``Host`` header, which a caller controls. A path resolves
    against wherever the page reached the proxy (the Firebase Hosting origin or ``*.run.app``),
    and the dashboard follows it only inside the proxy's base URL."""
    return f"{path}?{urlencode(params)}"
