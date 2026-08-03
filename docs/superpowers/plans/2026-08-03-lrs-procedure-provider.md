# Fetch URL：LRS Procedure Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有 `fetch_url` Tool 与 `fetch_url@1` API 的前提下，为 Fetch URL 增加 LRS `describe_procedures@1` / `invoke_procedure@1` provider 契约，使网页全文抓取能被麦麦深度调查组自动发现。

**Architecture:** 继续复用 `FetchUrlPlugin._invoke_fetch_url()` 作为唯一抓取实现；新的 descriptor 返回一项严格 Procedure definition，invoker 校验统一参数、固定 `return_image=False`、调用现有入口并把结果包装成 LRS `ProcedureResult`。provider 配置、Jina/VLM keys 与 timeout 仍完全归 Fetch URL 插件所有。

**Tech Stack:** 现有 Fetch URL dependencies、MaiBot SDK `@API`、Pydantic 2（SDK 已依赖）、pytest/offline smoke。

## Global Constraints

- 在独立 first-party 仓库 `maibot-fetch-url-plugin/` 工作；不修改 LRS、Host 或 SDK。
- 保留现有 `@Tool("fetch_url")` 与公开 `@API("fetch_url", version="1")` 的名称、参数和返回兼容性。
- 新增功能是向后兼容 feature，manifest 版本从 `0.3.0` 递增到 `0.4.0`；插件 config schema 未变化，不递增 config_version、不改用户 live `config.toml`。
- `describe_procedures` 必须 `public=True`、version `1`，metadata 固定 `lunagentic_extension="procedures"`、`lunagentic_contract="1"`。
- `invoke_procedure` 必须 `public=True`、version `1`；只接受 procedure ID `fetch_url.fetch`。
- Provider 不保存 LRS scoped metadata，不读取其他 branch context，不把 API key/cookie/header 写入结果或日志。
- LRS 调用固定 `return_image=False`，图片 URL只返回现有文字描述/元数据，不回传 base64，避免 Procedure payload携带大块二进制。
- Provider 不实现自己的网络重试；沿用 Fetch URL 现有抓取策略。Procedure definition声明 idempotent=true，是否重试由 LRS 统一决定并复用 request_id。
- 新增测试继续使用仓库现有 `PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py` / pytest 方式，不引入新 dependency。

---

### Task 1: 定义 descriptor 与 invoker contract tests

**Files:**
- Modify: `tests/smoke_test.py`
- Create: `tests/test_lrs_provider.py`

**Interfaces:**
- Consumes: 当前 `create_plugin()`/`get_components()` 与 `_invoke_fetch_url()`。
- Produces: 锁定 provider API metadata、definition envelope、参数和 normalized result 的失败测试。

- [ ] **Step 1: 写 API 注册与 descriptor 失败测试**

```python
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


@pytest.mark.asyncio
async def test_descriptor_contract() -> None:
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
                "on_exceed": {"type": "string", "enum": ["summarize", "truncate"], "default": "summarize"},
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
```

- [ ] **Step 2: 写 invoker success/failure/validation 测试**

```python
@pytest.mark.asyncio
async def test_invoke_reuses_fetch_pipeline_without_binary(monkeypatch) -> None:
    plugin = fetch_plugin.create_plugin()
    captured = {}

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

    monkeypatch.setattr(plugin, "_invoke_fetch_url", fake_invoke)
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


@pytest.mark.asyncio
async def test_unknown_procedure_and_invalid_arguments_are_structured() -> None:
    plugin = fetch_plugin.create_plugin()
    missing = await plugin.invoke_procedure("missing", "req_1", {}, {})
    invalid = await plugin.invoke_procedure("fetch_url.fetch", "req_2", {"url": "", "extra": 1}, {})
    assert missing["error"]["code"] == "procedure_unavailable"
    assert invalid["error"]["code"] == "invalid_arguments"
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `PYTHONPATH=.:../maibot-plugin-sdk pytest tests/test_lrs_provider.py -v`

Expected: FAIL，因为两个 API 尚未实现。

- [ ] **Step 4: 提交 contract tests**

```bash
git add tests/test_lrs_provider.py tests/smoke_test.py
git commit -m "test: define LRS fetch provider contract"
```

### Task 2: 实现 descriptor、严格参数与统一结果包装

**Files:**
- Modify: `plugin.py`
- Modify: `tests/test_lrs_provider.py`

**Interfaces:**
- Consumes: Task 1 fixed contract、现有 `_invoke_fetch_url()`。
- Produces: `FetchProcedureArguments`、`describe_procedures()`、`invoke_procedure()`、`_normalize_lrs_procedure_result()`。

- [ ] **Step 1: 定义 strict 参数模型和 definition 常量**

在现有 imports 加 `ConfigDict`/Pydantic `BaseModel`（或从 SDK 当前公开依赖 import Pydantic），模型禁止 extra并保持现有默认：

```python
class FetchProcedureArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=8192)
    start_char: int = Field(default=0, ge=0)
    end_char: int = Field(default=-1, ge=-1)
    on_exceed: Literal["summarize", "truncate"] = "summarize"
    summary_focus: str = Field(default="", max_length=2000)
```

`FETCH_PROCEDURE_DEFINITION` 精确等于 Task 1 expected dict，返回前用 `deepcopy`，防止调用方修改 module constant。

- [ ] **Step 2: 实现 tagged descriptor API**

```python
@API(
    "describe_procedures",
    description="列出 Fetch URL 提供给 Lunagentic Research Swarm 的 Procedure",
    version="1",
    public=True,
    lunagentic_extension="procedures",
    lunagentic_contract="1",
)
async def describe_procedures(self) -> dict[str, Any]:
    return {"contract_version": "1", "procedures": [deepcopy(FETCH_PROCEDURE_DEFINITION)]}
```

不要把 plugin config、Jina key、cookie、proxy 或内部 cache state 放入 descriptor。

- [ ] **Step 3: 实现 invoker 与 normalized wrapper**

```python
@API(
    "invoke_procedure",
    description="按 LRS Procedure contract 调用 Fetch URL",
    version="1",
    public=True,
)
async def invoke_procedure(
    self,
    procedure_id: str,
    request_id: str,
    arguments: dict[str, Any],
    scoped_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del scoped_metadata
    started = time.monotonic()
    if procedure_id != "fetch_url.fetch":
        return _procedure_error("procedure_unavailable", f"未知 Procedure：{procedure_id}", started)
    try:
        parsed = FetchProcedureArguments.model_validate(arguments)
    except ValidationError as exc:
        return _procedure_error("invalid_arguments", "Fetch URL Procedure 参数无效", started, exc.errors())
    result = await self._invoke_fetch_url(
        url=parsed.url,
        start_char=parsed.start_char,
        end_char=parsed.end_char,
        on_exceed=parsed.on_exceed,
        summary_focus=parsed.summary_focus,
        return_image=False,
    )
    return _normalize_lrs_procedure_result(request_id, parsed.url, result, started)
```

成功 wrapper 的 `data` 只复制 JSON-safe `content/final_url/provider/processed/total_chars/returned_range/metadata/cached`；不复制 `content_items`。metadata固定 `provider_plugin_id/duration_ms/provenance/external_cost`；provenance 含 requested URL、final URL、fetch provider、cached/processed。失败 wrapper 从现有 `content` 生成 `fetch_failed` message，data空、error对象存在；不把 HTTP response body或 secret带回。

- [ ] **Step 4: 运行 provider tests**

Run: `PYTHONPATH=.:../maibot-plugin-sdk pytest tests/test_lrs_provider.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 运行现有 Fetch URL regression tests**

Run: `PYTHONPATH=.:../maibot-plugin-sdk pytest -v`

Expected: 全部 PASS，现有 Tool/API返回未改变。

- [ ] **Step 6: 提交 provider 实现**

```bash
git add plugin.py tests/test_lrs_provider.py
git commit -m "feat: expose Fetch URL as LRS procedure provider"
```

### Task 3: 更新版本、中文文档与离线冒烟验收

**Files:**
- Modify: `_manifest.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/smoke_test.py`

**Interfaces:**
- Consumes: 完整 provider implementation。
- Produces: Fetch URL 0.4.0 metadata、LRS 集成说明和 offline validation。

- [ ] **Step 1: 扩充 smoke test**

在现有 `test_api_fetch_url_component_registered()` 后加入同步 component metadata检查，并用 `asyncio.run()` 运行 descriptor，断言 contract version/procedure ID/`return_image`不暴露在 arguments schema。把该 test 加入 smoke main调用序列。

- [ ] **Step 2: 更新 manifest 与 changelog**

manifest version改 `0.4.0`；description追加“提供 LRS `describe_procedures@1` / `invoke_procedure@1` 可选集成”，但不把 LRS 写成 dependency。capabilities/dependencies不变。CHANGELOG增加 0.4.0：保留 fetch_url@1、增加 provider APIs、LRS调用图片只返回文字、provider config仍外部所有。

- [ ] **Step 3: 写 README 的 LRS 集成章节**

说明：

- 安装/启用两个插件后 LRS 自动扫描 tagged descriptor，不需要在 LRS复制 Fetch URL key/config。
- Procedure ID `fetch_url.fetch`、参数和分页方式。
- Fetch URL 是推荐不是必需；卸载后 LRS新调用显示 unavailable，LRS本身继续运行。
- `return_image=False` 的隐私/体积理由；普通 MaiBot `fetch_url` Tool仍可回传图片。
- provider result只提供 provenance metadata，不承诺由插件对内容真实性背书。

- [ ] **Step 4: 运行完整验证**

Run: `PYTHONPATH=.:../maibot-plugin-sdk pytest -v`

Expected: 全部 PASS。

Run: `PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py`

Expected: exit 0，包含 `ok: LRS procedure provider APIs registered`。

Run: `python -m ruff check plugin.py tests/test_lrs_provider.py tests/smoke_test.py`

Expected: exit 0。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 5: 提交 0.4.0 文档与版本**

```bash
git add _manifest.json README.md CHANGELOG.md tests/smoke_test.py
git commit -m "docs: document Fetch URL LRS integration"
```

## 本计划完成门槛

- 原 `fetch_url` Tool 与 `fetch_url@1` API regression tests 全部通过。
- descriptor metadata/envelope/definition与 LRS contract 1完全一致。
- invoker只接受 `fetch_url.fetch`，strict校验参数并固定 `return_image=False`。
- result符合 success/data/error/metadata，带 request provenance，不带 binary、secret或 scoped metadata。
- manifest 0.4.0，LRS不在 dependencies，README明确推荐/可选关系。
- pytest、offline smoke、窄 ruff、`git diff --check`通过，最终提交后工作树干净。
