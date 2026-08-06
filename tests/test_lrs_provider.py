"""LRS Procedure provider contract tests for Fetch URL."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as fetch_plugin


def test_lrs_provider_apis_registered() -> None:
    plugin = fetch_plugin.create_plugin()
    components = plugin.get_components()
    describe = next(item for item in components if item["type"] == "API" and item["name"] == "describe_procedures")
    invoke = next(item for item in components if item["type"] == "API" and item["name"] == "invoke_procedure")

    assert describe["metadata"]["public"] is True
    assert describe["metadata"]["version"] == "1"
    assert describe["metadata"]["lunagentic_extension"] == "procedures"
    assert describe["metadata"]["lunagentic_contract"] == "1"
    assert invoke["metadata"]["public"] is True
    assert invoke["metadata"]["version"] == "1"


def test_descriptor_contract() -> None:
    async def run() -> None:
        plugin = fetch_plugin.create_plugin()
        envelope = await plugin.describe_procedures()
        assert envelope["contract_version"] == "1"
        assert len(envelope["procedures"]) == 1
        definition = envelope["procedures"][0]
        assert definition == {
            "procedure_id": "fetch_url.fetch",
            "version": "1",
            "display_name": "抓取网页全文",
            "description": (
                "抓取 http/https URL；网页和 PDF 返回 Markdown，图片返回文字描述。"
                "支持字符窗口、超长内容总结或截断。"
            ),
            "arguments_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "start_char": {"type": "integer", "minimum": 0, "default": 0},
                    "end_char": {"type": "integer", "minimum": -1, "default": -1},
                    "on_exceed": {
                        "type": "string",
                        "enum": ["summarize", "truncate"],
                        "default": "summarize",
                    },
                    "summary_focus": {"type": "string", "maxLength": 2000, "default": ""},
                },
                "required": ["url"],
            },
            "result_schema": {"type": "object"},
            "idempotent": True,
            "timeout_seconds": 120,
            "external_cost_kind": "provider_metered",
            "enabled": True,
        }
        assert "return_image" not in definition["arguments_schema"]["properties"]

    asyncio.run(run())


def test_invoke_reuses_fetch_pipeline_without_binary() -> None:
    async def run() -> None:
        plugin = fetch_plugin.create_plugin()
        captured: dict = {}

        async def fake_invoke(**kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "content": "markdown",
                "final_url": "https://example.com/final",
                "provider": "jina",
                "processed": "full",
                "returned_range": [0, 8],
                "metadata": {"title": "Example"},
                "cached": False,
            }

        plugin._invoke_fetch_url = fake_invoke  # type: ignore[method-assign]
        result = await plugin.invoke_procedure(
            procedure_id="fetch_url.fetch",
            request_id="req_1",
            arguments={"url": "https://example.com"},
            scoped_metadata={"task_id": "lrs_1", "branch_id": "br_1"},
        )
        assert captured["return_image"] is False
        assert result["success"] is True
        assert result["data"]["content"] == "markdown"
        assert result["metadata"]["provider_plugin_id"] == "com.0-hz.fetch-url"
        assert result["metadata"]["provenance"][0]["url"] == "https://example.com/final"
        assert "content_items" not in result["data"]
        assert result.get("error") is None

    asyncio.run(run())


def test_unknown_procedure_and_invalid_arguments_are_structured() -> None:
    async def run() -> None:
        plugin = fetch_plugin.create_plugin()
        missing = await plugin.invoke_procedure("missing", "req_1", {}, {})
        invalid = await plugin.invoke_procedure("fetch_url.fetch", "req_2", {"url": "", "extra": 1}, {})
        assert missing["error"]["code"] == "procedure_unavailable"
        assert invalid["error"]["code"] == "invalid_arguments"
        assert missing["success"] is False
        assert invalid["success"] is False

    asyncio.run(run())


def test_invoke_failure_is_structured_without_secrets() -> None:
    async def run() -> None:
        plugin = fetch_plugin.create_plugin()

        async def fake_invoke(**kwargs):
            del kwargs
            return {"success": False, "content": "抓取失败：请求超时（https://example.com）"}

        plugin._invoke_fetch_url = fake_invoke  # type: ignore[method-assign]
        result = await plugin.invoke_procedure(
            procedure_id="fetch_url.fetch",
            request_id="req_fail",
            arguments={"url": "https://example.com"},
            scoped_metadata={},
        )
        assert result["success"] is False
        assert result["error"]["code"] == "fetch_failed"
        assert "请求超时" in result["error"]["message"]
        assert result["data"] is None or result["data"] == {}

    asyncio.run(run())
