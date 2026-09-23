# Known Conformance Divergences

## DISC-001: Local repository hydration

- Reference: EXP-V1-0006 section 3.3
- Implementation: Local requires a pre-hydrated directory and rejects repository entries.
- Impact: Local cannot yet execute a manifest containing repositories.
- Resolution: WILL-FIX
- Tests affected: Local hydration conformance
- Review date: 2026-10-22

## DISC-003: Docker repository, skill, capability, and transcript materialization

- Reference: EXP-V1-0006 sections 3.3, 3.5, 3.6, and 3.7
- Implementation: Docker rejects unsupported declarations instead of silently accepting them.
- Impact: The Rust Docker adapter does not yet claim full functional conformance.
- Resolution: WILL-FIX
- Tests affected: Docker hydration and capture conformance
- Review date: 2026-10-22

## DISC-004: Portable Docker disk limits

- Reference: EXP-V1-0006 section 3.8
- Implementation: Docker enforces requested memory and CPU limits but rejects disk limits.
- Impact: A manifest with `disk_mb` cannot launch through Docker.
- Resolution: DOCUMENTED-PLATFORM-LIMITATION
- Tests affected: Docker resource conformance
- Review date: 2026-10-22

## DISC-002: Local skill and transcript materialization

- Reference: EXP-V1-0006 sections 3.5 and 3.7
- Implementation: Local core types preserve these declarations but the adapter does not materialize skills or emit an APS-V1-0004 envelope yet.
- Impact: End-to-end Local launches cannot claim conformance.
- Resolution: WILL-FIX
- Tests affected: Local skill and capture conformance
- Review date: 2026-10-22
