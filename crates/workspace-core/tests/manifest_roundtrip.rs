use agentic_workspace_core::{FileInput, LaunchManifest, WorkspaceError, materialize_file};

const APSS_FIXTURE: &str =
    include_str!("../../../tests/conformance/fixtures/EXP-V1-0006/minimal-workspace-launch.json");

#[test]
fn apss_fixture_round_trips_without_loss() {
    let original: serde_json::Value = serde_json::from_str(APSS_FIXTURE).unwrap();
    let manifest: LaunchManifest = serde_json::from_value(original.clone()).unwrap();
    manifest.validate_boundary().unwrap();
    assert_eq!(serde_json::to_value(manifest).unwrap(), original);
}

#[test]
fn execution_id_cannot_escape_provider_root() {
    let mut manifest: LaunchManifest = serde_json::from_str(APSS_FIXTURE).unwrap();
    manifest.execution_id = "../escape".into();
    assert!(manifest.validate_boundary().is_err());
}

#[test]
fn overlapping_tool_policy_is_rejected() {
    let mut manifest: LaunchManifest = serde_json::from_str(APSS_FIXTURE).unwrap();
    manifest.tools.allow = vec!["shell".into()];
    manifest.tools.deny = vec!["shell".into()];
    assert!(manifest.validate_boundary().is_err());
}

#[test]
fn file_hydration_verifies_digest_before_writing() {
    let source = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(source.path(), b"expected").unwrap();
    let root = tempfile::tempdir().unwrap();
    let input = FileInput {
        source: source.path().display().to_string(),
        destination: "inputs/value.txt".into(),
        read_only: false,
        sha256: Some("0000000000000000000000000000000000000000000000000000000000000000".into()),
    };
    assert!(matches!(
        materialize_file(root.path(), &input),
        Err(WorkspaceError::DigestMismatch { .. })
    ));
    assert!(!root.path().join("inputs/value.txt").exists());
}
