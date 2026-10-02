#!/usr/bin/env bash
# APS-V1-0004 session-store degrade hook (ADR-040, #27).
#
# Run by entrypoint.sh 5.7 when this capability's doctor failed and its
# manifest declared failure_policy=degrade, i.e. when the workspace is about
# to start with capture DISABLED. Executed, not sourced.
#
# WHAT IT UNDOES: the native child capture hooks init.sh installs for the
# local provider. They are FAIL CLOSED by design (a launch whose intent
# cannot be recorded is denied, syntropic137#1398), which is right while
# capture is on and wrong once it is off: a later journal failure would deny
# Claude and Codex child launches in a workspace reported as running without
# capture. init.sh installs Codex, then Claude, so a failure between the two
# leaves a partial install; both configs are therefore always visited.
#
# Removal takes only the exact groups the installer writes (and moves later
# Codex groups' index-keyed trust down with them); an operator's own hooks
# and an unparseable config are left untouched. A config that does not exist
# stays absent. Remote providers install no hooks, so this is a no-op there.
#
# Paths are the ones init.sh installs to. Exit 0 when every config is clean
# afterwards, non-zero otherwise; the entrypoint reports it loudly but still
# starts the workspace, because refusing to start is the outcome #27 removes.

set -u

__py=python3
if [ -x /opt/venv/bin/python ]; then __py=/opt/venv/bin/python; fi

__rc=0
"${__py}" -m agentic_session_store.install_child_hooks \
    "${CODEX_HOME:-${HOME}/.codex}/config.toml" --uninstall || __rc=1
"${__py}" -m agentic_session_store.install_child_hooks \
    "${CLAUDE_CONFIG_DIR:-${HOME}/.claude}/settings.json" --harness claude \
    --uninstall || __rc=1

if [ "${__rc}" -eq 0 ]; then
    echo "[session-store] capture hooks removed: child launches no longer depend on the recorder" >&2
fi
exit "${__rc}"
