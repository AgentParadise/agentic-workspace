//! The vendored APSS 0.2.0 fixtures against the Rust operation models.

use agentic_workspace_conformance::operations::{
    behaviour_documents, invalid_documents, parse_as, valid_documents,
};

#[test]
fn every_valid_fixture_parses_and_round_trips_without_loss() {
    let documents = valid_documents();
    // 26 success fixtures across the 10 0.2.0 schemas at APSS 3bdbc33; an
    // empty walk would pass vacuously.
    assert_eq!(documents.len(), 26);
    for (schema, path, document) in documents {
        let back = parse_as(&schema, document.clone())
            .unwrap_or_else(|e| panic!("{}: rejected: {e}", path.display()));
        assert_eq!(back, document, "{}: changed on round trip", path.display());
    }
}

#[test]
fn every_invalid_fixture_is_rejected_by_serde() {
    let documents = invalid_documents();
    assert_eq!(documents.len(), 33);
    for (schema, path, document) in documents {
        assert!(
            parse_as(&schema, document).is_err(),
            "{}: accepted an invalid {schema} document",
            path.display()
        );
    }
}

#[test]
fn every_behaviour_expectation_parses_and_round_trips() {
    let documents = behaviour_documents();
    // 21 expected responses across the 7 behaviour fixtures.
    assert_eq!(documents.len(), 21);
    for (step, schema, document) in documents {
        let back =
            parse_as(&schema, document.clone()).unwrap_or_else(|e| panic!("{step}: rejected: {e}"));
        assert_eq!(back, document, "{step}: changed on round trip");
    }
}
