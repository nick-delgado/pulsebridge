"""Make ``connect_google_health`` importable without installing it.

`make install` installs every package under libs/ and services/ but not scripts/, and this tool's
own third-party dependency (google-cloud-secret-manager) is only needed by the real Secret Manager
adapter, which the tests replace with a fake. So the tests import the package from the source
tree; pulsebridge_fhir and pulsebridge_gcp come from the shared venv.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
