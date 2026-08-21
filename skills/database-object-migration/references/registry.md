# Rule registry

`masking_rules` controls enabled object categories, token prefixes, and processing order.

`unmasking_rules` controls target-dialect parameter prefixes, variable prefixes, and SAP-style `@` preservation.

`migration_skills` contains one enabled row per source/target pair:

- `instructions`: human/agent guidance for semantic conversion.
- `transformations_json`: ordered `{pattern, replacement}` regex operations for deterministic conversions.
- `enabled`: makes a skill available without changing application code.

Treat regex transformations as conservative syntax rewrites. Add procedural conversions only after tests cover representative routines, exception handling, transactions, temporary tables, identity/sequence behavior, and datatype edge cases.
