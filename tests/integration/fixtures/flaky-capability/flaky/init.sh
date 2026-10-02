#!/usr/bin/env bash
# Declares a credential, so a degraded capability is shown to keep it withheld.
export FLAKY_SECRET="flaky-owns-this"
AGENTIC_CAPABILITY_WITHHOLD="${AGENTIC_CAPABILITY_WITHHOLD:-} FLAKY_SECRET"
export AGENTIC_CAPABILITY_WITHHOLD
return 0
