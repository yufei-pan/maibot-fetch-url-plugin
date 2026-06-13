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


def _make_detailed_image_bytes(fmt: str, size: tuple[int, int]) -> bytes:
    """生成可中等压缩的细节图：低分辨率噪声放大成平滑色块。

    这类内容遵循 JPEG“体积近似随质量平方变化”的模型，单次质量估算在其上表现具有代表性
    （纯随机噪声是任何单次估算器的病态输入，不适合用来校验单次压缩的目标贴合度）。
    """
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
    """生成多帧动画 GIF 字节（逐帧改变颜色）。"""
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


def test_image_compression_single_pass() -> None:
    # 单次估算编码会预缩放到 max_dimension 并按估算质量一次成型。单次压缩不保证严格贴合目标
    # （结果允许在目标附近上下浮动），但估算应对目标有单调响应：目标越紧 → 质量越低、体积越小。
    data = _make_detailed_image_bytes("png", (1600, 1200))
    common = dict(
        acceptable_formats={"jpeg", "png", "gif", "webp"},
        convert_format="jpeg",
        max_dimension=1024,
        quality_start=80,
        quality_floor=10,
    )
    loose = fetch_plugin._normalize_image_blocking(data, max_image_size=200 * 1024, **common)
    tight = fetch_plugin._normalize_image_blocking(data, max_image_size=12 * 1024, **common)

    # 行为：均触发转码并预缩放到 max_dimension，且远小于原始 PNG 体积
    assert loose["converted"] is True and tight["converted"] is True
    assert loose["downscaled"] is True and tight["downscaled"] is True
    assert len(tight["data"]) < len(data)
    # 单次估算对目标有单调响应：更紧的目标产出更低的质量与更小的体积
    assert tight["quality"] is not None and tight["quality"] < 80
    assert len(tight["data"]) < len(loose["data"])
    print(
        f"ok: single-pass responds to target — loose={len(loose['data'])}B(q{loose['quality']}), "
        f"tight={len(tight['data'])}B(q{tight['quality']})"
    )


def test_animated_keep_as_webp() -> None:
    # gif 不在可接受列表 → 触发转码；keep_animated 应保留为动态 webp
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"webp"},
        convert_format="webp",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
        animated_policy="keep_animated",
        max_animation_frames=512,
    )
    assert result["converted"] is True
    assert result["format"] == "webp"
    assert result["animation_preserved"] is True
    with Image.open(BytesIO(result["data"])) as img:
        assert img.format == "WEBP"
        assert getattr(img, "n_frames", 1) > 1
    print("ok: animated gif kept as animated webp")


def test_animated_first_frame_policy() -> None:
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"webp"},
        convert_format="webp",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
        animated_policy="first_frame",
    )
    assert result["animation_preserved"] is False
    with Image.open(BytesIO(result["data"])) as img:
        assert getattr(img, "n_frames", 1) == 1
    print("ok: first_frame policy flattens animation to a static frame")


def test_animated_skip_policy_passthrough() -> None:
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"webp"},
        convert_format="webp",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
        animated_policy="skip",
    )
    assert result["converted"] is False
    assert result["animation_preserved"] is True
    assert result["data"] == data
    print("ok: skip policy passes animated image through untouched")


def test_animated_jpeg_target_degrades_to_first_frame() -> None:
    # jpeg 无法承载动画，keep_animated 应优雅退化为首帧静态图
    data = _make_animated_gif_bytes((64, 64), frames=4)
    result = fetch_plugin._normalize_image_blocking(
        data,
        acceptable_formats={"webp"},
        convert_format="jpeg",
        max_image_size=2 * 1024 * 1024,
        max_dimension=2048,
        quality_start=80,
        quality_floor=10,
        animated_policy="keep_animated",
    )
    assert result["converted"] is True
    assert result["format"] == "jpeg"
    assert result["animation_preserved"] is False
    with Image.open(BytesIO(result["data"])) as img:
        assert img.format == "JPEG"
    print("ok: jpeg target degrades animation to first frame")


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
    test_image_compression_single_pass()
    test_animated_keep_as_webp()
    test_animated_first_frame_policy()
    test_animated_skip_policy_passthrough()
    test_animated_jpeg_target_degrades_to_first_frame()
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
