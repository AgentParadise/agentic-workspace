# ScriptedAgentProfile Contract Provenance

- Source repository: `syntropic137/syntropic137`
- Source commit: `1017ec7b9c514afb1eceda0773c0a53031ff867a` (merge of syntropic137#1879)
- Source path: `packages/syn-perf/src/syn_perf/loadtest/`
- Vendored here: `scripted_agent_profile.schema.json` (byte-for-byte)
- Ported to `scripted_agent/profile.py`: the models, validators,
  `planned_run`, `task_result` and `render_artifact` from
  `scripted_agent_profile.py`, plus syn_domain's `review_rounds.next_phase`

`tests/test_contract_parity.py` checks the digest below and that the ported
model generates the vendored schema exactly, the same check upstream runs
against its own model. Re-vendor only from a merged syntropic137 revision, and
update the commit, this record, the schema and the port in one change.

## SHA-256

| File | SHA-256 |
|---|---|
| `scripted_agent/contract/scripted_agent_profile.schema.json` | `e16acbe0db3af90b685dd2f4497bac66154d6be8067a5fd2f59ee96f40297dda` |
| upstream `scripted_agent_profile.py` (ported, not vendored) | `f8cf6073e709a32374ddb9acfdd8dd005903ec635b5d11e2a50f1f4a8529a5b8` |
