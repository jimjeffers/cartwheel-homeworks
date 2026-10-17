"""Cancellation authorization and state checks on temporary database copies."""

import pytest

from agent import db, tools
from agent.auth import AuthContext


SHOPPER = AuthContext(user_id=1, role="shopper")
MERCHANT = AuthContext(user_id=9001, role="merchant", store_id=1)
SUPPORT = AuthContext(user_id=9501, role="support")


@pytest.mark.parametrize("caller", [
    AuthContext(user_id=2, role="shopper"),
    AuthContext(user_id=9002, role="merchant", store_id=2),
])
def test_unauthorized_cancellation_does_not_disclose_order_state(world_copy, caller):
    result = tools.cancel_order(caller, 4127, "cancel this order")

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert set(result) == {"ok", "error", "reason"}
    assert caller.role in result["reason"]
    assert "4127" in result["reason"]
    assert "delivered" not in result["reason"].lower()
    with db.connection() as conn:
        assert db.get_order(conn, 4127).status == "delivered"


@pytest.mark.parametrize("caller", [SHOPPER, MERCHANT, SUPPORT])
@pytest.mark.parametrize("status", ["shipped", "delivered", "cancelled", "refunded"])
def test_every_role_requires_placed_status_to_cancel(world_copy, caller, status):
    with db.connection() as conn:
        db.set_order_status(conn, 4127, status)

    result = tools.cancel_order(caller, 4127, "changed my mind")

    assert result["ok"] is False
    assert result["error"] == "not_eligible"
    assert status in result["reason"]
    assert "before shipment" in result["reason"]
    with db.connection() as conn:
        assert db.get_order(conn, 4127).status == status


@pytest.mark.parametrize("caller", [MERCHANT, SUPPORT])
def test_authorized_staff_cancellation_persists(world_copy, caller):
    with db.connection() as conn:
        db.set_order_status(conn, 4127, "placed")

    result = tools.cancel_order(caller, 4127, "customer requested cancellation")

    assert result == {"ok": True, "order_id": 4127, "status": "cancelled"}
    with db.connection() as conn:
        assert db.get_order(conn, 4127).status == "cancelled"


def test_readonly_kill_switch_stops_cancellation_before_database_access(monkeypatch, tmp_path):
    missing_database = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("CARTWHEEL_DB", str(missing_database))
    monkeypatch.setenv("CARTWHEEL_KILL_SWITCH", "readonly")

    result = tools.cancel_order(SHOPPER, 4127, "changed my mind")

    assert result["ok"] is False
    assert result["error"] == "paused"
    assert result["reason"]
    assert not missing_database.exists()
