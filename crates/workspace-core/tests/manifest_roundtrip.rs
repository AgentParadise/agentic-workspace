use agentic_workspace_core::{
    APSS_WORKSPACE_STANDARD_ID, APSS_WORKSPACE_STANDARD_VERSION, FileInput, LaunchManifest,
    WorkspaceError, materialize_file,
};

const APSS_FIXTURE: &str =
    include_str!("../../../tests/conformance/fixtures/EXP-V1-0006/minimal-workspace-launch.json");

#[test]
fn apss_fixture_round_trips_without_loss() {
    assert_eq!(APSS_WORKSPACE_STANDARD_ID, "EXP-V1-0006");
    assert_eq!(APSS_WORKSPACE_STANDARD_VERSION, "0.1.0");
    let original: serde_json::Value = serde_json::from_str(APSS_FIXTURE).unwrap();
    let manifest: LaunchManifest = serde_json::from_value(original.clone()).unwrap();
    manifest.validate_boundary().unwrap();
    assert_eq!(serde_json::to_value(manifest).unwrap(), original);
}

#[test]
fn apss_semantics_reject_unpinned_skills() {
    let mut manifest: LaunchManifest = serde_json::from_str(APSS_FIXTURE).unwrap();
    manifest.skills[0].digest.clear();
    let error = manifest.validate_boundary().unwrap_err().to_string();
    assert!(error.contains("EXP-V1-0006 v0.1.0"), "{error}");
    assert!(
        error.contains("skill revision and digest are required"),
        "{error}"
    );
}

#[test]
fn apss_semantics_reject_unpinned_repositories() {
    let mut manifest: LaunchManifest = serde_json::from_str(APSS_FIXTURE).unwrap();
    manifest.content.repositories[0].revision.clear();
    let error = manifest.validate_boundary().unwrap_err().to_string();
    assert!(error.contains("content.repositories.revision"), "{error}");
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

#[test]
fn file_hydration_accepts_matching_lowercase_hex_digest() {
    let source = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(source.path(), b"hello").unwrap();
    let root = tempfile::tempdir().unwrap();
    let input = FileInput {
        source: source.path().display().to_string(),
        destination: "inputs/value.txt".into(),
        read_only: false,
        sha256: Some("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824".into()),
    };
    materialize_file(root.path(), &input).unwrap();
    assert_eq!(
        std::fs::read(root.path().join("inputs/value.txt")).unwrap(),
        b"hello"
    );
}
