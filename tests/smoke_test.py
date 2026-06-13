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
from config_migrations import CURRENT_CONFIG_VERSION, normalize_fetch_url_config  # noqa: E402

_INBOUND = dict(
    acceptable_formats={"jpeg", "png", "gif", "webp"},
    convert_format="webp",
    max_image_size=16 * 1024 * 1024,
    max_dimension=4096,
    quality_start=80,
    quality_floor=10,
)

_VLM = dict(
    convert_format="webp",
    target_image_size=1024 * 1024,
    max_dimension=2048,
    max_quality=80,
    min_quality=10,
    animated_policy="keep_animated",
    max_animation_frames=512,
)


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


def _make_detailed_image_bytes(fmt: str, size: tuple[int, int]) -> bytes:
    """生成可中等压缩的细节图。"""
    import random

    random.seed(11)
    low_width = max(1, size[0] // 16)
    low_height = max(1, size[1] // 16)
    low = Image.new("RGB", (low_width, low_height))
    low.putdata(
        [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(low_width * low_height)
        ]
    )
    image = low.resize(size, Image.Resampling.BILINEAR)
    buffer = BytesIO()
    image.save(buffer, format=fmt.upper())
    return buffer.getvalue()


def _make_animated_gif_bytes(size: tuple[int, int], frames: int = 4) -> bytes:
    """生成多帧动画 GIF 字节。"""
    images = [Image.new("RGB", size, ((index * 60) % 256, 30, 200)) for index in range(frames)]
    buffer = BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=120,
        loop=0,
    )
    return buffer.getvalue()


def test_config_migration_from_1_2_0() -> None:
    """旧版默认应在升级时迁移到新默认，用户自定义值应保留。"""
    default_config = fetch_plugin.FetchUrlConfig().model_dump(mode="python")
    legacy = {
        "plugin": {"enabled": True, "config_version": "1.2.0", "always_visible_for_planner": False},
        "fetch": {"timeout": 15.0, "proxy": "", "max_download_size": 16777216},
        "image": {
            "acceptable_formats": ["jpeg", "png", "gif", "webp"],
            "convert_format": "webp",
            "max_image_size": 2097152,
            "max_dimension": 2048,
            "quality_start": 80,
            "quality_floor": 10,
            "animated_policy": "keep_animated",
            "max_animation_frames": 512,
        },
    }
    merged, changed, notes = normalize_fetch_url_config(legacy, default_config)
    assert changed
    assert merged["plugin"]["config_version"] == CURRENT_CONFIG_VERSION
    assert merged["fetch"]["max_download_size"] == 64 * 1024 * 1024
    assert merged["image"]["max_image_size"] == 16 * 1024 * 1024
    assert merged["image"]["max_dimension"] == 4096
    assert "animated_policy" not in merged["image"]
    assert notes
    print("ok: config migration 1.2.0 -> current defaults")

    legacy_custom = {
        **legacy,
        "image": {**legacy["image"], "max_image_size": 5 * 1024 * 1024},
    }
    merged_custom, _, _ = normalize_fetch_url_config(legacy_custom, default_config)
    assert merged_custom["image"]["max_image_size"] == 5 * 1024 * 1024
    print("ok: customized max_image_size preserved during migration")


def test_config_migration_alt_text_image_renames() -> None:
    default_config = fetch_plugin.FetchUrlConfig().model_dump(mode="python")
    legacy = {
        "plugin": {"enabled": True, "config_version": "1.3.0", "always_visible_for_planner": False},
        "alt_text": {
            "max_images": 3,
            "image": {
                "convert_format": "webp",
                "max_image_size": 524288,
                "max_dimension": 1024,
                "quality_start": 80,
                "quality_floor": 10,
                "animated_policy": "first_frame",
                "max_animation_frames": 512,
            },
        },
    }
    merged, changed, notes = normalize_fetch_url_config(legacy, default_config)
    assert changed
    alt_image = merged["alt_text"]["image"]
    assert alt_image["target_image_size"] == 1024 * 1024
    assert alt_image["max_quality"] == 80
    assert alt_image["min_quality"] == 10
    assert alt_image["animated_policy"] == "keep_animated"
    assert "max_image_size" not in alt_image
    assert notes
    print("ok: alt_text.image field renames and default bumps")


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
    assert default_config["plugin"]["config_version"] == CURRENT_CONFIG_VERSION
    assert default_config["image"]["convert_format"] == "webp"
    assert default_config["alt_text"]["cache_size"] == 1024
    assert default_config["alt_text"]["image"]["convert_format"] == "webp"
    assert default_config["alt_text"]["image"]["target_image_size"] == 1024 * 1024
    assert default_config["alt_text"]["image"]["animated_policy"] == "keep_animated"
    assert "animated_policy" not in default_config["image"]

    config_data = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    for section, fields in config_data.items():
        assert section in default_config, f"config.toml 中存在未知配置节：{section}"
        for field in fields:
            assert field in default_config[section], f"config.toml 中存在未知字段：{section}.{field}"
    print("ok: plugin importable, config model consistent")


def test_inbound_passthrough_acceptable_format() -> None:
    data = _make_image_bytes("png", (64, 64))
    result = fetch_plugin._normalize_inbound_image_blocking(data, **_INBOUND)
    assert result["converted"] is False
    assert result["data"] == data
    print("ok: acceptable small image passes through untouched")


def test_inbound_preprocess_oversized_dimension() -> None:
    # 体积与格式均可接受，但最长边超过 max_dimension → 预处理编码
    data = _make_image_bytes("png", (3000, 2000))
    result = fetch_plugin._normalize_inbound_image_blocking(
        data,
        **{**_INBOUND, "max_dimension": 2048},
    )
    assert result["converted"] is True
    assert result["format"] == "webp"
    assert max(result["width"], result["height"]) <= 2048
    print("ok: oversized dimension preprocessed and downscaled")


def test_inbound_preprocess_oversized_acceptable_format() -> None:
    # 可接受格式但超过 max_image_size → 预处理编码
    data = _make_image_bytes("png", (1200, 1200), noisy=True)
    result = fetch_plugin._normalize_inbound_image_blocking(
        data,
        **{**_INBOUND, "max_image_size": 200 * 1024},
    )
    assert result["converted"] is True
    assert result["format"] == "webp"
    assert len(result["data"]) < len(data)
    print("ok: oversized acceptable format preprocessed to webp")


def test_inbound_format_conversion() -> None:
    data = _make_image_bytes("bmp", (64, 64))
    result = fetch_plugin._normalize_inbound_image_blocking(data, **_INBOUND)
    assert result["converted"] is True
    assert result["format"] == "webp"
    with Image.open(BytesIO(result["data"])) as img:
        assert img.format == "WEBP"
    print("ok: unsupported format converted to webp")


def test_vlm_prepare_always_compresses() -> None:
    data = _make_image_bytes("png", (256, 256))
    passthrough = fetch_plugin._normalize_inbound_image_blocking(data, **_INBOUND)
    assert passthrough["converted"] is False

    payload = fetch_plugin._prepare_vlm_image_blocking(data, min_dimension=64, **_VLM)
    assert payload is not None
    assert payload["format"] == "webp"
    with Image.open(BytesIO(__import__("base64").b64decode(payload["base64"]))) as img:
        assert img.format == "WEBP"
    print("ok: VLM prepare always compresses via vlm pipeline")


def test_vlm_prepare_skips_small_icons() -> None:
    data = _make_image_bytes("png", (32, 32))
    payload = fetch_plugin._prepare_vlm_image_blocking(data, min_dimension=128, **_VLM)
    assert payload is None
    print("ok: VLM prepare skips icons below min_dimension")


def test_vlm_animated_keep_as_webp() -> None:
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_vlm_image_blocking(data, **_VLM)
    assert result["converted"] is True
    assert result["format"] == "webp"
    assert result["animation_preserved"] is True
    with Image.open(BytesIO(result["data"])) as img:
        assert img.format == "WEBP"
        assert getattr(img, "n_frames", 1) > 1
    print("ok: VLM animated gif kept as animated webp")


def test_vlm_animated_first_frame_policy() -> None:
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_vlm_image_blocking(data, **{**_VLM, "animated_policy": "first_frame"})
    assert result["animation_preserved"] is False
    with Image.open(BytesIO(result["data"])) as img:
        assert getattr(img, "n_frames", 1) == 1
    print("ok: VLM first_frame policy flattens animation")


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
    cache.put("k3", "desc3")
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
    instance._alt_max_images = 0

    summarize_calls: list[str] = []

    async def fake_summarize(text: str, url: str, focus: str) -> str:
        summarize_calls.append(text)
        return "这是摘要"

    instance._summarize = fake_summarize  # type: ignore[method-assign]
    document = "abcdefghij" * 50

    async def run() -> None:
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "summarized"
        assert result["total_chars"] == 500
        assert "这是摘要" in result["content"]
        assert len(summarize_calls) == 1 and len(summarize_calls[0]) == 500

        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="truncate", summary_focus="",
        )
        assert result["processed"] == "truncated"
        assert result["returned_range"] == [0, 100]
        assert len(summarize_calls) == 1

        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=100, end_char=180, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "full"
        assert document[100:180] in result["content"]
        assert result["returned_range"] == [100, 180]

        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=100, end_char=400, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "summarized"
        assert len(summarize_calls) == 2 and len(summarize_calls[1]) == 300

        instance._llm_summarize = False
        result = await instance._build_text_result(
            markdown=document, provider="jina", url="https://e.com", final_url="https://e.com",
            start_char=0, end_char=-1, on_exceed="summarize", summary_focus="",
        )
        assert result["processed"] == "truncated"
        assert len(summarize_calls) == 2

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
    test_config_migration_from_1_2_0()
    test_config_migration_alt_text_image_renames()
    test_inbound_passthrough_acceptable_format()
    test_inbound_preprocess_oversized_dimension()
    test_inbound_preprocess_oversized_acceptable_format()
    test_inbound_format_conversion()
    test_vlm_prepare_always_compresses()
    test_vlm_prepare_skips_small_icons()
    test_vlm_animated_keep_as_webp()
    test_vlm_animated_first_frame_policy()
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
