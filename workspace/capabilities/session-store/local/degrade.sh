#!/usr/bin/env bash
# The local provider installs the same child capture hooks through apss/init.sh,
# so it removes them the same way when the capability degrades (#27).
exec "$(dirname "${BASH_SOURCE[0]}")/../apss/degrade.sh" "$@"
