"""Shared access to the vendored EXP-V1-0006 0.2.0 schemas and fixtures."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

import agentic_isolation.contract as contract

SCHEMAS = Path(contract.__file__).parent / "schemas"
FIXTURES = Path(__file__).parent / "fixtures" / "apss_exp_v1_0006"

Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]


def _registry() -> Registry[Json]:
    resources = []
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


_REGISTRY = _registry()


def schema_accepts(document: Json) -> bool:
    """The normative verdict: dispatch on `schema` like APSS's reference consumer."""
    if not isinstance(document, dict):
        return False
    tag = document.get("schema")
    if not (isinstance(tag, str) and tag.startswith("apss.") and tag.endswith("/v1")):
        return False
    path = SCHEMAS / f"{tag.removeprefix('apss.').removesuffix('/v1')}.schema.json"
    if not path.is_file():
        return False
    validator = Draft202012Validator(json.loads(path.read_text()), registry=_REGISTRY)
    return validator.is_valid(document)


def load(relative: str) -> Json:
    return json.loads((FIXTURES / relative).read_text())


def _parent(document: Json, pointer: str) -> tuple[Json, str]:
    *parents, last = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    node = document
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]  # type: ignore[index]
    return node, last


def apply_edits(base: Json, set_: list[dict[str, Json]], remove: list[str]) -> Json:
    """Apply an invalid fixture's JSON Pointer `set` and `remove` edits to a copy of `base`."""
    document = copy.deepcopy(base)
    for edit in set_:
        node, key = _parent(document, str(edit["path"]))
        if isinstance(node, list):
            node[int(key)] = edit["value"]
        else:
            node[key] = edit["value"]  # type: ignore[index]
    for pointer in remove:
        node, key = _parent(document, pointer)
        del node[int(key) if isinstance(node, list) else key]  # type: ignore[arg-type]
    return document


def valid_fixtures() -> Iterator[Path]:
    yield from sorted((FIXTURES / "valid").rglob("*.json"))


def invalid_fixtures() -> Iterator[Path]:
    yield from sorted((FIXTURES / "invalid").glob("*.json"))


def invalid_document(path: Path) -> Json:
    case = json.loads(path.read_text())
    return apply_edits(load(case["base"]), case["set"], case["remove"])


def behaviour_documents() -> Iterator[tuple[str, Json]]:
    """Every schema-tagged document a behaviour fixture expects, as `(id, document)`.

    Untagged expectations (`operationRef`, a `$defs` fragment) have no
    top-level schema to dispatch on and are not contract documents.
    """
    for path in sorted((FIXTURES / "behaviour").glob("*.json")):
        for index, step in enumerate(json.loads(path.read_text())["steps"]):
            document = step.get("expect", {}).get("document")
            if isinstance(document, dict) and "schema" in document:
                yield f"{path.stem}/step{index}", document
