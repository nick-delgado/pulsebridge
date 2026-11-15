"""Keep the Google client libraries from printing what they send.

The script handles the OAuth client secret and the refresh token, and sends them to Secret
Manager through ``google-cloud-secret-manager``. That library can print request and response
payloads, and so those values, in several ways the deployer's shell can switch on:

- ``GOOGLE_SDK_PYTHON_LOGGING_SCOPE``: ``google-api-core`` reads it when the first client is
  created and gives the named logger (``google`` or any logger below it, such as
  ``google.cloud.secretmanager_v1``) a DEBUG handler on stderr. The generated client then logs
  every request and response, including ``AddSecretVersionRequest.payload.data`` (the secret,
  base64-encoded) and the payload of ``AccessSecretVersion``.
- ``GRPC_TRACE`` / ``GRPC_VERBOSITY``: gRPC's C core reads them when ``grpc`` is first imported
  and can trace connection and transport details to stderr.
- A DEBUG configuration of the ``urllib3``, ``requests`` or ``grpc`` loggers, and
  ``http.client``'s ``debuglevel`` (which prints request headers, so the ``Authorization``
  header and the token form, to stdout).

:func:`quiet_sdk_logging` neutralises all of them. It must run before any Google client or
``grpc`` is imported; :func:`connect_google_health.cli.real_dependencies` calls it first.
The script itself never logs, so switching logging off loses nothing.
"""

from __future__ import annotations

import http.client
import logging
import os
from collections.abc import MutableMapping

# Removed from the environment: each can make a library print payloads or transport traces.
REMOVED_VARIABLES = ("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", "GRPC_TRACE")
# Set: gRPC C-core logs nothing at all.
GRPC_VERBOSITY = "NONE"
# Loggers given a do-nothing handler, no propagation and the CRITICAL level, so that
# google-api-core's own setup sees them as already configured and leaves them alone.
QUIET_LOGGERS = ("google", "grpc", "urllib3", "requests")


def quiet_sdk_logging(environ: MutableMapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    for name in REMOVED_VARIABLES:
        env.pop(name, None)
    env["GRPC_VERBOSITY"] = GRPC_VERBOSITY
    for name in QUIET_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
    http.client.HTTPConnection.debuglevel = 0
    # Last line of defence: a logger set up below "google" by anything else, at any level, is
    # still silent, because no logging call at CRITICAL or below is processed at all.
    logging.disable(logging.CRITICAL)
