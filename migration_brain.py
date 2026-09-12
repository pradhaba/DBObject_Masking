"""Git-trackable, sanitized YAML knowledge generated from approved references."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import yaml

from masker import mask_text
from migration_references import source_features


BRAIN_DIR = Path(__file__).resolve().parent / "brain" / "lessons"


def export_masked_lesson(run: dict, corrected_ddl: str,
                         corrections: list[tuple[str, str]], brain_dir: Path | None = None) -> Path:
    """Create an immutable YAML lesson containing no unmasked identifiers."""
    masked_source, mapping = mask_text(run["input_ddl"], run["source_dialect"], False)
    documents = [masked_source,
                 _mask_target(run["generated_ddl"] if "generated_ddl" in run else run["output_ddl"], mapping),
                 _mask_target(corrected_ddl, mapping)]
    masked_corrections = [
        [_mask_target(before, mapping), _mask_target(after, mapping)]
        for before, after in corrections
    ]
    documents, masked_corrections = _mask_schemas(documents, masked_corrections)
    documents, masked_corrections = _anonymize_literals(documents, masked_corrections)
    core = {
        "schema_version": 1,
        "dialects": {"source": run["source_dialect"], "target": run["target_dialect"]},
        "features": sorted(source_features(run["input_ddl"])),
        "examples": {
            "source_masked": documents[0],
            "generated_masked": documents[1],
            "corrected_masked": documents[2],
        },
        "corrections": [
            {"before_masked": before, "after_masked": after, "reuse": "exact_or_renderer_review"}
            for before, after in masked_corrections
        ],
        "verification": {"status": "approved_human_reference"},
    }
    lesson_id = hashlib.sha256(_canonical_yaml_data(core)).hexdigest()[:20]
    payload = {"lesson_id": lesson_id, **core}
    _assert_sanitized(payload, mapping)
    brain_dir = Path(brain_dir or BRAIN_DIR)
    brain_dir.mkdir(parents=True, exist_ok=True)
    destination = brain_dir / f"lesson-{lesson_id}.yaml"
    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    if destination.exists():
        existing = yaml.safe_load(destination.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(f"Immutable brain lesson collision: {destination.name}")
        return destination
    temporary = destination.with_suffix(".yaml.writing")
    temporary.write_text(serialized, encoding="utf-8", newline="\n")
    temporary.replace(destination)
    return destination


def load_brain_lessons(brain_dir: Path | None = None) -> list[dict]:
    lessons = []
    brain_dir = Path(brain_dir or BRAIN_DIR)
    if not Path(brain_dir).exists():
        return lessons
    for path in sorted(Path(brain_dir).glob("lesson-*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"Unsupported migration brain lesson: {path.name}")
        payload["knowledge_path"] = str(path)
        lessons.append(payload)
    return lessons


def relevant_brain_lessons(source_sql: str, source_dialect: str, target_dialect: str,
                           brain_dir: Path | None = None) -> list[dict]:
    incoming = source_features(source_sql)
    matches = []
    for lesson in load_brain_lessons(brain_dir):
        dialects = lesson.get("dialects", {})
        if dialects != {"source": source_dialect, "target": target_dialect}:
            continue
        features = set(lesson.get("features", []))
        union = incoming | features
        score = len(incoming & features) / len(union) if union else 0.0
        if score >= 0.20:
            matches.append({
                "lesson_id": lesson["lesson_id"], "similarity": round(score, 3),
                "knowledge_path": lesson["knowledge_path"],
                "correction_count": len(lesson.get("corrections", [])),
            })
    return sorted(matches, key=lambda item: (-item["similarity"], item["lesson_id"]))[:10]


def _mask_target(sql: str, mapping: dict) -> str:
    value = sql
    replacements = []
    for category, entries in mapping.items():
        if not isinstance(entries, dict):
            continue
        for original, token in entries.items():
            replacements.append((str(original).lstrip('@'), str(token), category))
    for original, token, category in sorted(replacements, key=lambda item: -len(item[0])):
        variants = [original]
        if category == "parameters":
            variants.extend((f"p_{original}", f"@{original}"))
        elif category == "variables":
            variants.extend((f"_{original}", f"@{original}"))
        for variant in sorted(set(variants), key=len, reverse=True):
            value = re.sub(
                rf'(?<![A-Za-z0-9_$])"?{re.escape(variant)}"?(?![A-Za-z0-9_$])',
                token, value, flags=re.I,
            )
    aliases = []
    for match in re.finditer(
        r'\b(?:FROM|JOIN)\s+(?:[\w"]+\.)?(?:TBL_\d+|VW_\d+)\s+(?:AS\s+)?("?[A-Za-z_]\w*"?)',
        value, re.I,
    ):
        alias = match.group(1).strip('"')
        if alias.upper() not in {"ON", "WHERE", "JOIN"} and alias not in aliases:
            aliases.append(alias)
    for index, alias in enumerate(aliases, start=1):
        value = re.sub(rf'(?<!\w)"?{re.escape(alias)}"?(?!\w)', f"ALIAS_{index}", value, flags=re.I)
    return value


def _anonymize_literals(documents, corrections):
    string_values = {}
    def sanitize(text):
        def replace_string(match):
            raw = match.group(0)
            key = raw[1:-1].replace("''", "'")
            if key not in string_values:
                string_values[key] = f"TEXT_LITERAL_{len(string_values) + 1}"
            return "'" + string_values[key] + "'"
        return re.sub(r"'(?:''|[^'])*'", replace_string, text)
    sanitized_documents = [sanitize(item) for item in documents]
    sanitized_corrections = [[sanitize(before), sanitize(after)] for before, after in corrections]
    return sanitized_documents, sanitized_corrections


def _mask_schemas(documents, corrections):
    schemas = []
    combined = "\n".join(documents)
    for match in re.finditer(r'(?<!\w)"?([A-Za-z_]\w*)"?\s*\.\s*(?:TBL|VW|PROC|FUNC|TYPE)_\d+', combined, re.I):
        schema = match.group(1)
        if schema.upper() not in schemas:
            schemas.append(schema.upper())
    def sanitize(text):
        for index, schema in enumerate(schemas, start=1):
            text = re.sub(
                rf'(?<!\w)"?{re.escape(schema)}"?(?=\s*\.)',
                f'SCHEMA_{index}', text, flags=re.I,
            )
        return text
    return [sanitize(item) for item in documents], [
        [sanitize(before), sanitize(after)] for before, after in corrections
    ]


def _assert_sanitized(payload, mapping):
    serialized = json.dumps(
        {"examples": payload.get("examples", {}), "corrections": payload.get("corrections", [])},
        ensure_ascii=False,
    )
    leaked = []
    for entries in mapping.values():
        if not isinstance(entries, dict):
            continue
        for original in entries:
            name = str(original).lstrip('@')
            if len(name) > 2 and re.search(rf'(?<!\w){re.escape(name)}(?!\w)', serialized, re.I):
                leaked.append(name)
    if leaked:
        raise ValueError("Brain lesson sanitization failed for identifier(s): " + ", ".join(sorted(set(leaked))))


def _canonical_yaml_data(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
