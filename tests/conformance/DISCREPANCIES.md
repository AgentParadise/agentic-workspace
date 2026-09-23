# Known Conformance Divergences

## DISC-001: Local repository hydration

- Reference: EXP-V1-0006 section 3.3
- Implementation: Local requires a pre-hydrated directory and rejects repository entries.
- Impact: Local cannot yet execute a manifest containing repositories.
- Resolution: WILL-FIX
- Tests affected: Local hydration conformance
- Review date: 2026-10-22

## DISC-002: Local skill and transcript materialization

- Reference: EXP-V1-0006 sections 3.5 and 3.7
- Implementation: Local core types preserve these declarations but the adapter does not materialize skills or emit an APS-V1-0004 envelope yet.
- Impact: End-to-end Local launches cannot claim conformance.
- Resolution: WILL-FIX
- Tests affected: Local skill and capture conformance
- Review date: 2026-10-22
