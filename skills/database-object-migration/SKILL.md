---
name: database-object-migration
description: Migrate SQL DDL objects between PostgreSQL, Oracle, SQL Server, SAP ASE, and SAP SQL Anywhere/ASA using the application's SQLite-backed masking, unmasking, and migration rule registry. Use when adding, reviewing, testing, or executing a database object dialect migration or when maintaining rules in migration_skills.
---

# Database Object Migration

Use the embedded SQLite registry as the authority for masking and dialect conversion.

## Workflow

1. Identify the source and target dialect from the project.
2. Load enabled masking rules from `masking_rules`; never translate original identifiers directly.
3. Mask the source DDL and retain its mapping JSON.
4. Load the enabled `migration_skills` row matching the dialect pair.
5. For SAP ASA, reject non-procedure objects and deterministically classify the
   PostgreSQL target as a function or procedure.
6. Apply transformations in stored order while preserving masked identifiers.
7. Unmask using the target dialect's `unmasking_rules` row.
8. Store input DDL, output DDL, mapping, dialect pair, skill/version IDs, and
   classification in `processing_runs`.
9. Test PostgreSQL DDL in a rollback-only transaction. Convert failures into
   correction proposals; activate a new version only after testing and approval.
10. Report unsupported constructs for review instead of inventing equivalent semantics.

For registry fields and extension rules, read [references/registry.md](references/registry.md).
