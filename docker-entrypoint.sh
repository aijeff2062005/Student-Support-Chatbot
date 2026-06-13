#!/bin/bash
set -e

# Load secrets from Infisical
if [ -n "$INFISICAL_TOKEN" ]; then
  # Use exec so the infisical process replaces this shell PID (preserves signals and logging)
  # Note: drop the --silent flag so child process stdout/stderr are forwarded and not suppressed.
  exec infisical run \
    --token "$INFISICAL_TOKEN" \
    --projectId "$INFISICAL_PROJECT_ID" \
    --domain "$INFISICAL_API_URL" \
    --env "$DEPLOYMENT_ENVIRONMENT" \
    --path "$SECRET_PATH" \
    -- "$@"
else
  exec "$@"
fi
