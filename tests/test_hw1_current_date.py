"""Date lookup uses the course clock through the registered SDK tool, offline."""

import asyncio

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from agent import db
from agent.agent import TOOLS_BY_ROLE
from agent.auth import AuthContext

SHOPPER = AuthContext(user_id=1, role="shopper")
MERCHANT = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")


def invoke_current_date(caller=SHOPPER):
    matches = [tool for tool in TOOLS_BY_ROLE[caller.role] if tool.name == "get_current_date"]
    assert len(matches) == 1, "The caller needs a registered get_current_date tool"
    tool = matches[0]
    assert tool.params_json_schema["properties"] == {}
    ctx = ToolContext.from_agent_context(
        RunContextWrapper(context=caller), "offline-current-date",
        tool_name="get_current_date", tool_arguments="{}",
    )
    return asyncio.run(tool.on_invoke_tool(ctx, "{}"))


@pytest.mark.parametrize("caller", [SHOPPER, MERCHANT, SUPPORT])
def test_every_role_can_read_the_course_date(world, caller):
    assert invoke_current_date(caller) == {
        "ok": True, "current_date": "2026-07-01", "source": "world_asof",
    }


def test_date_is_read_from_the_configured_database_on_each_call(world_copy):
    for configured_date in ("2032-02-29", "2032-03-01"):
        with db.connection() as conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'world_asof'", (configured_date,))
            conn.commit()
        before = world_copy.read_bytes()

        assert invoke_current_date() == {
            "ok": True, "current_date": configured_date, "source": "world_asof",
        }
        assert world_copy.read_bytes() == before


@pytest.mark.parametrize("configured_date,error", [(None, RuntimeError), ("not-a-date", ValueError)])
def test_missing_or_invalid_course_date_raises_without_inventing_today(
    world_copy, configured_date, error,
):
    with db.connection() as conn:
        if configured_date is None:
            conn.execute("DELETE FROM meta WHERE key = 'world_asof'")
        else:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'world_asof'", (configured_date,))
        conn.commit()

    with pytest.raises(error):
        invoke_current_date()
