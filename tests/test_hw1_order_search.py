"""Order-search boundaries not covered by the supplied role/older-order cases."""

from types import SimpleNamespace

import pytest

from agent import db, tools
from agent.auth import AuthContext


SHOPPER = AuthContext(user_id=1, role="shopper")


@pytest.mark.parametrize("caller", [
    SimpleNamespace(user_id=1, role="admin", store_id=None),
    SimpleNamespace(user_id=None, role="shopper", store_id=None),
    SimpleNamespace(user_id=9002, role="merchant", store_id=None),
    SimpleNamespace(user_id=None, role="merchant", store_id=2),
    SimpleNamespace(user_id=None, role="support", store_id=None),
])
def test_invalid_identity_is_denied_before_database_access(caller, monkeypatch, tmp_path):
    monkeypatch.setenv("CARTWHEEL_DB", str(tmp_path / "missing.db"))

    result = tools.find_order(caller, "mug")

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert result["reason"]


@pytest.mark.parametrize("query", [
    "  SEARCH FIXTURE  ",
    "please find my unique search fixture product order",
])
def test_case_insensitive_name_fragments_and_names_in_sentences(order_search_cases, query):
    _title, expected = order_search_cases

    result = tools.find_order(SHOPPER, query)

    assert result["ok"] is True
    assert [order["order_id"] for order in result["orders"]] == expected["shopper"]


def test_blank_query_does_not_match_every_order(world):
    assert tools.find_order(SHOPPER, " \t\n") == {"ok": True, "orders": []}


def test_missing_product_title_does_not_match_arbitrary_queries(order_search_cases):
    title, _expected = order_search_cases
    with db.connection() as conn:
        conn.execute("UPDATE products SET title = '' WHERE title = ?", (title,))
        conn.commit()

    assert tools.find_order(SHOPPER, "zzzznonexistent9999") == {"ok": True, "orders": []}
