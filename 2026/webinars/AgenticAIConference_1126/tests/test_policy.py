"""Unit tests for the pure SQL-reference extraction behind the policy check."""

import pytest

from agent.policy import STAR, extract_refs
from agent.warehouse import assert_read_only


def test_single_table_unqualified_columns():
    r = extract_refs("SELECT SUM(arr_usd) FROM subscriptions WHERE status = 'active'", "acme_corp")
    assert r.tables == {"subscriptions"}
    assert r.columns["subscriptions"] == {"arr_usd", "status"}


def test_aliases_resolve_to_tables():
    r = extract_refs(
        "SELECT d.name, AVG(c.base_salary) FROM compensation c "
        "JOIN employees e ON c.employee_id = e.employee_id "
        "JOIN departments d ON e.department_id = d.department_id GROUP BY 1",
        "acme_corp",
    )
    assert r.tables == {"compensation", "employees", "departments"}
    assert "base_salary" in r.columns["compensation"]
    assert "name" in r.columns["departments"]


def test_select_star_reads_everything():
    r = extract_refs("SELECT * FROM employees", "acme_corp")
    assert r.columns["employees"] == {STAR}


def test_qualified_star():
    r = extract_refs("SELECT e.* FROM employees e JOIN teams t ON e.team_id = t.team_id", "acme_corp")
    assert STAR in r.columns["employees"]
    assert STAR not in r.columns.get("teams", set())


def test_unqualified_column_in_join_is_unresolved():
    r = extract_refs(
        "SELECT email FROM employees e JOIN customer_contacts cc ON e.employee_id = cc.contact_id",
        "acme_corp",
    )
    assert "email" in r.unresolved


def test_cte_names_are_not_tables():
    r = extract_refs(
        "WITH active AS (SELECT * FROM subscriptions WHERE status = 'active') "
        "SELECT SUM(arr_usd) FROM active",
        "acme_corp",
    )
    assert r.tables == {"subscriptions"}


def test_fully_qualified_names_and_foreign_datasets():
    r = extract_refs(
        "SELECT * FROM `my-proj.acme_corp.orders` o "
        "JOIN `my-proj.other_ds.secrets` s ON o.order_id = s.id",
        "acme_corp",
    )
    assert r.tables == {"orders"}
    assert r.foreign == {"my-proj.other_ds.secrets"}


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders WHERE TRUE",
    "INSERT INTO orders (order_id) VALUES ('x')",
    "SELECT 1; SELECT 2",
    "DROP TABLE orders",
])
def test_rejects_non_select(sql):
    with pytest.raises(ValueError):
        assert_read_only(sql)


def test_accepts_union_and_with():
    assert_read_only("WITH x AS (SELECT 1 AS a) SELECT a FROM x UNION ALL SELECT 2")


def test_count_star_is_not_select_star():
    r = extract_refs("SELECT department_id, COUNT(*) FROM employees GROUP BY 1", "acme_corp")
    assert r.columns["employees"] == {"department_id"}


def test_select_star_inside_cte_counts():
    r = extract_refs("WITH e AS (SELECT * FROM employees) SELECT COUNT(*) FROM e", "acme_corp")
    assert STAR in r.columns["employees"]
