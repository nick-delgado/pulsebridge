#!/bin/bash
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
[ -n "$GCP_SA_KEY_B64" ] || { echo "GCP_SA_KEY_B64 not set"; exit 0; }
mkdir -p "$(dirname "$GOOGLE_APPLICATION_CREDENTIALS")"
echo "$GCP_SA_KEY_B64" | base64 -d > "$GOOGLE_APPLICATION_CREDENTIALS"
chmod 600 "$GOOGLE_APPLICATION_CREDENTIALS"
gcloud auth activate-service-account --key-file="$GOOGLE_APPLICATION_CREDENTIALS" --quiet
gcloud config set project "$CLOUDSDK_CORE_PROJECT" --quiet
