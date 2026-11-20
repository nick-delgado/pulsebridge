"""The daily bot-check cap (D16): ``BOT_CHECKS_PER_DAY`` assessments per UTC day, counted in
Cloud Storage so the count is shared by every instance and survives restarts.

Why not a counter in memory: Cloud Run scales the proxy to zero when idle and starts up to
``max_instance_count`` instances under load. An in-memory count would start again at 0 after
every idle spell and would be kept separately on each instance, so the cap would mean
"per instance, since it last started": a bot could reset it simply by waiting for scale-to-zero.
A bucket costs nothing when idle, which a database or Memorystore would not.

**One object per day**, ``bot-checks/<YYYY-MM-DD>`` (UTC), in ``DEMO_COUNTER_BUCKET``. The count
is in the object's custom metadata (``metadata.count``) and, for a person looking at the
bucket, in its content. The bucket's lifecycle rule (T44) deletes objects after 7 days.

**An increment that cannot lose a count** (optimistic concurrency):

1. Read the object's metadata (``objects.get``). It carries the **generation**, a number Cloud
   Storage changes every time the object is written. No object yet: count 0, generation 0.
2. If the count has reached the cap: refused (``demo-busy``).
3. Write count + 1 (``objects.insert``, a multipart upload) with ``ifGenerationMatch=<that
   generation>``, a **precondition**: Cloud Storage performs the write only if the object is
   still the one read in step 1 (``ifGenerationMatch=0``: only if it still does not exist).
   If another instance wrote in between, the answer is ``412 Precondition Failed`` and nothing
   was written; start again from step 1 (at most :data:`MAX_ATTEMPTS` times).

Two instances can therefore never both turn 41 into 42. The increment happens **before** the
assessment, so the cap counts assessments made, including those that then fail; a request that
gets ``demo-busy`` makes none. Any other failure (no answer, 403, 5xx, an object whose count is
not a number, or contention that outlasts the retries) is :class:`CounterUnavailable`, and the
caller fails closed: no counter, no assessment.

Requests (JSON API, docs.cloud.google.com/storage/docs/json_api/v1/objects/get and
/objects/insert; "Request preconditions", docs.cloud.google.com/storage/docs/request-preconditions;
"Perform a multipart upload", docs.cloud.google.com/storage/docs/uploading-objects):

- ``GET https://storage.googleapis.com/storage/v1/b/<bucket>/o/bot-checks%2F<day>``
- ``POST https://storage.googleapis.com/upload/storage/v1/b/<bucket>/o?uploadType=multipart
  &ifGenerationMatch=<generation>``, the object's name in the JSON part

**Cloud Storage's write rate on one object.** Cloud Storage allows about one write per second
to the same object name and answers ``429`` above that ("Quotas and limits"). So the counter
admits about one assessment a second across all visitors, which is plenty for a demo; a
``429`` waits :data:`SLOW_DOWN_SECONDS` and tries again (within :data:`MAX_ATTEMPTS` and the
request's time budget).

The proxy's service account needs to read and create objects in this bucket and nothing else
(``storage.objects.get`` and ``storage.objects.create``; overwriting an object also needs
``storage.objects.delete``): T44 grants a bucket-level role.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from pulsebridge_gcp import HealthcareApiError, TimedOutError

STORAGE_BASE = "https://storage.googleapis.com/storage/v1"
UPLOAD_BASE = "https://storage.googleapis.com/upload/storage/v1"
OBJECT_PREFIX = "bot-checks/"
MAX_ATTEMPTS = 8
SLOW_DOWN_SECONDS = 1.0
BOUNDARY = "pulsebridge-demo-counter"
_COUNT = re.compile(r"[0-9]{1,9}", re.ASCII)
_GENERATION = re.compile(r"[0-9]{1,20}", re.ASCII)


class CounterUnavailable(Exception):
    """The counter could not be read or written. ``str()`` is a fixed code (``timeout``,
    ``unreachable``, ``http-<status>``, ``bad-object``, ``contention``)."""


class DemoBusy(Exception):
    """Today's cap is reached. ``retry_after``: whole seconds until the next UTC day."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("demo-busy")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Taken:
    """One assessment counted: today's count after the increment."""

    day: str
    count: int


class DailyCounter:
    def __init__(
        self,
        transport: object,
        *,
        bucket: str,
        cap: int,
        timeout: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._sleep = sleep
        self._bucket = quote(bucket, safe="")
        self._cap = cap
        self._timeout = timeout
        self._clock = clock

    def take(self) -> Taken:
        """Count one assessment for today, or raise :class:`DemoBusy` /
        :class:`CounterUnavailable`."""
        now = self._clock().astimezone(UTC)
        day = now.date().isoformat()
        name = OBJECT_PREFIX + day
        for _ in range(MAX_ATTEMPTS):
            count, generation = self._read(name)
            if count >= self._cap:
                midnight = datetime(now.year, now.month, now.day, tzinfo=UTC).timestamp() + 86400
                raise DemoBusy(max(1, int(midnight - now.timestamp() + 0.999)))
            written = self._write(name, count + 1, generation)
            if written == "written":
                return Taken(day, count + 1)
            if written == "slow-down":
                self._sleep(SLOW_DOWN_SECONDS)
            # "conflict": another instance wrote first; read its count and try again at once.
        raise CounterUnavailable("contention")

    def _call(self, method: str, url: str, **kwargs: object) -> object:
        try:
            return self._transport.request(  # type: ignore[attr-defined]
                method, url, timeout=self._timeout, **kwargs
            )
        except HealthcareApiError as exc:
            code = "timeout" if isinstance(exc, TimedOutError) else "unreachable"
            raise CounterUnavailable(code) from None

    def _read(self, name: str) -> tuple[int, int]:
        url = f"{STORAGE_BASE}/b/{self._bucket}/o/{quote(name, safe='')}"
        response = self._call("GET", url, params=None, body=None, headers={})
        if response.status == 404:
            return 0, 0  # no assessment yet today; generation 0 = "must not exist"
        if response.status != 200:
            raise CounterUnavailable(f"http-{response.status}")
        try:
            resource = response.json()
        except (UnicodeDecodeError, ValueError):
            raise CounterUnavailable("bad-object") from None
        if not isinstance(resource, dict):
            raise CounterUnavailable("bad-object")
        generation = resource.get("generation")  # a string: an int64 in JSON
        metadata = resource.get("metadata")
        count = metadata.get("count") if isinstance(metadata, dict) else None
        if not (
            isinstance(generation, str)
            and _GENERATION.fullmatch(generation)
            and isinstance(count, str)
            and _COUNT.fullmatch(count)
        ):
            # Fail closed: a counter someone edited by hand is not trusted to be under the cap.
            raise CounterUnavailable("bad-object")
        return int(count), int(generation)

    def _write(self, name: str, count: int, generation: int) -> str:
        """``written``; ``conflict`` (412: the precondition failed, someone else wrote first);
        ``slow-down`` (429: the object's write rate)."""
        metadata = {"name": name, "contentType": "text/plain", "metadata": {"count": str(count)}}
        body = (
            f"--{BOUNDARY}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{BOUNDARY}\r\n"
            "Content-Type: text/plain\r\n\r\n"
            f"{count}\n\r\n"
            f"--{BOUNDARY}--\r\n"
        ).encode()
        response = self._call(
            "POST",
            f"{UPLOAD_BASE}/b/{self._bucket}/o",
            params={"uploadType": "multipart", "ifGenerationMatch": str(generation)},
            body=body,
            headers={"Content-Type": f"multipart/related; boundary={BOUNDARY}"},
        )
        if response.status == 412:
            return "conflict"
        if response.status == 429:
            return "slow-down"
        if response.status != 200:
            raise CounterUnavailable(f"http-{response.status}")
        return "written"
