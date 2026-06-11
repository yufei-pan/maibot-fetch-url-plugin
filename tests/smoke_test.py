"""离线冒烟测试：不依赖 MaiBot Host，验证插件可导入与纯逻辑部分行为。

运行方式（在仓库根目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from io import BytesIO
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from PIL import Image  # noqa: E402

import plugin as fetch_plugin  # noqa: E402


def _make_image_bytes(fmt: str, size: tuple[int, int], noisy: bool = False) -> bytes:
    """生成测试图片字节。noisy=True 时使用随机噪声（难以压缩）。"""
    import random

    image = Image.new("RGB", size, (200, 30, 30))
    if noisy:
        random.seed(7)
        pixels = [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(size[0] * size[1])
        ]
        image.putdata(pixels)
    buffer = BytesIO()
    image.save(buffer, format=fmt.upper())
    return buffer.getvalue()


def test_get_components_planner_visibility() -> None:
    instance = fetch_plugin.create_plugin()
    components_off = instance.get_components()
    fetch_off = next(item for item in components_off if item["name"] == "fetch_url")
    assert not fetch_off["metadata"].get("core_tool")
    assert fetch_off["metadata"].get("visibility") != "visible"

    instance._always_visible_for_planner = True
    components_on = instance.get_components()
    fetch_on = next(item for item in components_on if item["name"] == "fetch_url")
    assert fetch_on["metadata"].get("core_tool") is True
    assert fetch_on["metadata"].get("visibility") == "visible"
    print("ok: get_components planner visibility toggle")


def test_plugin_importable() -> None:
    instance = fetch_plugin.create_plugin()
    assert instance is not None
    default_config = type(instance).build_default_config()
    assert default_config["plugin"]["enabled"] is True
    assert default_config["image"]["convert_format"] == "webp"
    assert default_config["alt_text"]["cache_size"] == 1024

    # config.toml 与配置模型字段一致（允许 config.toml 省略字段，不允许多出字段）
    config_data = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    for section, fields in config_data.items():
        assert section in default_config, f"config.toml 中存在未知配置节：{section}"
        for field in fields:
            assert field in default_config[section], f"config.toml 中存在未知字段：{section}.{field}"
    print("ok: plugin importable, config model consistent")


def test_image_passthrough() -> None:
    data = _make_image_bytes("png", (64, 64))
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"jpeg", "png", "gif", "webp"},
        convert_format="webp",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
    )
    assert result["converted"] is False
    assert result["data"] == data
    print("ok: acceptable small image passes through untouched")


def test_image_format_conversion() -> None:
    data = _make_image_bytes("bmp", (64, 64))
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"jpeg", "png", "gif", "webp"},
        convert_format="webp",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
    )
    assert result["converted"] is True
    assert result["format"] == "webp"
    with Image.open(BytesIO(result["data"])) as img:
        assert img.format == "WEBP"
    print("ok: unacceptable format converted to webp")


def test_image_compression_with_downscale() -> None:
    # 大尺寸噪声图 + 很小的目标，必须触发质量搜索与尺寸缩放
    data = _make_image_bytes("png", (1600, 1200), noisy=True)
    target = 24 * 1024
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"jpeg", "png", "gif", "webp"},
        convert_format="jpeg",
        max_image_size=target,
        max_dimension=1024,
        quality_start=80,
        quality_floor=10,
    )
    assert result["converted"] is True
    assert len(result["data"]) <= target, f"压缩结果 {len(result['data'])} 超过目标 {target}"
    assert result["downscaled"] is True
    print(f"ok: noisy image compressed to {len(result['data'])} bytes (target {target}), quality={result['quality']}")


def test_alt_text_regex_and_sanitize() -> None:
    markdown = '前文 ![old alt](https://example.com/a.png "标题") 后文 ![](relative/b.jpg)'
    matches = list(fetch_plugin._MD_IMAGE_RE.finditer(markdown))
    assert len(matches) == 2
    assert matches[0].group("src") == "https://example.com/a.png"
    sanitized = fetch_plugin._sanitize_alt_text("第一行[测试]\n第二行(括号)")
    assert "[" not in sanitized and "]" not in sanitized and "\n" not in sanitized
    print("ok: markdown image regex + alt sanitizer")


def test_alt_text_cache_persistence(tmp_dir: Path) -> None:
    cache_path = tmp_dir / "cache.json"
    cache = fetch_plugin.AltTextCache(cache_path, max_entries=2)
    cache.put("k1", "desc1")
    cache.put("k2", "desc2")
    cache.put("k3", "desc3")  # 淘汰 k1
    cache._flush()
    assert cache.get("k1") is None
    assert cache.get("k3") == "desc3"

    reloaded = fetch_plugin.AltTextCache(cache_path, max_entries=2)
    reloaded.load()
    assert reloaded.get("k2") == "desc2"
    assert reloaded.get("k3") == "desc3"
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(raw["entries"]) == 2
    print("ok: persistent LRU cache (eviction + reload)")


def test_private_address_detection() -> None:
    assert fetch_plugin._is_private_address("127.0.0.1") is True
    assert fetch_plugin._is_private_address("10.1.2.3") is True
    assert fetch_plugin._is_private_address("192.168.0.1") is True
    assert fetch_plugin._is_private_address("169.254.1.1") is True
    assert fetch_plugin._is_private_address("::1") is True
    assert fetch_plugin._is_private_address("8.8.8.8") is False
    print("ok: private address detection")


def test_ssrf_guard() -> None:
    instance = fetch_plugin.create_plugin()
    instance._allow_private_networks = False

    async def run() -> None:
        try:
            await instance._assert_host_allowed("127.0.0.1")
        except fetch_plugin.FetchUrlError:
            return
        raise AssertionError("内网地址未被拦截")

    asyncio.run(run())
    print("ok: SSRF guard blocks loopback")


def test_cookie_matching() -> None:
    instance = fetch_plugin.create_plugin()
    instance._global_cookies = "g=1"
    instance._domain_cookies = {"example.com": "s=2", "other.net": "t=3"}
    header = instance._cookie_header_for("https://sub.example.com/page")
    assert "g=1" in header and "s=2" in header and "t=3" not in header
    header_plain = instance._cookie_header_for("https://nomatch.org/")
    assert header_plain == "g=1"
    print("ok: global + per-domain cookie matching")


def test_html_to_markdown() -> None:
    html = (
        "<html><head><style>body{}</style><script>evil()</script></head>"
        "<body><h1>标题</h1><p>正文 <a href='/rel'>链接</a></p>"
        "<img src='/img/pic.png' alt='示意图'></body></html>"
    )
    markdown = fetch_plugin.FetchUrlPlugin._html_to_markdown_blocking(html, "https://example.com/base/")
    assert "evil" not in markdown
    assert "https://example.com/rel" in markdown
    assert "https://example.com/img/pic.png" in markdown
    assert "# 标题" in markdown
    print("ok: markdownify fallback strips noise + resolves relative URLs")


def test_windowing_semantics() -> None:
    instance = fetch_plugin.create_plugin()
    instance._max_content_length = 100
    instance._llm_summarize = True
    instance._alt_max_images = 0  # 跳过 VLM 路径

    summarize_calls: list[str] = []

    async def fake_summarize(text: str, url: str, focus: str) -> str:
        summarize_calls.append(text)
        return "这是摘要"

    instance._summarize = fake_summarize  # type: ignore[method-assign]
    document = "abcdefghij" * 50  # 500 字符

    async def run() -> None:
        # 1. 全文超限 + 默认 summarize → 摘要
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "summarized"
        assert result["total_chars"] == 500
        assert "这是摘要" in result["content"]
        assert len(summarize_calls) == 1 and len(summarize_calls[0]) == 500

        # 2. 显式 truncate → 截断原文，不调用 LLM
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="truncate", summary_focus="",
        )
        assert result["processed"] == "truncated"
        assert result["returned_range"] == [0, 100]
        assert len(summarize_calls) == 1  # 未新增调用

        # 3. 自定义窗口小于上限 → 原文完整返回
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=100, end_char=180, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "full"
        assert document[100:180] in result["content"]
        assert result["returned_range"] == [100, 180]

        # 4. 自定义窗口大于上限 + summarize → 只总结该窗口
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=100, end_char=400, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "summarized"
        assert len(summarize_calls) == 2 and len(summarize_calls[1]) == 300

        # 5. llm_summarize 关闭 → 即使要求 summarize 也截断
        instance._llm_summarize = False
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "truncated"
        assert len(summarize_calls) == 2

        # 6. 空窗口 → 友好提示
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=900, end_char=-1, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "empty"

    asyncio.run(run())
    print("ok: windowing + on_exceed semantics (summarize/truncate/full/empty)")


def main() -> None:
    import tempfile

    test_get_components_planner_visibility()
    test_plugin_importable()
    test_image_passthrough()
    test_image_format_conversion()
    test_image_compression_with_downscale()
    test_alt_text_regex_and_sanitize()
    with tempfile.TemporaryDirectory(dir=str(PLUGIN_DIR)) as tmp:
        test_alt_text_cache_persistence(Path(tmp))
    test_private_address_detection()
    test_ssrf_guard()
    test_cookie_matching()
    test_html_to_markdown()
    test_windowing_semantics()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
