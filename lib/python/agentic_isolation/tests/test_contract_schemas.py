"""The contract models accept and reject exactly what the vendored schemas do.

The schemas are normative. Every vendored success and failure fixture is run
through both `jsonschema` and `parse_document`, and the two verdicts must match
the fixture's expectation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

import agentic_isolation.contract as contract
from agentic_isolation.contract import ContractModel, parse_document

from ._apss_contract import (
    Json,
    apply_edits,
    behaviour_documents,
    invalid_document,
    invalid_fixtures,
    load,
    schema_accepts,
    valid_fixtures,
)


def _model_accepts(document: Json) -> bool:
    try:
        parse_document(json.dumps(document))
    except ValidationError:
        return False
    return True


def _ids(paths: Iterator[Path]) -> list[Path]:
    return list(paths)


VALID = _ids(valid_fixtures())
INVALID = _ids(invalid_fixtures())


def test_fixture_inventory_matches_apss_0_2_0() -> None:
    # Guards against an empty or partial vendor silently passing the
    # parametrised tests below.
    assert len(VALID) == 26
    assert len(INVALID) == 33
    assert {path.parent.name for path in VALID} == {
        path.name.removesuffix(".schema.json")
        for path in (Path(contract.__file__).parent / "schemas").glob("*.schema.json")
    } - {"workspace-launch"}


@pytest.mark.parametrize("path", VALID, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_success_fixture_accepted_by_schema_and_model(path: Path) -> None:
    document = json.loads(path.read_text())
    assert schema_accepts(document)
    assert _model_accepts(document)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_failure_fixture_rejected_by_schema_and_model(path: Path) -> None:
    document = invalid_document(path)
    assert not schema_accepts(document)
    assert not _model_accepts(document)


@pytest.mark.parametrize("path", VALID, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_model_serializes_back_to_the_fixture(path: Path) -> None:
    # The hop after parsing: what a caller writes back out must still be the
    # same schema-valid document, with absent optional fields left absent.
    document = json.loads(path.read_text())
    dumped = json.loads(parse_document(json.dumps(document)).model_dump_json())
    assert schema_accepts(dumped)
    assert parse_document(json.dumps(dumped)) == parse_document(json.dumps(document))
    assert dumped.keys() == document.keys()


BEHAVIOUR = list(behaviour_documents())


def test_behaviour_inventory_matches_apss_0_2_0() -> None:
    # 14 schema-tagged expectations across the 7 behaviour fixtures; an empty
    # walk would otherwise pass the parametrised test below vacuously.
    assert len(BEHAVIOUR) == 14
    assert {document["schema"] for _, document in BEHAVIOUR if isinstance(document, dict)} == {
        "apss.workspace-status/v1",
        "apss.workspace-observation/v1",
        "apss.workspace-result/v1",
    }


@pytest.mark.parametrize("document", [d for _, d in BEHAVIOUR], ids=[i for i, _ in BEHAVIOUR])
def test_behaviour_document_accepted_by_schema_and_model(document: Json) -> None:
    # The behaviour fixtures are what a provider must produce, so the model
    # must accept them exactly as the schema does and write them back intact.
    assert schema_accepts(document)
    assert _model_accepts(document)
    dumped = json.loads(parse_document(json.dumps(document)).model_dump_json())
    assert dumped == document


def test_document_without_discriminator_is_rejected() -> None:
    document = load("valid/workspace-ref/minimal.json")
    assert isinstance(document, dict)
    del document["schema"]
    assert not schema_accepts(document)
    assert not _model_accepts(document)


# Every contract model, located in a success fixture. Injecting an unknown
# field at that location must be rejected; with the model set to
# extra="allow" the same document must be accepted, proving the rejection
# rests on that model's extra="forbid".
UNKNOWN_FIELD_SITES: dict[type[ContractModel], tuple[str, str]] = {
    contract.CredentialRef: ("valid/credential-ref/minimal.json", ""),
    contract.CredentialBinding: ("valid/workspace-operation/full.json", "/credential_bindings/0"),
    contract.ContentRef: ("valid/content-ref/with-authority.json", ""),
    contract.EnvironmentRef: ("valid/environment-ref/oci-image.json", ""),
    contract.WorkspaceRef: ("valid/workspace-ref/minimal.json", ""),
    contract.LaunchMarker: ("valid/workspace-operation/full.json", "/launch_marker"),
    contract.OperationSpec: ("valid/workspace-operation/full.json", ""),
    contract.OperationRef: ("valid/workspace-status/running.json", "/ref"),
    contract.OperationStatus: ("valid/workspace-status/running.json", ""),
    contract.OperationError: ("valid/workspace-status/error-unknown-operation.json", ""),
    contract.ObservationGap: ("valid/workspace-observation/gap-exact.json", "/gap"),
    contract.ObservationPage: ("valid/workspace-observation/running.json", ""),
    contract.FinalizationOutcome: ("valid/workspace-finalization/completed.json", ""),
    contract.ResolvedCommit: ("valid/workspace-result/succeeded.json", "/resolved_commits/0"),
    contract.CaptureOutcome: ("valid/workspace-result/succeeded.json", "/capture"),
    contract.WorkspaceResult: ("valid/workspace-result/succeeded.json", ""),
    contract.EnforceableLimits: ("valid/workspace-provider/docker.json", "/enforceable_limits"),
    contract.ObservationDescriptor: ("valid/workspace-provider/docker.json", "/observation"),
    contract.ProviderDescriptor: ("valid/workspace-provider/docker.json", ""),
}


def _all_models() -> list[type[ContractModel]]:
    found: list[type[ContractModel]] = []
    pending = list(ContractModel.__subclasses__())
    while pending:
        model = pending.pop(0)
        found.append(model)
        pending.extend(model.__subclasses__())
    return found


def _with_unknown_field(model: type[ContractModel]) -> Json:
    base, pointer = UNKNOWN_FIELD_SITES[model]
    return apply_edits(load(base), [{"path": f"{pointer}/x_unknown", "value": "1"}], [])


@contextmanager
def _extra_allowed(model: type[ContractModel], monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def rebuild() -> None:
        # Children are defined before parents, so rebuilding in definition
        # order propagates the change into every enclosing model.
        for each in _all_models():
            each.model_rebuild(force=True)
        monkeypatch.setattr(contract, "_DOCUMENT", TypeAdapter(contract.ContractDocument))

    monkeypatch.setitem(model.model_config, "extra", "allow")
    rebuild()
    try:
        yield
    finally:
        monkeypatch.setitem(model.model_config, "extra", "forbid")
        rebuild()


def test_every_model_has_an_unknown_field_site() -> None:
    assert set(_all_models()) == set(UNKNOWN_FIELD_SITES)


@pytest.mark.parametrize("model", list(UNKNOWN_FIELD_SITES), ids=lambda m: m.__name__)
def test_unknown_field_rejected_at_every_model(model: type[ContractModel]) -> None:
    document = _with_unknown_field(model)
    assert not schema_accepts(document)
    assert not _model_accepts(document)


@pytest.mark.parametrize("model", list(UNKNOWN_FIELD_SITES), ids=lambda m: m.__name__)
def test_extra_allow_on_any_model_breaks_unknown_field_rejection(
    model: type[ContractModel], monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _with_unknown_field(model)
    with _extra_allowed(model, monkeypatch):
        assert _model_accepts(document), (
            f"{model.__name__} with extra='allow' still rejected an unknown field, "
            "so test_unknown_field_rejected_at_every_model does not guard it"
        )
    assert not _model_accepts(document)
