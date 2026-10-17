//! The APSS lifecycle hazards (LH-1..LH-7) under their own harness. A hazard
//! test that cannot measure the liveness it judges is NOT RUN, as the matrix
//! reports it (#68, #78): a runtime skip with its reason, which libtest
//! cannot express, so they leave `operations`' harness for this one.
//!
//! They share `operations`' stand-in; its own libtest tests are compiled out
//! here, so what only they use is dead code in this target. Off Linux the
//! module is empty, as `operations` is, and there is nothing to run.

#[allow(dead_code, unused_imports)]
#[path = "operations.rs"]
mod operations;

fn main() {
    #[cfg(target_os = "linux")]
    operations::hazards::main();
}
