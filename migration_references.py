"""Approved human corrections used as conservative migration lessons."""
from __future__ import annotations

from contextlib import closing
from difflib import SequenceMatcher
import json
import re


FEATURE_WORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "EXISTS", "JOIN",
    "CURSOR", "LOOP", "WHILE", "IF", "ELSE", "EXCEPTION", "SIGNAL",
    "COMMIT", "ROLLBACK", "EXECUTE", "RESULT", "RETURN", "LIST",
    "DATEADD", "LOCATE", "ISNULL", "IFNULL", "TOP", "FIRST",
}


def source_features(sql: str) -> set[str]:
    """Extract stable structural features without storing object-name tokens."""
    words = {match.group(0).upper() for match in re.finditer(r'\b[A-Za-z_]\w*\b', sql)}
    features = words & FEATURE_WORDS
    features.update(f"CREATE_{kind.upper()}" for kind in re.findall(
        r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|PROC|FUNCTION)\b', sql, re.I
    ))
    if re.search(r'\bFROM\b[^;]+,\s*(?:[\w".]+)', sql, re.I | re.S):
        features.add("COMMA_JOIN")
    if re.search(r'\bSELECT\b.*\bSELECT\b', sql, re.I | re.S):
        features.add("NESTED_SELECT")
    return features


def extract_reference_corrections(generated: str, corrected: str) -> list[tuple[str, str]]:
    """Extract replacement blocks; insert-only edits remain reference-only."""
    before_lines = generated.splitlines(keepends=True)
    after_lines = corrected.splitlines(keepends=True)
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    corrections = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"replace", "delete"} or i1 == i2:
            continue
        before = "".join(before_lines[i1:i2])
        after = "".join(after_lines[j1:j2])
        if before.strip() and before != after:
            corrections.append((before, after))
    return corrections


def apply_relevant_references(source_sql: str, generated_sql: str, source_dialect: str,
                              target_dialect: str, database_path) -> tuple[str, list[dict], list[dict]]:
    """Apply exact approved edits and return all structurally relevant lessons."""
    from database import connect, now
    incoming = source_features(source_sql)
    with closing(connect(database_path)) as db, db:
        rows = db.execute(
            """SELECT * FROM migration_references WHERE source_dialect=? AND target_dialect=?
            AND enabled=1 ORDER BY approved_at DESC,id DESC""",
            (source_dialect, target_dialect),
        ).fetchall()
        scored = []
        for row in rows:
            features = set(json.loads(row["feature_json"] or "[]"))
            union = incoming | features
            score = len(incoming & features) / len(union) if union else 0.0
            if score >= 0.20:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], -item[1]["id"]))
        output = generated_sql
        trace = []
        references = []
        consumed_fragments = set()
        for score, row in scored[:10]:
            applied = 0
            corrections = db.execute(
                """SELECT * FROM migration_reference_corrections
                WHERE reference_id=? AND auto_apply=1 ORDER BY occurrence_order,id""",
                (row["id"],),
            ).fetchall()
            for correction in corrections:
                before = correction["before_text"]
                # Eligibility is evaluated against the original draft. This
                # prevents one learned edit from manufacturing the input for a
                # second, unrelated reference. Newest approved reference wins
                # when multiple lessons replace the same exact fragment.
                if (before in generated_sql and before in output
                        and before not in consumed_fragments):
                    output = output.replace(before, correction["after_text"], 1)
                    consumed_fragments.add(before)
                    applied += 1
            references.append({
                "reference_id": row["id"], "similarity": round(score, 3),
                "reviewer": row["reviewer"], "approved_at": row["approved_at"],
                "notes": row["review_notes"], "applied_corrections": applied,
            })
            if applied:
                db.execute(
                    "UPDATE migration_references SET usage_count=usage_count+1,last_used_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                trace.append({
                    "line": "approved-reference", "source": f"Reference #{row['id']}",
                    "output": f"Applied {applied} exact approved correction(s)",
                    "rules": [{"rule_id": f"reference-{row['id']}",
                               "rule_code": "approved-migration-reference",
                               "priority": 2100, "matches": applied}],
                })
        return output, trace, references
