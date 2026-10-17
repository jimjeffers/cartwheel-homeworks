"""Store configuration review contracts, using only temporary course data."""

import asyncio
import shutil
from types import SimpleNamespace

import pytest
import yaml
from agents import RunContextWrapper
from agents.exceptions import ModelBehaviorError
from agents.tool_context import ToolContext

from agent import config, tools
from agent.agent import TOOLS_BY_ROLE, search_help_center_logic
from agent.auth import AuthContext
from seed.policies import render_platform_policies, render_store_policy

MERCHANT = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")
SHOPPER = AuthContext(user_id=1, role="shopper")


@pytest.fixture
def policies_copy(world, tmp_path, monkeypatch):
    target = tmp_path / "policies"
    shutil.copytree(world["policies"], target)
    monkeypatch.setenv("CARTWHEEL_POLICIES_DIR", str(target))
    return target


def test_merchant_defaults_to_own_store_and_receives_citable_sources(world):
    result = tools.get_store_policies_logic(MERCHANT)

    assert result["ok"] is True
    assert result["store"] == {
        "store_id": 2, "name": "Juniper Home Goods", "slug": "juniper-home-goods",
    }
    assert result["has_store_overrides"] is True
    assert result["configured_overrides"] == {
        "return_window_days": 14, "restocking_fee_opt_in": False,
    }
    assert result["effective_rules"] == {
        "return_window_days": 14,
        "restocking_fee": {"enabled": False, "maximum_percent": 0, "opened_items_only": True},
    }
    assert [doc["policy_id"] for doc in result["policies"]] == [
        "cw-restocking-fees", "cw-returns", "cw-store-overrides",
        "store-juniper-home-goods-policy",
    ]
    assert result["count"] == 4
    for doc in result["policies"]:
        assert doc["title"] and doc["audience"] == "all"
        assert doc["body"].startswith("# ")
        assert doc["scope"] == ("store" if doc["policy_id"].startswith("store-") else "platform")
    assert "within 14 days" in result["policies"][-1]["body"]
    assert tools.get_store_policies_logic(MERCHANT, 2) == result


@pytest.mark.parametrize("store_id,window,fees,has_overrides,count", [
    (1, 30, False, False, 3),  # No store page: platform defaults still succeed.
    (5, 30, True, True, 4),
    (7, 45, False, True, 4),
])
def test_support_can_review_any_store_and_apply_its_settings(
    world, store_id, window, fees, has_overrides, count,
):
    result = tools.get_store_policies_logic(SUPPORT, store_id)

    assert result["ok"] is True
    assert result["store"]["store_id"] == store_id
    assert result["has_store_overrides"] is has_overrides
    assert result["effective_rules"] == {
        "return_window_days": window,
        "restocking_fee": {"enabled": fees, "maximum_percent": 15 if fees else 0, "opened_items_only": True},
    }
    assert result["count"] == len(result["policies"]) == count


@pytest.mark.parametrize("caller,store_id", [
    (SHOPPER, 2),
    (SimpleNamespace(role="admin", user_id=9501, store_id=None), 2),
    (SimpleNamespace(role="support", user_id=None, store_id=None), 2),
    (SimpleNamespace(role="support", user_id=0, store_id=None), 2),
    (SimpleNamespace(role="merchant", user_id=None, store_id=2), None),
    (SimpleNamespace(role="merchant", user_id=9002, store_id=None), 2),
    (SimpleNamespace(role="merchant", user_id=9002, store_id=0), 2),
    (MERCHANT, 1),
    (MERCHANT, 999999),
])
def test_unauthorized_call_is_denied_before_accessing_store_or_policy_data(
    caller, store_id, tmp_path, monkeypatch,
):
    monkeypatch.setenv("CARTWHEEL_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("CARTWHEEL_POLICIES_DIR", str(tmp_path / "missing-policies"))

    result = tools.get_store_policies_logic(caller, store_id)

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    assert result["reason"]
    assert "store" not in result and "policies" not in result


@pytest.mark.parametrize("caller,store_id", [
    (SUPPORT, None), (SUPPORT, 0), (SUPPORT, -1), (SUPPORT, True),
    (SUPPORT, 2.5), (SUPPORT, "2"), (MERCHANT, False),
])
def test_invalid_selector_is_rejected_before_database_access(
    caller, store_id, tmp_path, monkeypatch,
):
    monkeypatch.setenv("CARTWHEEL_DB", str(tmp_path / "missing.db"))

    result = tools.get_store_policies_logic(caller, store_id)

    assert result["ok"] is False
    assert result["error"] == "invalid_argument"
    assert result["reason"]


@pytest.mark.parametrize("caller", [SUPPORT, AuthContext(9999, "merchant", 999999)])
def test_authorized_unknown_store_is_not_found(world, caller):
    result = tools.get_store_policies_logic(caller, 999999)
    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert result["reason"]


@pytest.mark.parametrize("policy_id,store_id", [
    ("cw-returns", 1), ("cw-restocking-fees", 2), ("cw-store-overrides", 2),
    ("store-juniper-home-goods-policy", 2), ("store-cascade-audio-policy", 5),
])
def test_missing_required_document_is_inconsistent_data(policies_copy, policy_id, store_id):
    (policies_copy / f"{policy_id}.md").unlink()

    result = tools.get_store_policies_logic(SUPPORT, store_id)

    assert result["ok"] is False
    assert result["error"] == "inconsistent_data"
    assert policy_id in result["reason"]
    assert "effective_rules" not in result


def test_effective_defaults_and_limits_come_from_facts(world, policies_copy, tmp_path, monkeypatch):
    facts = yaml.safe_load(config.facts_path().read_text())
    facts.update(return_window_days=40, restocking_fee_max_percent=9)
    facts_file = tmp_path / "facts.yaml"
    facts_file.write_text(yaml.safe_dump(facts))
    monkeypatch.setattr(config, "facts_path", lambda: facts_file)
    docs = render_platform_policies(facts)
    docs.append(render_store_policy(facts, "Cascade Audio", "cascade-audio", None, True))
    for doc in docs:
        (policies_copy / f"{doc.policy_id}.md").write_text(doc.to_markdown())

    result = tools.get_store_policies_logic(SUPPORT, 5)

    assert result["effective_rules"]["return_window_days"] == 40
    assert result["effective_rules"]["restocking_fee"]["maximum_percent"] == 9


def test_review_does_not_modify_database_or_policy_files(world_copy, policies_copy):
    before_db = world_copy.read_bytes()
    before_docs = {p.name: p.read_bytes() for p in policies_copy.iterdir()}

    assert tools.get_store_policies_logic(MERCHANT)["ok"] is True

    assert world_copy.read_bytes() == before_db
    assert {p.name: p.read_bytes() for p in policies_copy.iterdir()} == before_docs


@pytest.mark.parametrize("failure", ["database", "directory", "parse"])
def test_unexpected_execution_failure_propagates(policies_copy, tmp_path, monkeypatch, failure):
    if failure == "database":
        monkeypatch.setenv("CARTWHEEL_DB", str(tmp_path / "missing.db"))
    elif failure == "directory":
        monkeypatch.setenv("CARTWHEEL_POLICIES_DIR", str(tmp_path / "missing-policies"))
    else:
        (policies_copy / "cw-returns.md").write_text("---\npolicy_id: [\n---\nbody")

    with pytest.raises(yaml.YAMLError if failure == "parse" else FileNotFoundError):
        tools.get_store_policies_logic(MERCHANT)


def test_sdk_registers_review_only_for_merchants_and_support():
    assert "get_store_policies" not in {tool.name for tool in TOOLS_BY_ROLE["shopper"]}
    for role in ("merchant", "support"):
        assert tools.get_store_policies in TOOLS_BY_ROLE[role]
    assert set(tools.get_store_policies.params_json_schema["properties"]) == {"store_id"}


def test_sdk_call_uses_injected_context_and_propagates_execution_errors(world, tmp_path, monkeypatch):
    arguments = '{"store_id": null}'
    ctx = ToolContext.from_agent_context(
        RunContextWrapper(context=MERCHANT), "offline-test",
        tool_name="get_store_policies", tool_arguments=arguments,
    )
    result = asyncio.run(tools.get_store_policies.on_invoke_tool(ctx, arguments))
    assert result["ok"] is True and result["store"]["store_id"] == 2

    monkeypatch.setenv("CARTWHEEL_DB", str(tmp_path / "missing.db"))
    with pytest.raises(FileNotFoundError):
        asyncio.run(tools.get_store_policies.on_invoke_tool(ctx, arguments))


@pytest.mark.parametrize("arguments", [
    '{"store_id": true}', '{"store_id": "2"}', '{"store_id": 2.0}',
])
def test_sdk_does_not_coerce_invalid_types_into_authorized_store_ids(world, arguments):
    ctx = ToolContext.from_agent_context(
        RunContextWrapper(context=SUPPORT), "offline-invalid-type",
        tool_name="get_store_policies", tool_arguments=arguments,
    )
    with pytest.raises(ModelBehaviorError):
        asyncio.run(tools.get_store_policies.on_invoke_tool(ctx, arguments))


@pytest.mark.parametrize("caller", [SHOPPER, MERCHANT, SUPPORT])
def test_published_store_policies_remain_public(world, caller):
    result = tools.get_policy(caller, "store-juniper-home-goods-policy")
    assert result["ok"] is True and "within 14 days" in result["body"]
    search = search_help_center_logic(caller, "Juniper Home Goods")
    assert "store-juniper-home-goods-policy" in {doc["policy_id"] for doc in search["results"]}
