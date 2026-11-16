"""Bind whole values compared to lookup() using the same normalization.

This is a conservative SQLite token recognizer, not a general SQL rewriter.
SQL text, LIKE patterns, ranges and raw equality operands are never rewritten.
"""

from __future__ import annotations

import re
from collections import defaultdict

from bizreason.utils import normalize_lookup

TOKEN = re.compile(
    r"\s+|--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
    r"|\?\d*|[:@$][\w]+|[\w]+|==|!=|<>|<=|>=|\|\||.", re.UNICODE
)
LEFT = {"WHERE", "AND", "OR", "ON", "WHEN", "THEN", "ELSE", "SELECT", "HAVING", "NOT", "(", ","}
RIGHT = {"AND", "OR", "THEN", "ELSE", "END", "AS", "FROM", "WHERE", "GROUP", "ORDER", "HAVING",
         "LIMIT", "OFFSET", "UNION", "EXCEPT", "INTERSECT", ")", ",", ";"}


class ParameterBindingError(ValueError):
    pass


def _tokens(sql: str) -> list[str]:
    return [m[0] for m in TOKEN.finditer(sql)
            if not m[0].isspace() and not m[0].startswith(("--", "/*"))]


def _word(token: str) -> str:
    if token.startswith(('"', '`', '[')):
        token = token[1:-1]
    return token.upper()


def bind_lookup_parameters(sql: str, parameters: list) -> list:
    tokens = _tokens(sql)
    pairs, stack = {}, []
    parameter_at, uses, maximum = {}, defaultdict(set), 0
    for i, token in enumerate(tokens):
        if token == "(":
            stack.append(i)
        elif token == ")":
            if not stack:
                raise ParameterBindingError("Unbalanced SQL parentheses")
            opened = stack.pop()
            pairs[opened] = i
            pairs[i] = opened
        elif token.startswith("?"):
            index = int(token[1:]) if token[1:] else maximum + 1
            if index < 1:
                raise ParameterBindingError("Invalid positional parameter index")
            maximum = max(maximum, index)
            parameter_at[i] = index - 1
            uses[index - 1].add(i)
        elif token.startswith((":", "@", "$")):
            raise ParameterBindingError("Use positional ? bindings, not named parameters")
    if stack:
        raise ParameterBindingError("Unbalanced SQL parentheses")
    if maximum != len(parameters):
        if maximum == 0:
            return []
        if len(parameters) > maximum:
            parameters = parameters[:maximum]
        else:
            raise ParameterBindingError(f"SQL expects {maximum} positional bindings; got {len(parameters)}")

    def left_boundary(index):
        return index == 0 or _word(tokens[index - 1]) in LEFT

    def right_boundary(index):
        return index == len(tokens) - 1 or _word(tokens[index + 1]) in RIGHT

    normalize_uses, explicit_uses = set(), set()
    for i in range(len(tokens) - 1):
        if _word(tokens[i]) != "LOOKUP" or tokens[i + 1] != "(" or i + 1 not in pairs:
            continue
        end = pairs[i + 1]
        if end == i + 3 and i + 2 in parameter_at:
            explicit_uses.add(i + 2)  # lookup(?) already normalizes inside SQLite.
        start, stop = i, end
        while start > 0 and stop + 1 < len(tokens) and tokens[start - 1] == "(" and pairs.get(start - 1) == stop + 1:
            # Parentheses may group the result, but another function may transform it.
            if start > 1:
                prefix = _word(tokens[start - 2])
                if re.fullmatch(r"[A-Z_]\w*", prefix) and prefix not in LEFT | RIGHT:
                    break
            start, stop = start - 1, stop + 1
        if stop + 2 < len(tokens) and tokens[stop + 1] in {"=", "==", "!=", "<>"} and stop + 2 in parameter_at:
            target = stop + 2
            if not (left_boundary(start) and right_boundary(target)):
                raise ParameterBindingError("Complex lookup comparison needs explicit lookup(?) on the whole value")
            normalize_uses.add(target)
        if start >= 2 and tokens[start - 1] in {"=", "==", "!=", "<>"} and start - 2 in parameter_at:
            target = start - 2
            if not (left_boundary(target) and right_boundary(stop)):
                raise ParameterBindingError("Complex lookup comparison needs explicit lookup(?) on the whole value")
            normalize_uses.add(target)
        after = stop + 1
        if after < len(tokens) and _word(tokens[after]) == "NOT":
            after += 1
        if after + 1 < len(tokens) and _word(tokens[after]) == "IN" and tokens[after + 1] == "(" and left_boundary(start):
            opening, closing = after + 1, pairs.get(after + 1)
            if closing is None:
                continue
            if opening + 1 < closing and _word(tokens[opening + 1]) in {"SELECT", "WITH"}:
                continue
            # IN-list operands must be whole bound values, not SELECT/expression fragments.
            cursor = opening + 1
            while cursor < closing:
                if tokens[cursor] == "(" and cursor in pairs:
                    cursor = pairs[cursor] + 1
                    continue
                if cursor in parameter_at and (cursor == opening + 1 or tokens[cursor - 1] == ",") and (cursor + 1 == closing or tokens[cursor + 1] == ","):
                    normalize_uses.add(cursor)
                cursor += 1
    result = list(parameters)
    for position in normalize_uses:
        index = parameter_at[position]
        value = parameters[index]
        if value is None:
            continue
        normalized = normalize_lookup(value)
        if value != normalized and not uses[index] <= normalize_uses | explicit_uses:
            raise ParameterBindingError("A reused parameter mixes normalized and raw comparisons; use separate bindings")
        result[index] = normalized
    return result


def table_aliases(sql: str, physical_names: set[str]) -> dict[str, set[str]]:
    """Only direct FROM/JOIN aliases of actual catalog tables; no CTE lineage guesses."""
    tokens = _tokens(sql)
    known = {name.upper(): name for name in physical_names}
    aliases = defaultdict(set)
    reserved = RIGHT | LEFT | {"JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS", "USING", "INDEXED", "AS"}
    for i, token in enumerate(tokens[:-1]):
        if _word(token) not in {"FROM", "JOIN"}:
            continue
        source_index = i + 1
        if source_index + 2 < len(tokens) and _word(tokens[source_index]) in {"MAIN", "TEMP"} and tokens[source_index + 1] == ".":
            source_index += 2
        actual = known.get(_word(tokens[source_index]))
        if actual is None:
            continue
        cursor = source_index + 1
        if cursor < len(tokens) and _word(tokens[cursor]) == "AS":
            cursor += 1
        if cursor < len(tokens) and re.fullmatch(r'[\w]+|"(?:""|[^"])*"|`[^`]*`|\[[^\]]*\]', tokens[cursor]) and _word(tokens[cursor]) not in reserved:
            aliases[_word(tokens[cursor]).casefold()].add(actual)
    return dict(aliases)
