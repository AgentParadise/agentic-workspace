//! Proves the process-tree cancel pattern without Docker, as a plain
//! subprocess, so it runs in agent workspaces that have no daemon. The same
//! probe runs inside every pinned workspace image in `conformance.rs`.

#[path = "support/process_tree_cancel.rs"]
mod process_tree_cancel;

use std::process::Command;

#[test]
#[cfg(target_os = "linux")]
fn group_kill_with_escalation_cancels_the_whole_tree_on_the_host() {
    let output = Command::new("sh")
        .args(["-c", process_tree_cancel::SCRIPT, "process_tree_cancel"])
        .output()
        .unwrap();
    process_tree_cancel::assert_cancels_whole_tree(&output, "the host");
}
