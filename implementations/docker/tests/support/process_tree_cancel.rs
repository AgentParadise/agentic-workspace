//! Shared by the host test and the per-image Docker conformance tests: both
//! run the same cancel probe and must see the same outcome.

use std::process::Output;

/// Group TERM -> grace -> group KILL against a trusted, recorded session
/// leader; see the header of the script for the protocol and its output.
pub const SCRIPT: &str = include_str!("../fixtures/process_tree_cancel.sh");

/// A workload that forks an orphaned grandchild dies on the group TERM; one
/// that ignores TERM needs the KILL escalation, and so does one whose leader
/// obeys TERM while its child ignores it; none leaves a recorded pid alive,
/// and the start-time check rejects a reused pid.
pub fn assert_cancels_whole_tree(output: &Output, target: &str) {
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(
        stdout.lines().collect::<Vec<_>>(),
        [
            "PASS forks_grandchild TERM",
            "PASS traps_term KILL",
            "PASS child_traps_term KILL",
            "PASS pid_reuse_guard",
        ],
        "process tree cancel probe in {target} (status {}):\n{stdout}{}",
        output.status,
        String::from_utf8_lossy(&output.stderr),
    );
    assert!(output.status.success(), "probe exit status in {target}");
}
