//! The vendored APSS 0.2.1 fixtures against the Rust operation models.

use agentic_workspace_conformance::operations::{
    behaviour_documents, invalid_documents, parse_as, valid_documents,
};

#[test]
fn every_valid_fixture_parses_and_round_trips_without_loss() {
    let documents = valid_documents();
    // 27 success fixtures across the 0.2.1 schemas at APSS ee926c6 (0.2.1
    // adds valid/workspace-provider/small-retention.json); an empty walk
    // would pass vacuously.
    assert_eq!(documents.len(), 27);
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
    // 59 contract-schema alternatives across the 14 behaviour fixtures: 21
    // from the 0.2.0 seven, 38 from the 0.2.1 LH-1..LH-7 (the LH-7 file
    // probe's fixture-local schema excluded).
    assert_eq!(documents.len(), 59);
    for (step, schema, document) in documents {
        let back =
            parse_as(&schema, document.clone()).unwrap_or_else(|e| panic!("{step}: rejected: {e}"));
        assert_eq!(back, document, "{step}: changed on round trip");
    }
}
