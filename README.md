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

### GUI

Run the GUI with:

```bash
python gui.py
```

Select a `.sql` file with **Browse SQL**, or paste/type DDL directly into the left
input pane. Then choose the mode and dialect and press **Process**. Loading another
file replaces the current input; **Clear input** resets both the editor and its
selected-file path.

For masking, choose **Save location** under Mapping path. The mapping is saved as
`<object-name>_mapping.json`, using the first procedure or table declared in the
DDL. Use **JSON file** to select an existing mapping when unmasking.

The GUI displays input DDL, output DDL, and mapping JSON in separate panes. Use
**Copy output** or **Copy mapping** to place either result on the clipboard.
Embedded mapping is optional and disabled by default so output DDL remains clean.
