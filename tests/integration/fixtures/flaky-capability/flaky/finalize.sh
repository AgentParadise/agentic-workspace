#!/usr/bin/env bash
# Must never run for a degraded workspace: a degraded capability is disabled.
set -u
echo "FLAKY_FINALIZE_RAN FLAKY_SECRET=${FLAKY_SECRET:-<unset>}" >&2
exit 0
