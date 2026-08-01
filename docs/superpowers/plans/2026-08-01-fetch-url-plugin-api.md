# fetch-url Inter-plugin API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose a public `@API("fetch_url")` that mirrors Tool params, returns Tool-shaped dicts, defaults images to VLM/alt-text descriptions (`return_image=False`), and never appends to Maisaka context.

**Architecture:** Dual entry on `FetchUrlPlugin`: keep `@Tool fetch_url` unchanged in behavior; add `@API fetch_url` that calls the same `_fetch_url_impl` with an explicit `return_image: bool`. Image describe mode reuses `_prepare_vlm_image_blocking` + `AltTextCache` + `_describe_image_with_vlm`.

**Tech Stack:** Python 3.10+, `maibot-plugin-sdk` (`API`, `Tool`), existing plugin helpers (`httpx`, Pillow, VLM via `ctx.llm`).

**Spec:** `docs/superpowers/specs/2026-08-01-fetch-url-plugin-api-design.md`

## Global Constraints

- Plugin-only changes under `maibot-fetch-url-plugin/`; do not edit Host or SDK.
- User-facing strings (log / `content` notices): 简体中文.
- No silent fallbacks that hide root causes; VLM miss on API describe is an **explicit** metadata-only success (`processed="metadata_only"`), not a swallowed exception.
- Do not hand-edit a user's live `config.toml`; no `config_version` bump (no schema change).
- Bump plugin version to `0.3.0`.
- Offline verification: `PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py` from the plugin directory.
- Leave any unrelated WIP (e.g. staged `config.default.toml` restore helpers) alone unless a step explicitly requires it.

## File Structure

| File | Responsibility |
|------|----------------|
| `plugin.py` | Add `@API`; extend `_fetch_url_impl` with `return_image`; add `_build_image_describe_result`; shared error wrapper optional |
| `tests/smoke_test.py` | Registration + describe/metadata/image-bytes + get_components type filter |
| `_manifest.json` | Version `0.3.0` + description mention of inter-plugin API |
| `README.md` | Caller section for other plugins |
| `CHANGELOG.md` | `0.3.0` entry |

No new modules — follow the existing single-`plugin.py` layout.

---

### Task 1: API registration + fix Tool component lookup

**Files:**
- Modify: `tests/smoke_test.py`
- Modify: `plugin.py` (import + stub `@API` method)
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: `create_plugin()`, `get_components()` → `{name, type, metadata}`
- Produces: public API component `name="fetch_url"`, `type="API"`, `metadata.public=True`, handler `api_fetch_url`

- [ ] **Step 1: Update existing get_components test to filter by TOOL type**

In `test_get_components_planner_visibility`, both Tool and API will share `name="fetch_url"`. Change lookups to require `type == "TOOL"`:

```python
def test_get_components_planner_visibility() -> None:
    instance = fetch_plugin.create_plugin()
    components_off = instance.get_components()
    fetch_off = next(
        item for item in components_off if item["name"] == "fetch_url" and item["type"] == "TOOL"
    )
    assert not fetch_off["metadata"].get("core_tool")
    assert fetch_off["metadata"].get("visibility") != "visible"

    instance._always_visible_for_planner = True
    components_on = instance.get_components()
    fetch_on = next(
        item for item in components_on if item["name"] == "fetch_url" and item["type"] == "TOOL"
    )
    assert fetch_on["metadata"].get("core_tool") is True
    assert fetch_on["metadata"].get("visibility") == "visible"
    print("ok: get_components planner visibility toggle")
```

- [ ] **Step 2: Write failing API registration test**

Add before `main()`:

```python
def test_api_fetch_url_component_registered() -> None:
    instance = fetch_plugin.create_plugin()
    components = instance.get_components()
    api = next(
        (item for item in components if item["name"] == "fetch_url" and item["type"] == "API"),
        None,
    )
    assert api is not None, "缺少公开 API 组件 fetch_url"
    assert api["metadata"].get("public") is True
    assert api["metadata"].get("version") == "1"
    assert api["metadata"].get("handler_name") == "api_fetch_url"
    print("ok: API fetch_url registered public=True")
```

Wire it into `main()` near the other get_components test:

```python
    test_get_components_planner_visibility()
    test_api_fetch_url_component_registered()
```

- [ ] **Step 3: Run tests to verify registration fails**

Run:

```bash
cd /mnt/klein/work/maibot-plugins/maibot-fetch-url-plugin
PYTHONPATH=../maibot-plugin-sdk python -c "
import tests.smoke_test as t
t.test_get_components_planner_visibility()
t.test_api_fetch_url_component_registered()
"
```

Expected: planner visibility PASS; API registration FAIL with `缺少公开 API 组件 fetch_url` (or `StopIteration` / assert).

- [ ] **Step 4: Add stub `@API` that reuses Tool error mapping**

In `plugin.py`:

1. Change import from:

```python
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
```

to:

```python
from maibot_sdk import API, Field, MaiBotPlugin, PluginConfigBase, Tool
```

2. Extract shared invoke helper (place just above the Tool method):

```python
    async def _invoke_fetch_url(
        self,
        *,
        url: str,
        start_char: int,
        end_char: int,
        on_exceed: str,
        summary_focus: str,
        return_image: bool,
    ) -> dict[str, Any]:
        """Tool / API 共用入口：执行抓取并把异常转成 Tool 形返回。"""
        try:
            return await self._fetch_url_impl(
                url=str(url or "").strip(),
                start_char=_safe_int(start_char, 0),
                end_char=_safe_int(end_char, -1),
                on_exceed=str(on_exceed or "summarize").strip().lower(),
                summary_focus=str(summary_focus or ""),
                return_image=bool(return_image),
            )
        except FetchUrlError as exc:
            return {"success": False, "content": f"抓取失败：{exc}"}
        except httpx.HTTPStatusError as exc:
            return {
                "success": False,
                "content": f"抓取失败：目标服务器返回 HTTP {exc.response.status_code}（{url}）",
            }
        except httpx.TimeoutException:
            return {"success": False, "content": f"抓取失败：请求超时（{url}）"}
        except Exception as exc:
            self.ctx.logger.error("fetch_url 执行异常：url=%s, error=%s", url, _format_exception(exc))
            return {"success": False, "content": f"抓取失败：{_format_exception(exc)}"}
```

3. Slim the existing Tool body to:

```python
    async def fetch_url(
        self,
        url: str = "",
        start_char: int = 0,
        end_char: int = -1,
        on_exceed: str = "summarize",
        summary_focus: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        return await self._invoke_fetch_url(
            url=url,
            start_char=start_char,
            end_char=end_char,
            on_exceed=on_exceed,
            summary_focus=summary_focus,
            return_image=True,
        )
```

4. Add API method **immediately after** the Tool method (before `_fetch_url_impl`):

```python
    @API(
        "fetch_url",
        description="抓取 URL 并返回内容（供其他插件调用；图片默认返回文字描述）",
        version="1",
        public=True,
    )
    async def api_fetch_url(
        self,
        url: str = "",
        start_char: int = 0,
        end_char: int = -1,
        on_exceed: str = "summarize",
        summary_focus: str = "",
        return_image: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        return await self._invoke_fetch_url(
            url=url,
            start_char=start_char,
            end_char=end_char,
            on_exceed=on_exceed,
            summary_focus=summary_focus,
            return_image=return_image,
        )
```

5. Extend `_fetch_url_impl` signature with `return_image: bool = True` (default keeps Tool behavior if anything else calls it). **Do not change image branch yet** — always call `_build_image_result` for now so Task 1 stays green for registration while describe lands in Task 2.

```python
    async def _fetch_url_impl(
        self,
        *,
        url: str,
        start_char: int,
        end_char: int,
        on_exceed: str,
        summary_focus: str,
        return_image: bool = True,
    ) -> dict[str, Any]:
```

Pass `return_image` through the signature only; ignore it inside the body until Task 2.

- [ ] **Step 5: Re-run registration tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "
import tests.smoke_test as t
t.test_get_components_planner_visibility()
t.test_api_fetch_url_component_registered()
"
```

Expected: both PASS / print `ok: ...`.

- [ ] **Step 6: Commit**

```bash
git add plugin.py tests/smoke_test.py
git commit -m "$(cat <<'EOF'
Add public fetch_url API stub shared with Tool invoke path.

EOF
)"
```

---

### Task 2: Image describe / metadata-only result builder

**Files:**
- Modify: `plugin.py` (add `_build_image_describe_result`; branch in `_fetch_url_impl`)
- Modify: `tests/smoke_test.py`
- Test: `tests/smoke_test.py`

**Interfaces:**
- Consumes: probe dict with `data`, `final_url`, `content_type`; `self._alt_cache`; `_prepare_vlm_image_blocking`; `_describe_image_with_vlm`; `_check_vlm_available`
- Produces: `_build_image_describe_result(...) -> dict` with `processed` in `{"described", "metadata_only"}`, no `content_items`

- [ ] **Step 1: Write failing describe-path tests**

Add:

```python
def test_api_image_describe_and_metadata_only() -> None:
    """API 默认图片路径：VLM 成功 → described；VLM 失败 → metadata_only；无 content_items。"""

    async def run() -> None:
        instance = fetch_plugin.create_plugin()
        instance._refresh_config()
        instance._alt_cache = fetch_plugin.AltTextCache(
            Path("/tmp/fetch-url-api-alt-cache-test.json"),
            max_entries=8,
        )
        png = _make_image_bytes("PNG", (128, 128))
        probe = {
            "kind": "image",
            "data": png,
            "final_url": "https://example.com/photo.png",
            "content_type": "image/png",
        }

        async def fake_describe(_payload: dict) -> str:
            return "一只红色方块图片"

        async def vlm_ok() -> bool:
            return True

        async def vlm_no() -> bool:
            return False

        instance._check_vlm_available = vlm_ok  # type: ignore[method-assign]
        instance._describe_image_with_vlm = fake_describe  # type: ignore[method-assign]

        described = await instance._build_image_describe_result(
            "https://example.com/photo.png",
            probe,
            metadata={"content_type": "image/png"},
            from_cache=False,
        )
        assert described["success"] is True
        assert described["processed"] == "described"
        assert "一只红色方块图片" in described["content"]
        assert "content_items" not in described

        instance._check_vlm_available = vlm_no  # type: ignore[method-assign]

        async def describe_fail(_payload: dict) -> str:
            return ""

        instance._describe_image_with_vlm = describe_fail  # type: ignore[method-assign]
        # 清空缓存，避免命中上一次描述
        instance._alt_cache = fetch_plugin.AltTextCache(
            Path("/tmp/fetch-url-api-alt-cache-test-2.json"),
            max_entries=8,
        )
        meta_only = await instance._build_image_describe_result(
            "https://example.com/photo.png",
            probe,
            metadata={"content_type": "image/png"},
            from_cache=False,
        )
        assert meta_only["success"] is True
        assert meta_only["processed"] == "metadata_only"
        assert "content_items" not in meta_only
        assert "https://example.com/photo.png" in meta_only["content"]
        assert "描述" in meta_only["content"] or "VLM" in meta_only["content"] or "跳过" in meta_only["content"]

        bytes_result = await instance._build_image_result(
            "https://example.com/photo.png",
            probe,
            metadata={"content_type": "image/png"},
        )
        assert "content_items" in bytes_result

    asyncio.run(run())
    print("ok: API image describe / metadata_only / bytes paths")


def test_fetch_url_impl_respects_return_image_flag() -> None:
    async def run() -> None:
        instance = fetch_plugin.create_plugin()
        instance._refresh_config()
        instance._fetch_cache_enabled = False
        instance._alt_cache = fetch_plugin.AltTextCache(Path("/tmp/fetch-url-api-alt-cache-3.json"), 8)
        png = _make_image_bytes("PNG", (96, 96))
        probe = {
            "kind": "image",
            "data": png,
            "final_url": "https://example.com/a.png",
            "content_type": "image/png",
            "url": "https://example.com/a.png",
        }

        async def fake_probe(_client, _url: str) -> dict:
            return probe

        calls: list[str] = []

        async def fake_describe_result(*_a, **_k) -> dict:
            calls.append("describe")
            return {"success": True, "content": "desc", "processed": "described"}

        async def fake_image_result(*_a, **_k) -> dict:
            calls.append("bytes")
            return {"success": True, "content": "img", "content_items": [{"content_type": "image"}]}

        async def allow(_host: str) -> None:
            return None

        instance._assert_host_allowed = allow  # type: ignore[method-assign]
        instance._probe_url = fake_probe  # type: ignore[method-assign]
        instance._build_image_describe_result = fake_describe_result  # type: ignore[method-assign]
        instance._build_image_result = fake_image_result  # type: ignore[method-assign]

        class _DummyClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        instance._build_client = lambda: _DummyClient()  # type: ignore[method-assign]

        out = await instance._fetch_url_impl(
            url="https://example.com/a.png",
            start_char=0,
            end_char=-1,
            on_exceed="summarize",
            summary_focus="",
            return_image=False,
        )
        assert calls == ["describe"]
        assert out["processed"] == "described"

        calls.clear()
        out2 = await instance._fetch_url_impl(
            url="https://example.com/a.png",
            start_char=0,
            end_char=-1,
            on_exceed="summarize",
            summary_focus="",
            return_image=True,
        )
        assert calls == ["bytes"]
        assert "content_items" in out2

    asyncio.run(run())
    print("ok: _fetch_url_impl return_image branch")
```

Register both new tests in `main()`.

- [ ] **Step 2: Run tests — expect FAIL (missing `_build_image_describe_result` / no branch)**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "
import tests.smoke_test as t
t.test_api_image_describe_and_metadata_only()
t.test_fetch_url_impl_respects_return_image_flag()
"
```

Expected: FAIL (`AttributeError: _build_image_describe_result` and/or wrong branch).

- [ ] **Step 3: Implement `_build_image_describe_result`**

Place next to `_build_image_result` in `plugin.py`:

```python
    async def _build_image_describe_result(
        self,
        url: str,
        probe: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
        from_cache: bool = False,
    ) -> dict[str, Any]:
        """API 图片默认路径：经 alt_text 管道 + VLM 生成文字描述；失败则 metadata_only。"""
        page_metadata = dict(metadata or {})
        page_metadata.setdefault("content_type", probe.get("content_type", "image/*"))
        final_url = str(probe.get("final_url") or url)
        data = probe["data"]
        meta_lines = _format_metadata_notice_lines(
            page_metadata,
            requested_url=url,
            final_url=final_url,
            provider="image",
            from_cache=from_cache,
        )

        width = height = None
        fmt_hint = ""
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                width, height = image.size
                fmt_hint = _normalize_image_format(image.format or "")
        except Exception:
            width = height = None

        description = ""
        skip_reason = ""
        if self._alt_cache is None:
            skip_reason = "描述缓存未初始化"
        elif not await self._check_vlm_available():
            skip_reason = "VLM 模型不可用"
        else:
            cache_key = sha256(data).hexdigest()
            cached = self._alt_cache.get(cache_key)
            if cached:
                description = cached
            else:
                payload = await asyncio.to_thread(
                    _prepare_vlm_image_blocking,
                    data,
                    self._alt_min_dimension,
                    convert_format=self._alt_image_convert_format,
                    target_image_size=self._alt_image_target_size,
                    max_dimension=self._alt_image_max_dimension,
                    max_quality=self._alt_image_max_quality,
                    min_quality=self._alt_image_min_quality,
                    animated_policy=self._alt_image_animated_policy,
                    max_animation_frames=self._alt_image_max_animation_frames,
                )
                if payload is None:
                    skip_reason = "图片无法用于 VLM 描述（无效或尺寸过小）"
                else:
                    description = await self._describe_image_with_vlm(payload)
                    if description:
                        self._alt_cache.put(cache_key, description)
                    else:
                        skip_reason = "VLM 图片描述失败"

        if description:
            notice = "【fetch_url】\n" + "\n".join(meta_lines) + "\n已生成图片文字描述（未回传原始图片）。"
            return {
                "success": True,
                "content": notice + "\n\n" + description,
                "final_url": final_url,
                "metadata": page_metadata,
                "cached": from_cache,
                "processed": "described",
                "provider": "image",
            }

        dim_text = f"{width}x{height}" if width and height else "未知"
        fmt_text = fmt_hint or "未知"
        size_text = f"{len(data)} 字节"
        notice_lines = [
            "【fetch_url】",
            *meta_lines,
            f"格式 {fmt_text}，尺寸 {dim_text}，大小 {size_text}。",
            f"未回传原始图片；文字描述未生成（{skip_reason or '未知原因'}）。",
        ]
        return {
            "success": True,
            "content": "\n".join(notice_lines),
            "final_url": final_url,
            "metadata": page_metadata,
            "cached": from_cache,
            "processed": "metadata_only",
            "provider": "image",
        }
```

- [ ] **Step 4: Branch `_fetch_url_impl` image path on `return_image`**

Everywhere `_build_image_result` is called inside `_fetch_url_impl` (cache hit + fresh probe), use:

```python
                if return_image:
                    return await self._build_image_result(
                        url,
                        probe_or_cached,
                        metadata=...,
                        from_cache=...,
                    )
                return await self._build_image_describe_result(
                    url,
                    probe_or_cached,
                    metadata=...,
                    from_cache=...,
                )
```

Cache-hit image branch currently uses `cached["probe"]` — keep that; describe mode must still receive the probe `data` bytes from cache.

- [ ] **Step 5: Run describe tests**

```bash
PYTHONPATH=../maibot-plugin-sdk python -c "
import tests.smoke_test as t
t.test_api_image_describe_and_metadata_only()
t.test_fetch_url_impl_respects_return_image_flag()
t.test_get_components_planner_visibility()
t.test_api_fetch_url_component_registered()
"
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add plugin.py tests/smoke_test.py
git commit -m "$(cat <<'EOF'
Add API image describe path with metadata-only fallback.

EOF
)"
```

---

### Task 3: Version bump, docs, full smoke

**Files:**
- Modify: `_manifest.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `plugin.py` module docstring (mention API)

**Interfaces:**
- Consumes: Task 1–2 behavior
- Produces: released `0.3.0` docs surface for callers

- [ ] **Step 1: Bump `_manifest.json`**

Set `"version": "0.3.0"`. Update `description` to mention: 同时提供公开 `@API fetch_url` 供其他插件调用（图片默认返回文字描述，`return_image=true` 时回传图片）。

- [ ] **Step 2: Update module docstring at top of `plugin.py`**

Add a bullet that the plugin also exposes public API `fetch_url` for other plugins; image default is text describe.

- [ ] **Step 3: README — new section after「工具用法」**

```markdown
## 插件间 API（其他插件调用）

本插件暴露公开 API `fetch_url`（完整名：`com.0-hz.fetch-url.fetch_url`）。

```python
response = await self.ctx.api.call(
    "com.0-hz.fetch-url.fetch_url",
    url="https://example.com/page",
    start_char=0,
    end_char=-1,
    on_exceed="summarize",
    summary_focus="",
    return_image=False,
)
# Host 包装：业务结果在 response["result"]（当 response["success"] 为真）
result = response["result"]
```

参数与工具大体一致，额外：

| 参数 | 默认 | 说明 |
|------|------|------|
| `return_image` | `false` | 仅 API。`false`：图片走 VLM/alt_text 文字描述（失败则 metadata-only）；`true`：与工具相同，经 `content_items` 回传图片字节 |

API **不会**向 Maisaka 上下文追加内容；调用方只使用返回值。`result` 形如工具返回：`{success, content, ...}`。
```

- [ ] **Step 4: CHANGELOG entry**

```markdown
## [0.3.0] - 2026-08-01

### 新增

- 公开 `@API fetch_url`（`com.0-hz.fetch-url.fetch_url`）：参数对齐工具，供其他插件调用
- API 图片默认 `return_image=false`：经 alt_text/VLM 返回文字描述；VLM 不可用或失败时返回 metadata-only；`return_image=true` 时与工具相同回传图片
```

- [ ] **Step 5: Full smoke suite**

```bash
cd /mnt/klein/work/maibot-plugins/maibot-fetch-url-plugin
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: all `ok: ...` lines, exit code 0.

- [ ] **Step 6: Commit**

```bash
git add _manifest.json README.md CHANGELOG.md plugin.py tests/smoke_test.py
git commit -m "$(cat <<'EOF'
Release v0.3.0: public fetch_url API for inter-plugin page fetch.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Public `@API("fetch_url")` / `com.0-hz.fetch-url.fetch_url` | 1 |
| Params align with Tool + `return_image` | 1 |
| Tool-shaped return | 1–2 |
| Webpage/PDF unchanged | 1 (shared impl) |
| Image default describe, no `content_items` | 2 |
| VLM miss → metadata_only success | 2 |
| `return_image=True` → Tool image payload | 1–2 |
| No Maisaka append | design/docs (API returns only) — 3 |
| Tool unchanged for LLM | 1 (`return_image=True`) |
| Version 0.3.0 + README + CHANGELOG | 3 |
| Smoke tests | 1–3 |

## Placeholder / consistency notes

- Handler Python name is `api_fetch_url`; component `name` remains `fetch_url`.
- Tool always passes `return_image=True` into `_invoke_fetch_url`.
- `processed` values for API images: `described` | `metadata_only` only.
- Existing `test_get_components_planner_visibility` **must** filter `type == "TOOL"`.
