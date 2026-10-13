# Feasibility probe for cancelling an agent's whole process tree (provider
# operation contract plan, section 5.2, PR 3a). POSIX sh; needs /proc.
#
# Pattern under test: a pre-exec wrapper records its own pid and start time,
# then starts the workload as leader of a new session (so pid == pgid). Cancel
# signals the whole group with TERM, waits a grace period, then KILL, and
# confirms each recorded process is gone with `kill -0` plus a start-time
# comparison, so a reused pid is never reported as the original process.
#
# Prints one line per result and exits 0 only if every case passed:
#   MISSING <binary>            a required binary is not on PATH (exit 3)
#   PASS <case> <TERM|KILL>     every recorded pid gone; the signal that did it
#   FAIL <case> <reason>
# Run inside a pinned workspace image by tests/conformance.rs and on the host
# by tests/process_tree_cancel.rs.

set -u

GRACE_TICKS=20 # TERM grace period, in 0.1 s ticks
KILL_TICKS=20  # how long KILL may take to land, in 0.1 s ticks

# `kill` is also a shell builtin; the provider runs the binary through
# `docker exec`, so resolve the executable on PATH rather than `command -v`.
on_path() {
    old_ifs=$IFS
    IFS=:
    for dir in $PATH; do
        if [ -f "$dir/$1" ] && [ -x "$dir/$1" ]; then
            IFS=$old_ifs
            printf '%s\n' "$dir/$1"
            return 0
        fi
    done
    IFS=$old_ifs
    return 1
}

missing=0
for binary in setsid kill; do
    on_path "$binary" >/dev/null || { echo "MISSING $binary"; missing=1; }
done
[ "$missing" -eq 0 ] || exit 3
KILL_BIN=$(on_path kill)
SETSID_BIN=$(on_path setsid)

# Field <n> of /proc/<pid>/stat (3 = state, 5 = pgrp, 22 = starttime). The
# command name is parenthesised and may hold spaces, so split after its last ')'.
stat_field() {
    field=$2
    stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    # shellcheck disable=SC2086 # deliberate word splitting into fields
    set -- ${stat##*) }
    shift $((field - 3))
    printf '%s\n' "$1"
}

# The recorded process is still running: the pid answers `kill -0`, it has the
# start time recorded for it (not a reused pid), and it is not a zombie.
alive() {
    "$KILL_BIN" -0 "$1" 2>/dev/null || return 1
    [ "$(stat_field "$1" 22)" = "$2" ] || return 1
    [ "$(stat_field "$1" 3)" != Z ]
}

# Pid/start-time pairs recorded for one case, as "pid start pid start ...".
any_alive() {
    while [ "$#" -ge 2 ]; do
        alive "$1" "$2" && return 0
        shift 2
    done
    return 1
}

wait_gone() {
    ticks=$1
    shift
    while any_alive "$@"; do
        [ "$ticks" -gt 0 ] || return 1
        ticks=$((ticks - 1))
        sleep 0.1
    done
}

wait_file() {
    ticks=50
    until [ -s "$1" ]; do
        [ "$ticks" -gt 0 ] || return 1
        ticks=$((ticks - 1))
        sleep 0.1
    done
}

# The trusted-pid wrapper: becomes session (and group) leader, records its pid
# and start time before exec, then execs the workload in place.
WRAPPER='printf "%s %s\n" "$$" "$(cut -d" " -f22 /proc/$$/stat)" > "$0.tmp" && mv "$0.tmp" "$0" && exec "$@"'

run_case() {
    name=$1
    workload=$2
    dir=$(mktemp -d) || { echo "FAIL $name mktemp"; return 1; }
    "$SETSID_BIN" sh -c "$WRAPPER" "$dir/leader" sh -c "$workload" "$dir/grandchild" &
    if ! wait_file "$dir/leader" || ! wait_file "$dir/grandchild"; then
        echo "FAIL $name workload-did-not-start"
        return 1
    fi
    read -r leader leader_start < "$dir/leader"
    grandchild=$(cat "$dir/grandchild")
    grandchild_start=$(stat_field "$grandchild" 22)
    if [ "$(stat_field "$leader" 5)" != "$leader" ]; then
        echo "FAIL $name leader-is-not-group-leader"
        return 1
    fi
    if [ "$(stat_field "$grandchild" 5)" != "$leader" ]; then
        echo "FAIL $name grandchild-left-the-group"
        return 1
    fi
    set -- "$leader" "$leader_start" "$grandchild" "$grandchild_start"

    "$KILL_BIN" -TERM -- "-$leader" 2>/dev/null
    if wait_gone "$GRACE_TICKS" "$@"; then
        signal=TERM
    else
        "$KILL_BIN" -KILL -- "-$leader" 2>/dev/null
        if ! wait_gone "$KILL_TICKS" "$@"; then
            echo "FAIL $name survived-KILL"
            return 1
        fi
        signal=KILL
    fi
    wait
    rm -rf "$dir"
    echo "PASS $name $signal"
}

# The grandchild is orphaned (its parent subshell exits), so only the process
# group, not a walk of the leader's children, still reaches it.
FORKS_GRANDCHILD='(sh -c "echo \$\$ > \"\$0.tmp\" && mv \"\$0.tmp\" \"\$0\" && exec sleep 300" "$0" &); exec sleep 300'
# Ignored signals are inherited, so TERM reaches nobody and only KILL ends it.
TRAPS_TERM='trap "" TERM; sh -c "echo \$\$ > \"\$0.tmp\" && mv \"\$0.tmp\" \"\$0\" && exec sleep 300" "$0" & wait'

status=0
run_case forks_grandchild "$FORKS_GRANDCHILD" || status=1
run_case traps_term "$TRAPS_TERM" || status=1

# The start-time comparison must reject a live pid recorded with another start
# time, which is what a reused pid looks like.
own_start=$(stat_field "$$" 22)
if ! alive "$$" "$own_start"; then
    echo "FAIL pid_reuse_guard rejects-live-process"
    status=1
elif alive "$$" "$((own_start + 1))"; then
    echo "FAIL pid_reuse_guard accepts-reused-pid"
    status=1
else
    echo "PASS pid_reuse_guard"
fi
exit "$status"
