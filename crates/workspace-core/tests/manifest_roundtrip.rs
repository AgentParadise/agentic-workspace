use agentic_workspace_core::LaunchManifest;

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
