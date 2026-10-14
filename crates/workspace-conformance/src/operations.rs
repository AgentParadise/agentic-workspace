//! The vendored APSS EXP-V1-0006 0.2.0 fixtures, checked against the Rust
//! operation models.
//!
//! The fixtures are the ones the Python models are checked against
//! (`lib/python/agentic_isolation/tests/fixtures/apss_exp_v1_0006`, vendored
//! from APSS 3bdbc33), read in place so both languages stay on one pin.

use agentic_workspace_core::operations::{
    ContentRef, CredentialRef, EnvironmentRef, FinalizationOutcome, ObservationPage, OperationRef,
    OperationSpec, ProviderDescriptor, StatusDocument, WorkspaceRef, WorkspaceResult,
};
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::path::{Path, PathBuf};

/// The vendored fixture directory.
pub fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../lib/python/agentic_isolation/tests/fixtures/apss_exp_v1_0006")
}

fn read(path: &Path) -> Value {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn json_files(dir: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|entry| entry.expect("readable fixture entry").path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
        .collect();
    files.sort();
    files
}

fn round_trip<T: DeserializeOwned + serde::Serialize>(document: Value) -> Result<Value, String> {
    let parsed: T = serde_json::from_value(document).map_err(|e| e.to_string())?;
    serde_json::to_value(parsed).map_err(|e| e.to_string())
}

/// Parse `document` as the model for `schema` and serialise it back.
///
/// `schema` is a schema file name, optionally with a `#/$defs/...` fragment,
/// as behaviour fixtures name it, or a `valid/` directory name.
pub fn parse_as(schema: &str, document: Value) -> Result<Value, String> {
    match schema.trim_end_matches(".schema.json") {
        "workspace-operation" => round_trip::<OperationSpec>(document),
        "workspace-operation.schema.json#/$defs/operationRef" => {
            round_trip::<OperationRef>(document)
        }
        "workspace-status" => round_trip::<StatusDocument>(document),
        "workspace-observation" => round_trip::<ObservationPage>(document),
        "workspace-result" => round_trip::<WorkspaceResult>(document),
        "workspace-finalization" => round_trip::<FinalizationOutcome>(document),
        "workspace-provider" => round_trip::<ProviderDescriptor>(document),
        "workspace-ref" => round_trip::<WorkspaceRef>(document),
        "credential-ref" => round_trip::<CredentialRef>(document),
        "content-ref" => round_trip::<ContentRef>(document),
        "environment-ref" => round_trip::<EnvironmentRef>(document),
        other => Err(format!("no Rust model for schema {other:?}")),
    }
}

/// Every `valid/<schema>/<name>.json` as `(schema, path, document)`.
pub fn valid_documents() -> Vec<(String, PathBuf, Value)> {
    let root = fixture_root().join("valid");
    let mut schemas: Vec<PathBuf> = std::fs::read_dir(&root)
        .unwrap_or_else(|e| panic!("{}: {e}", root.display()))
        .map(|entry| entry.expect("readable fixture entry").path())
        .collect();
    schemas.sort();
    schemas
        .iter()
        .flat_map(|dir| {
            let schema = dir.file_name().unwrap().to_string_lossy().into_owned();
            json_files(dir)
                .into_iter()
                .map(move |path| (schema.clone(), read(&path), path))
        })
        .map(|(schema, document, path)| (schema, path, document))
        .collect()
}

/// Every `invalid/` fixture as `(schema, path, edited document)`: its `base`
/// success fixture with the `set` and `remove` JSON Pointer edits applied.
pub fn invalid_documents() -> Vec<(String, PathBuf, Value)> {
    let root = fixture_root();
    json_files(&root.join("invalid"))
        .into_iter()
        .map(|path| {
            let fixture = read(&path);
            let base = fixture["base"]
                .as_str()
                .expect("invalid fixture names a base");
            let schema = base
                .split('/')
                .nth(1)
                .expect("base is valid/<schema>/<name>");
            let mut document = read(&root.join(base));
            for edit in fixture["set"].as_array().into_iter().flatten() {
                let pointer = edit["path"].as_str().expect("set path");
                set_pointer(&mut document, pointer, edit["value"].clone());
            }
            for pointer in fixture["remove"].as_array().into_iter().flatten() {
                let pointer = pointer.as_str().expect("remove path");
                remove_pointer(&mut document, pointer);
            }
            (schema.to_owned(), path, document)
        })
        .collect()
}

/// Every schema-tagged document a behaviour fixture step expects, each
/// `expect_one_of` alternative included, as `(fixture name and step,
/// schema, document)`. Fixture-local schemas (`fixtures/...`, the LH-7 file
/// probe) describe harness readings, not contract documents, and are left out.
pub fn behaviour_documents() -> Vec<(String, String, Value)> {
    json_files(&fixture_root().join("behaviour"))
        .into_iter()
        .flat_map(|path| {
            let name = path.file_stem().unwrap().to_string_lossy().into_owned();
            let fixture = read(&path);
            let steps = fixture["steps"].as_array().cloned().unwrap_or_default();
            steps.into_iter().enumerate().flat_map(move |(i, step)| {
                let alternatives = match step.get("expect_one_of") {
                    Some(many) => many.as_array().expect("expect_one_of array").clone(),
                    None => vec![step["expect"].clone()],
                };
                let name = name.clone();
                alternatives
                    .into_iter()
                    .enumerate()
                    .filter_map(move |(j, expect)| {
                        let schema = expect["schema"].as_str().expect("schema").to_owned();
                        (!schema.starts_with("fixtures/")).then(|| {
                            (
                                format!("{name}#{i}.{j}"),
                                schema,
                                expect["document"].clone(),
                            )
                        })
                    })
            })
        })
        .collect()
}

fn tokens(pointer: &str) -> Vec<String> {
    pointer
        .split('/')
        .skip(1)
        .map(|t| t.replace("~1", "/").replace("~0", "~"))
        .collect()
}

fn parent<'a>(document: &'a mut Value, tokens: &[String]) -> &'a mut Value {
    tokens.iter().fold(document, |node, token| match node {
        Value::Array(items) => &mut items[token.parse::<usize>().expect("array index")],
        _ => node
            .get_mut(token)
            .unwrap_or_else(|| panic!("pointer token {token:?} not found")),
    })
}

fn set_pointer(document: &mut Value, pointer: &str, value: Value) {
    let tokens = tokens(pointer);
    let (last, path) = tokens.split_last().expect("non-root pointer");
    match parent(document, path) {
        Value::Array(items) => items[last.parse::<usize>().expect("array index")] = value,
        Value::Object(map) => {
            map.insert(last.clone(), value);
        }
        other => panic!("cannot set {pointer} inside {other}"),
    }
}

fn remove_pointer(document: &mut Value, pointer: &str) {
    let tokens = tokens(pointer);
    let (last, path) = tokens.split_last().expect("non-root pointer");
    match parent(document, path) {
        Value::Array(items) => {
            items.remove(last.parse::<usize>().expect("array index"));
        }
        Value::Object(map) => {
            map.remove(last)
                .unwrap_or_else(|| panic!("{pointer} not found"));
        }
        other => panic!("cannot remove {pointer} inside {other}"),
    }
}
