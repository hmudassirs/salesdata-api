"""SQL statement classification for authorization and concurrency control.

Two independent classifications, both purely textual (no real SQL
parser dependency was already present in this codebase, and adding one
is out of scope for this pass -- these heuristics are deliberately
conservative: anything ambiguous is classified as the *more*
restrictive/expensive option rather than the more permissive one):

- `classify_operation()` -- roadmap Phase 13.1 ("read-only query API"
  vs "full DB console" decision). This deployment chose "DB console
  with scope-gated writes" -- see AppSettings.require_write_scope_for_mutations
  for the reasoning. SELECT/WITH is "read" *only* when no DML/DDL
  keyword appears anywhere in the statement (see the writable-CTE note
  below); everything else -- INSERT, UPDATE, DELETE, DDL, or anything
  unrecognized -- is "write", so an unrecognized statement is never
  accidentally treated as safe-to-run-unscoped.

- `classify_cost()` -- roadmap Phase 14. A rough FAST/NORMAL/EXPENSIVE
  bucket used only to pick which concurrency semaphore a query waits
  on before reaching the DB pool; it is not a query planner and makes
  no claim about actual execution cost. Being wrong just means a query
  waits on a different semaphore than ideal, not an incorrect result.
"""

import re
from typing import Literal

Operation = Literal["read", "write"]
Cost = Literal["fast", "normal", "expensive"]

_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.DOTALL)
_FIRST_WORD_RE = re.compile(r"^\s*([A-Za-z]+)")

_READ_KEYWORDS = {"SELECT", "WITH"}

# Postgres/DuckDB (and, since 3.35, SQLite) support *writable* CTEs --
# `WITH x AS (...) DELETE FROM orders ...` -- so a leading `WITH` does
# NOT guarantee the statement is read-only. Without this check, a
# caller holding only the "read" scope could smuggle a write past
# classify_operation() by prefixing it with a trivial CTE. Any of
# these keywords appearing anywhere in a `WITH`-led statement forces
# it to "write" -- conservative by design, same as the rest of this
# module.
_WRITE_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Table-name extraction for cache invalidation (see
# core.caching.query_cache_coordinator.invalidate_tables). Purely
# heuristic, same conservative spirit as the rest of this module: it
# only needs to be reliable enough to *narrow* invalidation, since
# callers fall back to a full cache clear whenever no table can be
# resolved.
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# Any of these appearing (as whole words) anywhere in the statement
# marks it as at least NORMAL cost; combinations push it to EXPENSIVE.
_NORMAL_MARKERS = re.compile(
    r"\b(JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|DISTINCT|UNION)\b", re.IGNORECASE
)
_EXPENSIVE_MARKERS = re.compile(
    r"\b(CROSS\s+JOIN|GROUP\s+BY|WINDOW|OVER\s*\(|RECURSIVE)\b", re.IGNORECASE
)
_HAS_LIMIT = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)
_SELECT_STAR = re.compile(r"SELECT\s+\*", re.IGNORECASE)


def _first_keyword(sql: str) -> str:
    """Return the statement's leading keyword, skipping leading
    comments/whitespace. Empty string if the statement is blank/only
    comments -- callers treat that as non-read (safe default)."""
    stripped = _LEADING_COMMENT_RE.sub("", sql or "")
    match = _FIRST_WORD_RE.match(stripped)
    return match.group(1).upper() if match else ""


def classify_operation(sql: str) -> Operation:
    """Classify a statement as "read" (SELECT, or WITH with no writable
    CTE) or "write" (anything else, including unrecognized/empty input
    -- deny-by-default).

    A leading `WITH` is only "read" if no DML/DDL keyword appears
    anywhere in the statement -- see the writable-CTE note above
    `_WRITE_KEYWORDS_RE`.
    """
    keyword = _first_keyword(sql)
    if keyword not in _READ_KEYWORDS:
        return "write"
    if keyword == "WITH" and _WRITE_KEYWORDS_RE.search(sql):
        return "write"
    return "read"


def extract_tables(sql: str) -> set[str]:
    """Best-effort set of table names referenced by `sql`.

    Used to narrow query-cache invalidation to just the tables a write
    statement could have affected, instead of always clearing the
    whole cache. Deliberately conservative in the opposite direction
    from the rest of this module: an empty/uncertain result should
    make the *caller* fall back to a full cache clear, not silently
    skip invalidation.
    """
    return {m.group(1) for m in _TABLE_REF_RE.finditer(sql or "")}


def classify_cost(sql: str) -> Cost:
    """Rough concurrency-control bucket -- see module docstring."""
    if not sql or not sql.strip():
        return "fast"

    if _EXPENSIVE_MARKERS.search(sql):
        return "expensive"

    is_normal = bool(_NORMAL_MARKERS.search(sql))
    # An unbounded `SELECT *` with no LIMIT is exactly the "wide scan,
    # large result set" case Phase 14 calls out, independent of whether
    # it also has a JOIN/GROUP BY.
    unbounded_wide_scan = bool(_SELECT_STAR.search(sql)) and not _HAS_LIMIT.search(sql)

    if is_normal and unbounded_wide_scan:
        return "expensive"
    if is_normal or unbounded_wide_scan:
        return "normal"
    return "fast"


def has_scope(scopes: str | None, required: str) -> bool:
    """Check whether a comma-separated scopes string grants `required`.

    No scopes at all is treated as "read" only (safe default) rather
    than either "no access" (would break every existing caller that
    never set scopes) or "full access" (would silently defeat the
    write gate for exactly the callers who most need it -- the ones
    who never thought about scopes at all).
    """
    granted = {s.strip().lower() for s in (scopes or "").split(",") if s.strip()}
    if not granted:
        granted = {"read"}
    return required.lower() in granted
