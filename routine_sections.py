"""Structure-aware decomposition of SAP SQL Anywhere routines.

The migration engine deliberately keeps this representation lossless: joining
the section ``text`` values always recreates the input.  It is therefore safe
to run narrowly-scoped converters (identifiers, datatypes, statements, and
control flow) without treating a routine as one undifferentiated string.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import re


@dataclass(frozen=True)
class RoutineSection:
    kind: str
    text: str
    start: int
    end: int

    def summary(self) -> dict:
        value = asdict(self)
        value.pop("text")
        value["line_start"] = 1  # replaced by RoutineDocument.summary
        value["line_end"] = 1
        value["characters"] = len(self.text)
        return value


@dataclass(frozen=True)
class RoutineDocument:
    sections: tuple[RoutineSection, ...]

    def render(self) -> str:
        return "".join(section.text for section in self.sections)

    def summary(self, source: str) -> list[dict]:
        result = []
        for section in self.sections:
            item = section.summary()
            item["line_start"] = source.count("\n", 0, section.start) + 1
            item["line_end"] = source.count("\n", 0, max(section.start, section.end - 1)) + 1
            result.append(item)
        return result


def section_pipeline() -> dict[str, list[str]]:
    """Return the ordered migration skills applicable to each routine section."""
    return {
        "declaration": ["object_mapping", "routine_classification"],
        "parameters": ["parameter_mapping", "datatype_mapping"],
        "return_contract": ["column_mapping", "datatype_mapping", "result_metadata"],
        "header_options": ["keyword_mapping"],
        "declarations": ["variable_mapping", "datatype_mapping"],
        "body": ["object_mapping", "keyword_mapping", "expression_rewrites", "control_flow"],
        "exception_handlers": ["error_handling", "keyword_mapping"],
    }


def split_asa_routine(source: str) -> RoutineDocument:
    """Split an ASA CREATE PROCEDURE/FUNCTION into migration stages.

    Sections are: ``preamble``, ``declaration``, ``parameters``,
    ``return_contract``, ``header_options``, ``declarations``, ``body``, and
    optionally ``exception_handlers``. Unknown header syntax is retained as
    header_options rather than discarded.
    """
    declaration = re.search(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC(?:EDURE)?|FUNCTION)\s+[\w.$\"\[\]]+",
        source, re.I,
    )
    if not declaration:
        raise ValueError("Unable to locate an ASA procedure or function declaration.")
    begin_at = _find_keyword(source, "BEGIN", declaration.end())
    if begin_at is None:
        raise ValueError("Unable to locate the ASA routine BEGIN block.")

    pieces: list[RoutineSection] = []
    _add(pieces, "preamble", source, 0, declaration.start())
    _add(pieces, "declaration", source, declaration.start(), declaration.end())

    cursor = declaration.end()
    open_paren = _next_nonspace(source, cursor)
    if open_paren < begin_at and source[open_paren:open_paren + 1] == "(":
        close = _matching_paren(source, open_paren)
        if close is not None and close < begin_at:
            _add(pieces, "parameters", source, cursor, close + 1)
            cursor = close + 1

    result_at = _find_keyword(source, "RESULT", cursor, begin_at)
    returns_at = _find_keyword(source, "RETURNS", cursor, begin_at)
    contract_at = min((x for x in (result_at, returns_at) if x is not None), default=None)
    if contract_at is not None:
        _add(pieces, "header_options", source, cursor, contract_at)
        contract_end = _contract_end(source, contract_at, begin_at)
        _add(pieces, "return_contract", source, contract_at, contract_end)
        cursor = contract_end
    _add(pieces, "header_options", source, cursor, begin_at)

    exception_at = _find_keyword(source, "EXCEPTION", begin_at + 5)
    declaration_end = _leading_declarations_end(source, begin_at + 5,
                                                 exception_at or len(source))
    _add(pieces, "declarations", source, begin_at, declaration_end)
    if exception_at is not None and exception_at >= declaration_end:
        _add(pieces, "body", source, declaration_end, exception_at)
        _add(pieces, "exception_handlers", source, exception_at, len(source))
    else:
        _add(pieces, "body", source, declaration_end, len(source))
    return RoutineDocument(tuple(pieces))


def _add(parts, kind, source, start, end):
    if end > start:
        parts.append(RoutineSection(kind, source[start:end], start, end))


def _next_nonspace(text, at):
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def _matching_paren(text, opening):
    depth = 0
    for index, token in _code_tokens(text, opening):
        if token == '(':
            depth += 1
        elif token == ')':
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_keyword(text, keyword, start=0, end=None):
    end = len(text) if end is None else end
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.I)
    for match in pattern.finditer(text, start, end):
        if _is_code_position(text, match.start()):
            return match.start()
    return None


def _contract_end(text, start, limit):
    opening = text.find('(', start, limit)
    if opening >= 0:
        closing = _matching_paren(text, opening)
        if closing is not None and closing < limit:
            return closing + 1
    line_end = text.find('\n', start, limit)
    return limit if line_end < 0 else line_end


def _leading_declarations_end(text, start, limit):
    at = start
    while True:
        match = re.match(r"\s*DECLARE\b", text[at:limit], re.I)
        if not match:
            return at
        semicolon = text.find(';', at + match.end(), limit)
        if semicolon < 0:
            return at
        at = semicolon + 1


def _is_code_position(text, position):
    return any(index == position for index, _ in _code_tokens(text, 0, position + 1))


def _code_tokens(text, start=0, stop=None):
    """Yield code characters, excluding quoted strings/identifiers and comments."""
    stop = len(text) if stop is None else stop
    index = start
    while index < stop:
        if text.startswith('--', index):
            newline = text.find('\n', index + 2, stop)
            index = stop if newline < 0 else newline + 1
            continue
        if text.startswith('/*', index):
            close = text.find('*/', index + 2, stop)
            index = stop if close < 0 else close + 2
            continue
        if text[index] in "'\"":
            quote = text[index]
            index += 1
            while index < stop:
                if text[index] == quote:
                    if index + 1 < stop and text[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        yield index, text[index]
        index += 1
