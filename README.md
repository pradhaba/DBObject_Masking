# DDL Masking Tool

This repository contains a small Python utility to mask SQL DDL object names and restore them later.

## What it does

- Parses DDL text to identify tables, views, procedures, functions, triggers, indexes, sequences, types, columns, routine parameters, and locally declared variables. Parameters and local variables are kept in separate mapping categories.
- Replaces those names with generated masked tokens like `TBL_1`, `PROC_1`, and `COL_1`.
- Outputs a masked DDL string plus a mapping file.
- Can reverse the masked names back to original values using the mapping.

## Usage

### Mask DDL

```bash
python masker.py mask --input original.sql --output masked.sql --mapping mapping.json --embed-mapping --dialect postgresql
```

- `--input`: source SQL file
- `--output`: output masked SQL file
- `--mapping`: JSON mapping file written for later unmasking
- `--embed-mapping`: attaches the mapping as SQL comment metadata inside the masked output
- `--dialect`: source dialect (`generic`, `sybase_asa`, `postgresql`)

### Unmask DDL

```bash
python masker.py unmask --input translated.sql --output restored.sql --mapping mapping.json
```

If the masked SQL contains the embedded mapping comment, `--mapping` can be omitted.
During unmasking, select the target dialect. Parameter placeholders are recognized
with or without `@`/`:`; PostgreSQL restores parameter names with a `p_` prefix,
while Sybase ASA restores `@`-prefixed names.

## Example workflow

1. Mask original DDL:
   - `python masker.py mask -i original.sql -o masked.sql -m mapping.json -e`
2. Send `masked.sql` to an AI translator.
3. Receive translated DDL from the AI.
4. Unmask translated DDL:
   - `python masker.py unmask -i translated.sql -o restored.sql -m mapping.json`

## Dependencies

- Python 3.8+
- Optional: `sqlparse` for better SQL parsing, but the tool works without it.

Install dependencies:

```bash
pip install -r requirements.txt
```

PostgreSQL connections use `psycopg`. SAP ASE/ASA connections use `pyodbc` and
also require the matching SAP ODBC driver to be installed and registered in
Windows; that proprietary native driver is not distributed by this repository.

### GUI

Run the GUI with:

```bash
python gui.py
```

The application now opens with a project workflow before the DDL workspace:

1. Create a project with its purpose (mask, unmask, or migrate), object scope
   (one, multiple, or all), source/target dialect, and connection details. SAP ASE
   and SAP SQL Anywhere (ASA) are supported dialect choices. Passwords
   remain in memory and are never written to the project file.
2. Optionally test the source connection (the matching PostgreSQL, Oracle, or SQL
   Server Python driver must be installed).
3. Upload a ZIP/TAR/TAR.GZ/TGZ archive or select one or more loose `.sql`, `.ddl`,
   or `.txt` object definitions.
4. Select individual objects or **Select all**, then choose masking or migration
   preparation. The masking workspace opens with the selected target dialect and
   lets you switch between every selected file.

## Skill-based migration

Migration is driven by SQLite rules rather than source-code branches. The
`masking_rules` and `unmasking_rules` tables configure identifier protection and
restoration. The `migration_skills` table contains enabled source/target dialect
skills with instructions and ordered transformations. A migration masks names,
applies the matching skill, restores names for the target dialect, and records the
skill ID, mapping JSON, input, and output in `processing_runs`.

The repository also includes the reusable Codex skill definition under
`skills/database-object-migration` for maintaining and extending this workflow.

### Controlled SAP ASA procedure lifecycle

The current production focus is SAP ASA procedures migrated to PostgreSQL
functions or procedures. Non-procedure ASA objects are rejected by this skill.
Every migration uses the active approved immutable skill version. The target
routine type and classification reason are stored with the processing run.

Use **Test in PostgreSQL** after migration to compile the generated DDL inside a
transaction that is always rolled back. Failures store SQLSTATE, message, error
position, SQL, run, project, and skill version, then create a correction proposal.
Use **Skill Studio** on the Home screen to enter and test a deterministic correction
rule. Approval creates and activates a new skill version and supersedes—but never
deletes or modifies—the previous version.

Application data is stored in the embedded SQLite database
`data/ddl_masker.sqlite3`. It is initialized automatically and records projects,
connection profiles (never passwords), uploads, object selections, and processing
history including source/target dialects, input/output DDL, and mappings. When
unmasking, the workspace automatically reuses the latest mapping saved for the
selected project object if no mapping file or embedded mapping is supplied.
Extracted project files are stored
below `~/.ddl_masker/workspaces`. Migration currently prepares
masked DDL for the chosen target database; the database-specific DDL translation
engine remains a separate next-stage integration.

Select a `.sql` file with **Browse SQL**, or paste/type DDL directly into the left
input pane. Then choose the mode and dialect and press **Process**. Loading another
file replaces the current input; **Clear input** resets both the editor and its
selected-file path.

The project workspace stores masking mappings directly in SQLite. No JSON mapping
path or file selection is required. Unmasking automatically retrieves the latest
mapping for the selected project object.

The GUI displays input DDL, output DDL, and mapping JSON in separate panes. Use
**Copy output** or **Copy mapping** to place either result on the clipboard.
Embedded mapping is optional and disabled by default so output DDL remains clean.
# PostgreSQL vocabulary and metadata

PostgreSQL keywords, type names, SQL special forms, and the offline built-in
function protection list are centralized in `postgresql_vocabulary.py`.  Do
not add private keyword or built-in lists to migration or formatting modules.

Function return types and overloads are not maintained as a static list.  When
a target connection is available, `result_metadata.py` queries `pg_catalog`
first (the authoritative catalog for that PostgreSQL server version), then the
project's default schema for custom routines.  Only source-specific functions
and deterministic aggregate rules that must work before deployment belong in
`_source_builtin_return_type`.
