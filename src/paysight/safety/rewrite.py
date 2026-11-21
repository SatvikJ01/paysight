"""Row-level security by AST rewrite (SPEC §8.2).

Every reference to a governed view is replaced with a filtered subquery that keeps the
original alias, e.g.::

    FROM v_attempts_clean AS a
    -> FROM (SELECT * FROM v_attempts_clean WHERE region IN ('north')) AS a

The filter is added in code, so it holds no matter what SQL the LLM wrote.
"""

from __future__ import annotations

from sqlglot import exp

from paysight.safety.access import RolePolicy


class RowFilterError(ValueError):
    """A role's row filter cannot be applied to a referenced view (fail closed)."""


def apply_row_filters(
    tree: exp.Expression,
    role: RolePolicy,
    view_columns: dict[str, set[str]],
    cte_names: set[str],
) -> tuple[exp.Expression, list[str]]:
    """Return a rewritten copy of ``tree`` and a description of each filter applied.

    Args:
        tree: a parsed, already validated query.
        role: the role whose ``row_filters`` apply.
        view_columns: columns of each governed view, used to check the filter column exists.
        cte_names: names of CTEs in the query; references to them are not base views.

    Raises:
        RowFilterError: a view lacks a column the role is filtered on.
    """
    out = tree.copy()
    if not role.row_filters:
        return out, []

    applied: list[str] = []
    tables = [
        t
        for t in out.find_all(exp.Table)
        if isinstance(t.this, exp.Identifier) and not (t.name.lower() in cte_names and not t.db)
    ]
    for table in tables:
        view = table.name.lower()
        missing = [c for c in role.row_filters if c not in view_columns.get(view, set())]
        if missing:
            raise RowFilterError(f"cannot apply row filter on {missing} to {view}")
        predicate = exp.and_(
            *(
                exp.In(this=exp.column(col), expressions=[exp.Literal.string(v) for v in values])
                for col, values in sorted(role.row_filters.items())
            )
        )
        inner = exp.select("*").from_(exp.to_table(view)).where(predicate)
        table.replace(inner.subquery(table.alias_or_name))
        applied.append(
            f"{view}: "
            + " AND ".join(f"{c} IN ({', '.join(v)})" for c, v in sorted(role.row_filters.items()))
        )
    return out, applied
