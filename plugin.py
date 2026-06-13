"""Fetch URL 插件。

提供单一 ``fetch_url`` 工具：
- 网页 / PDF：优先通过 jina.ai Reader 转 Markdown，失败时回退到本地抓取 + markdownify；
- 图片：下载后仅对不支持格式转码；可接受格式原样回传（体积压缩交由 image-recompress 等入站插件）
- 支持 start_char / end_char 分页窗口、超长内容 LLM 总结（注入人设）或截断；
- 网页中的图片可由 VLM 生成描述并替换 alt 文本（优先级：VLM > jina 生成 alt > 原始 alt）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
from base64 import b64encode
from collections import OrderedDict
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown
from PIL import Image, ImageOps, ImageSequence

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.config import (
    extract_plugin_config_version,
    merge_plugin_config_data,
    rebuild_plugin_config_data,
    validate_plugin_config,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
JINA_READER_BASE = "https://r.jina.ai/"

# 图片格式魔数嗅探（Content-Type 缺失或不可信时使用）
_IMAGE_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)
_PDF_MAGIC = b"%PDF"

# Markdown 图片语法：![alt](src "title")
_MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]\n]*)\]\(\s*(?P<src>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\s*\)")

# 直接抓取图片（入站 content_items）的质量搜索 / 尺寸缩放迭代上限
_MAX_INBOUND_QUALITY_ITERATIONS = 6
_MAX_INBOUND_DOWNSCALE_ITERATIONS = 8

# VLM alt 体积估算用的试编码像素数上限（编码耗时大致与像素数成正比，
# 试编码成本仅为全尺寸压缩的几个百分点）
_PROBE_MAX_PIXELS = 65536

# VLM 单次估算模式的目标比例：估算目标 = target_image_size × 该比例，预留些许上下浮动空间
_SINGLE_PASS_TARGET_RATIO = 0.9

# 动图帧缺省时长（毫秒），与 Pillow 对 GIF 的常见缺省值一致
_DEFAULT_FRAME_DURATION_MS = 100

# 能承载动画的输出格式：webp→动态 WebP，png→APNG，gif→动态 GIF；jpeg 不支持
_ANIMATED_CAPABLE_FORMATS = {"webp", "png", "gif"}

# 动图处理策略：keep_animated=保留动画按输出格式编码；skip=动图原样放行；first_frame=只保留首帧
_VALID_ANIMATED_POLICIES = ("keep_animated", "skip", "first_frame")

# HEAD 排序候选图片数量上限与并发数
_ALT_TEXT_HEAD_CANDIDATE_LIMIT = 50
_ALT_TEXT_HEAD_CONCURRENCY = 8

# LLM 总结时允许送入的最大字符数（防止 prompt 超过模型上下文）
_MAX_SUMMARIZE_INPUT_CHARS = 60000

# 描述缓存延迟写盘秒数
_CACHE_FLUSH_DELAY_SECONDS = 5.0

DEFAULT_ALT_TEXT_PROMPT = (
    "请用中文详细描述这张图片的内容。请留意其主题、直观感受，输出为一段平文本，"
    "如果图中有文字，请把文字复述出来。如果一共超过了1024字，请进行概括总结。"
    "请注意不要分点，就输出一段最大1024字的文本"
)

DEFAULT_SUMMARIZE_PROMPT_TEMPLATE = """你是{nickname}。
你的人格设定：{personality}
你的表达风格：{reply_style}

你刚刚通过 fetch_url 工具抓取了 {url} 的内容，但内容太长了：当前 {total} 字符，需要压缩到 {max_length} 字符以内。
请将下面的内容总结成一份不超过 {max_length} 字符的摘要，尽量保留关键信息、数据、结论与重要链接。{focus_section}
只输出摘要正文本身，不要输出任何解释、前言或额外说明。

抓取到的内容：
{content}"""


class FetchUrlError(Exception):
    """抓取流程中可直接展示给 LLM 的可读错误。"""


# --------------------------------------------------------------------------- #
# 通用工具函数
# --------------------------------------------------------------------------- #


def _render(template: str, **values: Any) -> str:
    """使用 ``str.replace`` 渲染模板，避免正文中的花括号导致 format 异常。"""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _format_exception(exc: BaseException) -> str:
    """格式化异常为简短可读文本。"""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _normalize_image_format(fmt: str) -> str:
    """规范化图片格式名（jpg -> jpeg，统一小写）。"""
    normalized = str(fmt or "").strip().lower()
    return "jpeg" if normalized == "jpg" else normalized


def _sniff_image_mime(data: bytes) -> str:
    """根据魔数嗅探图片 MIME；WEBP 需要额外判断 RIFF 容器。"""
    for prefix, mime in _IMAGE_MAGIC_PREFIXES:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _sanitize_alt_text(text: str) -> str:
    """清洗 alt 文本，避免破坏 Markdown 图片语法。"""
    sanitized = " ".join(str(text or "").split())
    sanitized = sanitized.replace("[", "［").replace("]", "］").replace("(", "（").replace(")", "）")
    return sanitized[:1024]


def _is_private_address(address: str) -> bool:
    """判断 IP 字符串是否属于内网 / 环回 / 链路本地 / 保留地址。"""
    try:
        ip_obj = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    )


# --------------------------------------------------------------------------- #
# 图片处理（阻塞函数，统一通过 asyncio.to_thread 调用）
# --------------------------------------------------------------------------- #


def _flatten_for_jpeg(image: Image.Image) -> Image.Image:
    """JPEG 不支持透明通道，将带 alpha 的图片平铺到白色背景。"""
    if image.mode in {"RGBA", "LA", "PA"} or (image.mode == "P" and "transparency" in image.info):
        rgba_image = image.convert("RGBA")
        background = Image.new("RGB", rgba_image.size, (255, 255, 255))
        background.paste(rgba_image, mask=rgba_image.split()[-1])
        return background
    return image.convert("RGB")


def _coerce_rgb_like(image: Image.Image) -> Image.Image:
    """将冷门色彩模式（CMYK / P / I;16 等）规范到 webp/png 可编码的模式。"""
    if image.mode in {"RGB", "RGBA", "L", "LA"}:
        return image
    if image.mode == "P":
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    return image.convert("RGBA" if "A" in image.getbands() else "RGB")


def _encode_inbound_image(
    image: Image.Image,
    fmt: str,
    quality: int | None,
    *,
    keep_animation: bool = False,
    animation_source: Image.Image | None = None,
) -> bytes:
    """将图片编码为目标格式字节（直接抓取入站路径）。"""
    buffer = BytesIO()
    if fmt == "jpeg":
        _flatten_for_jpeg(image).save(buffer, format="JPEG", quality=quality or 80, optimize=True)
    elif fmt == "webp":
        if keep_animation and animation_source is not None:
            animation_source.save(buffer, format="WEBP", save_all=True, quality=quality or 80)
        else:
            _coerce_rgb_like(image).save(buffer, format="WEBP", quality=quality or 80, method=4)
    elif fmt == "png":
        _coerce_rgb_like(image).save(buffer, format="PNG", optimize=True)
    elif fmt == "gif":
        if keep_animation and animation_source is not None:
            animation_source.save(buffer, format="GIF", save_all=True)
        else:
            image.convert("RGB").save(buffer, format="GIF")
    else:
        raise FetchUrlError(f"不支持的转换目标格式：{fmt}")
    return buffer.getvalue()


def _adaptive_quality_search_inbound(
    image: Image.Image,
    fmt: str,
    target_size: int,
    quality_start: int,
    quality_floor: int,
    *,
    keep_animation: bool = False,
    animation_source: Image.Image | None = None,
) -> tuple[bytes, int, bool]:
    """入站路径自适应质量搜索：按 ``q × sqrt(target/actual)`` 启发式跳跃。

    Returns:
        (编码结果, 最终质量, 是否满足大小目标)
    """
    quality = max(quality_floor, min(100, quality_start))
    encoded = b""
    for _ in range(_MAX_INBOUND_QUALITY_ITERATIONS):
        encoded = _encode_inbound_image(
            image, fmt, quality, keep_animation=keep_animation, animation_source=animation_source
        )
        if len(encoded) <= target_size:
            return encoded, quality, True
        if quality <= quality_floor:
            break
        estimated = int(quality * math.sqrt(target_size / len(encoded)))
        quality = max(quality_floor, min(quality - 5, estimated))
    return encoded, quality, False


def _normalize_inbound_image_blocking(
    data: bytes,
    *,
    acceptable_formats: set[str],
    convert_format: str,
    max_image_size: int,
    max_dimension: int,
    quality_start: int,
    quality_floor: int,
) -> dict[str, Any]:
    """直接抓取图片 URL 时的转码 / 预处理（入站 content_items 路径）。

    同时满足「格式在 ``acceptable_formats`` 内」「体积 ≤ ``max_image_size``」
    「最长边 ≤ ``max_dimension``」时原样回传；否则按 ``convert_format``、
    ``quality_start`` / ``quality_floor`` 预处理编码，以 ``max_image_size`` 为体积目标，
    并遵守 ``max_dimension`` 预缩放。
    """
    with Image.open(BytesIO(data)) as probe:
        probe.verify()

    image = Image.open(BytesIO(data))
    image.load()
    src_format = _normalize_image_format(image.format or "")
    animated = getattr(image, "n_frames", 1) > 1
    within_dimension = max_dimension <= 0 or max(image.size) <= max_dimension

    if src_format in acceptable_formats and len(data) <= max_image_size and within_dimension:
        return {
            "data": data,
            "format": src_format,
            "width": image.width,
            "height": image.height,
            "converted": False,
            "quality": None,
            "downscaled": False,
            "animation_preserved": animated,
            "original_format": src_format,
            "original_size": len(data),
        }

    target_fmt = convert_format
    keep_animation = animated and target_fmt in {"webp", "gif"}
    work = image
    if animated:
        image.seek(0)
        work = image.copy()
    downscaled = False

    if max(work.size) > max_dimension:
        work = _resize_keep_aspect(work, max_dimension / max(work.size))
        keep_animation = False
        downscaled = True

    final_quality: int | None = None
    if target_fmt in {"webp", "jpeg"}:
        encoded, final_quality, fits = _adaptive_quality_search_inbound(
            work,
            target_fmt,
            max_image_size,
            quality_start,
            quality_floor,
            keep_animation=keep_animation,
            animation_source=image if keep_animation else None,
        )
        if keep_animation and not fits:
            keep_animation = False
            encoded, final_quality, fits = _adaptive_quality_search_inbound(
                work, target_fmt, max_image_size, quality_start, quality_floor
            )
    else:
        encoded = _encode_inbound_image(
            work,
            target_fmt,
            None,
            keep_animation=keep_animation,
            animation_source=image if keep_animation else None,
        )
        fits = len(encoded) <= max_image_size

    for _ in range(_MAX_INBOUND_DOWNSCALE_ITERATIONS):
        if fits:
            break
        factor = math.sqrt(max_image_size / len(encoded))
        factor = max(0.3, min(0.95, factor))
        if min(work.size) <= 16:
            break
        keep_animation = False
        work = _resize_keep_aspect(work, factor)
        downscaled = True
        if target_fmt in {"webp", "jpeg"}:
            encoded = _encode_inbound_image(work, target_fmt, quality_floor)
            final_quality = quality_floor
        else:
            encoded = _encode_inbound_image(work, target_fmt, None)
        fits = len(encoded) <= max_image_size

    with Image.open(BytesIO(encoded)) as output_probe:
        output_width, output_height = output_probe.size

    return {
        "data": encoded,
        "format": target_fmt,
        "width": output_width,
        "height": output_height,
        "converted": True,
        "quality": final_quality,
        "downscaled": downscaled,
        "animation_preserved": keep_animation,
        "original_format": src_format or "unknown",
        "original_size": len(data),
    }


# --------------------------------------------------------------------------- #
# VLM alt 图片压缩（体积估算单次编码，与入站路径独立）
# --------------------------------------------------------------------------- #


def _prepare_static_frame(image: Image.Image, fmt: str) -> Image.Image:
    """把静态图（或动图首帧）整理为适合目标格式编码的模式。

    - 先应用 EXIF 方向（转码后 EXIF 会丢失，必须先转正）；
    - jpeg 不支持透明，把 alpha 拍平到白色背景；
    - webp / png / gif 走 :func:`_coerce_rgb_like`，保留可保留的透明通道。
    """
    normalized = ImageOps.exif_transpose(image)
    if fmt == "jpeg":
        return _flatten_for_jpeg(normalized)
    return _coerce_rgb_like(normalized)


def _quality_adjustable(fmt: str) -> bool:
    """输出是否能通过降质量缩小体积：png / gif 不行，只能缩像素尺寸。"""
    return fmt in {"webp", "jpeg"}


def _encode_static_image(image: Image.Image, fmt: str, quality: int | None) -> bytes:
    """将单帧静态图编码为目标格式字节（VLM 路径；png / gif 忽略 quality）。"""
    buffer = BytesIO()
    if fmt == "jpeg":
        _flatten_for_jpeg(image).save(buffer, format="JPEG", quality=quality or 80, optimize=True)
    elif fmt == "webp":
        _coerce_rgb_like(image).save(buffer, format="WEBP", quality=quality or 80, method=4)
    elif fmt == "png":
        _coerce_rgb_like(image).save(buffer, format="PNG", optimize=True)
    elif fmt == "gif":
        image.convert("RGB").save(buffer, format="GIF")
    else:
        raise FetchUrlError(f"不支持的转换目标格式：{fmt}")
    return buffer.getvalue()


def _encode_animated(image: Image.Image, fmt: str, quality: int) -> bytes:
    """把多帧图片整体转为动画，保留每帧时长与循环次数。

    webp 输出为动态 WebP，png 输出为 APNG，gif 输出为动态 GIF；均保留逐帧时长与 loop。
    一次性整图编码（不做多轮质量搜索），符合单次压缩的资源约束。
    """
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(image):
        frames.append(frame.convert("RGBA"))
        durations.append(int(frame.info.get("duration", _DEFAULT_FRAME_DURATION_MS)) or _DEFAULT_FRAME_DURATION_MS)

    buffer = BytesIO()
    loop = int(image.info.get("loop", 0))
    if fmt == "webp":
        frames[0].save(
            buffer,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            quality=quality,
            method=4,
        )
    elif fmt == "png":
        # APNG：PNG 无损，固定最高压缩率
        frames[0].save(
            buffer,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            optimize=True,
            compress_level=9,
        )
    else:
        # 动态 GIF：Pillow 会把 RGBA 帧量化到调色板
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            disposal=2,
        )
    return buffer.getvalue()


def _resize_keep_aspect(image: Image.Image, factor: float) -> Image.Image:
    """按比例缩小图片，保持纵横比，最小边长 16 像素。"""
    new_width = max(16, round(image.width * factor))
    new_height = max(16, round(image.height * factor))
    return _coerce_rgb_like(image).resize((new_width, new_height), Image.Resampling.LANCZOS)


def _estimate_quality_and_scale(
    image: Image.Image,
    fmt: str,
    target_size: int,
    quality_start: int,
    quality_floor: int,
) -> tuple[int, float]:
    """单次估算满足目标体积所需的质量，必要时附加一个等比缩放比例。

    做法：把图片等比缩到不超过 ``_PROBE_MAX_PIXELS`` 像素做一次试编码，按像素数
    外推全尺寸体积，再用 ``q × sqrt(目标/实际)`` 启发式反解质量。质量下限仍不够时，
    按“体积近似随质量平方变化”的同一模型补一个缩放比例，保证仍然只做单次全尺寸编码。

    png / gif 没有质量可调，超出目标时直接按“体积随像素数线性变化”反解缩放比例。

    Returns:
        (估算质量, 缩放比例)；比例为 1.0 表示无需缩放。
    """
    pixels = image.width * image.height
    probe = image
    if pixels > _PROBE_MAX_PIXELS:
        probe = _resize_keep_aspect(image, math.sqrt(_PROBE_MAX_PIXELS / pixels))
    probe_encoded = _encode_static_image(probe, fmt, quality_start)
    estimated_full = len(probe_encoded) * pixels / (probe.width * probe.height)

    if estimated_full <= target_size:
        return quality_start, 1.0

    if not _quality_adjustable(fmt):
        # png / gif：只能缩小像素尺寸
        scale = math.sqrt(target_size / estimated_full)
        return quality_start, max(0.1, min(1.0, scale))

    estimated_quality = int(quality_start * math.sqrt(target_size / estimated_full))
    if estimated_quality >= quality_floor:
        return estimated_quality, 1.0

    # 质量压到下限仍不够：按 q² 体积模型估算下限时的体积，反解需要的缩放比例
    estimated_at_floor = estimated_full * (quality_floor / quality_start) ** 2
    scale = math.sqrt(target_size / estimated_at_floor)
    return quality_floor, max(0.1, min(1.0, scale))


def _encode_vlm_static_pipeline(
    image: Image.Image,
    fmt: str,
    max_dimension: int,
    max_quality: int,
    min_quality: int,
    target_image_size: int,
) -> tuple[bytes, int | None, bool]:
    """VLM 静态图单次编码：预缩放 → 始终按 ``target_image_size`` 体积估算编码。"""
    prepared = _prepare_static_frame(image, fmt)
    downscaled = False
    if max_dimension > 0 and max(prepared.size) > max_dimension:
        prepared = _resize_keep_aspect(prepared, max_dimension / max(prepared.size))
        downscaled = True

    adjustable = _quality_adjustable(fmt)
    target_size = int(target_image_size * _SINGLE_PASS_TARGET_RATIO)
    quality, scale = _estimate_quality_and_scale(prepared, fmt, target_size, max_quality, min_quality)
    if scale < 1.0:
        prepared = _resize_keep_aspect(prepared, scale)
        downscaled = True
    return (
        _encode_static_image(prepared, fmt, quality if adjustable else None),
        quality if adjustable else None,
        downscaled,
    )


def _normalize_vlm_image_blocking(
    data: bytes,
    *,
    convert_format: str,
    target_image_size: int,
    max_dimension: int,
    max_quality: int,
    min_quality: int,
    animated_policy: str,
    max_animation_frames: int,
) -> dict[str, Any]:
    """VLM alt 图片描述前的转码 / 压缩（始终走体积估算，配置见 ``[alt_text.image]``）。"""
    with Image.open(BytesIO(data)) as probe:
        probe.verify()

    image = Image.open(BytesIO(data))
    image.load()
    src_format = _normalize_image_format(image.format or "")
    is_animated = bool(getattr(image, "is_animated", False)) and getattr(image, "n_frames", 1) > 1
    target_fmt = convert_format

    keep_animation = is_animated and animated_policy == "keep_animated" and target_fmt in _ANIMATED_CAPABLE_FORMATS
    if keep_animation and max_animation_frames > 0:
        frame_count = getattr(image, "n_frames", 1)
        if frame_count > max_animation_frames:
            keep_animation = False

    if keep_animation:
        encoded = _encode_animated(image, target_fmt, max_quality)
        if len(encoded) <= target_image_size:
            with Image.open(BytesIO(encoded)) as output_probe:
                output_width, output_height = output_probe.size
            return {
                "data": encoded,
                "format": target_fmt,
                "width": output_width,
                "height": output_height,
                "converted": True,
                "quality": max_quality if _quality_adjustable(target_fmt) else None,
                "downscaled": False,
                "animation_preserved": True,
                "original_format": src_format or "unknown",
                "original_size": len(data),
            }

    if is_animated:
        image.seek(0)

    encoded, final_quality, downscaled = _encode_vlm_static_pipeline(
        image, target_fmt, max_dimension, max_quality, min_quality, target_image_size
    )

    with Image.open(BytesIO(encoded)) as output_probe:
        output_width, output_height = output_probe.size

    return {
        "data": encoded,
        "format": target_fmt,
        "width": output_width,
        "height": output_height,
        "converted": True,
        "quality": final_quality,
        "downscaled": downscaled,
        "animation_preserved": False,
        "original_format": src_format or "unknown",
        "original_size": len(data),
    }


def _prepare_vlm_image_blocking(
    data: bytes,
    min_dimension: int,
    *,
    convert_format: str,
    target_image_size: int,
    max_dimension: int,
    max_quality: int,
    min_quality: int,
    animated_policy: str,
    max_animation_frames: int,
) -> dict[str, Any] | None:
    """为 VLM 输入准备图片：经体积估算单次压缩管道转码后回传 base64。

    由 ``[alt_text.image]`` 独立配置；始终经 :func:`_normalize_vlm_image_blocking` 体积估算压缩。

    Returns:
        dict | None: ``{"base64", "format", "width", "height"}``；
        图片无效或最短边小于 ``min_dimension``（疑似图标）时返回 ``None``。
    """
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
    except Exception:
        return None

    if min(width, height) < min_dimension:
        return None

    try:
        info = _normalize_vlm_image_blocking(
            data,
            convert_format=convert_format,
            target_image_size=target_image_size,
            max_dimension=max_dimension,
            max_quality=max_quality,
            min_quality=min_quality,
            animated_policy=animated_policy,
            max_animation_frames=max_animation_frames,
        )
    except (FetchUrlError, Exception):
        return None

    return {
        "base64": b64encode(info["data"]).decode("ascii"),
        "format": info["format"],
        "width": info["width"],
        "height": info["height"],
    }


# --------------------------------------------------------------------------- #
# 持久化 LRU 描述缓存
# --------------------------------------------------------------------------- #


class AltTextCache:
    """以图片字节 sha256 为键的持久化 LRU 描述缓存。

    内存中维护 LRU 顺序，落盘为插件 ``data/`` 目录下的 JSON 文件；
    写入采用延迟合并（debounce）+ 卸载时强制刷盘。
    """

    def __init__(self, path: Path, max_entries: int) -> None:
        self._path = path
        self._max_entries = max(0, max_entries)
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._flush_task: asyncio.Task[None] | None = None
        self._dirty = False

    def load(self) -> None:
        """从磁盘加载缓存；文件缺失或损坏时静默从空开始。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw.get("entries") if isinstance(raw, dict) else None
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, list) and len(item) == 2:
                        self._entries[str(item[0])] = str(item[1])
        except Exception:
            self._entries.clear()
        self._trim()

    def get(self, key: str) -> str | None:
        """读取缓存并更新 LRU 顺序。"""
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key: str, value: str) -> None:
        """写入缓存（自动淘汰最旧条目）并调度延迟刷盘。"""
        if self._max_entries <= 0:
            return
        self._entries[key] = value
        self._entries.move_to_end(key)
        self._trim()
        self._dirty = True
        self._schedule_flush()

    def set_max_entries(self, max_entries: int) -> None:
        """热更新缓存容量。"""
        self._max_entries = max(0, max_entries)
        self._trim()

    async def aclose(self) -> None:
        """取消挂起的延迟刷盘并立即落盘。"""
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        self._flush()

    def _trim(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._delayed_flush())
        except RuntimeError:
            self._flush()

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(_CACHE_FLUSH_DELAY_SECONDS)
        self._flush()

    def _flush(self) -> None:
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"entries": list(self._entries.items())}, ensure_ascii=False)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self._path)
            self._dirty = False
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 配置版本迁移
# --------------------------------------------------------------------------- #

# 与 PluginSectionConfig.config_version 默认值保持同步
CURRENT_CONFIG_VERSION = "1.6.0"

DEFAULT_FETCH_TIMEOUT = 15.0
DEFAULT_FETCH_MAX_DOWNLOAD_SIZE = 64 * 1024 * 1024
DEFAULT_JINA_TIMEOUT = 30.0
DEFAULT_JINA_ENGINE = "browser"
DEFAULT_IMAGE_ACCEPTABLE_FORMATS = ("jpeg", "png", "gif", "webp")
DEFAULT_IMAGE_CONVERT_FORMAT = "webp"
DEFAULT_IMAGE_MAX_IMAGE_SIZE = 16 * 1024 * 1024
DEFAULT_IMAGE_MAX_DIMENSION = 4096
DEFAULT_IMAGE_QUALITY_START = 80
DEFAULT_IMAGE_QUALITY_FLOOR = 10
DEFAULT_CONTENT_MAX_LENGTH = 8192
DEFAULT_LLM_MODEL = "planner"
DEFAULT_LLM_TEMPERATURE = 0.3
DEFAULT_LLM_MAX_TOKENS = 0
DEFAULT_ALT_MAX_IMAGES = 3
DEFAULT_ALT_MIN_DIMENSION = 128
DEFAULT_ALT_MODEL = "vlm"
DEFAULT_ALT_CACHE_SIZE = 1024

_LEGACY_DEFAULTS: dict[str, dict[str, Any]] = {
    "fetch.max_download_size": {"<1.5.0": 16 * 1024 * 1024},
    "image.max_image_size": {"<1.5.0": 2 * 1024 * 1024},
    "image.max_dimension": {"<1.5.0": 2048},
    "alt_text.image.max_image_size": {"1.3.0": 512 * 1024},
    "alt_text.image.max_dimension": {"1.3.0": 1024, "1.4.0": 1024},
    "alt_text.image.animated_policy": {"1.3.0": "first_frame", "1.4.0": "first_frame"},
}

_CURRENT_DEFAULTS: dict[str, Any] = {
    "plugin.always_visible_for_planner": False,
    "fetch.timeout": DEFAULT_FETCH_TIMEOUT,
    "fetch.user_agent": DEFAULT_USER_AGENT,
    "fetch.max_download_size": DEFAULT_FETCH_MAX_DOWNLOAD_SIZE,
    "fetch.allow_private_networks": False,
    "jina.enabled": True,
    "jina.timeout": DEFAULT_JINA_TIMEOUT,
    "jina.engine": DEFAULT_JINA_ENGINE,
    "jina.with_generated_alt": True,
    "image.acceptable_formats": list(DEFAULT_IMAGE_ACCEPTABLE_FORMATS),
    "image.convert_format": DEFAULT_IMAGE_CONVERT_FORMAT,
    "image.max_image_size": DEFAULT_IMAGE_MAX_IMAGE_SIZE,
    "image.max_dimension": DEFAULT_IMAGE_MAX_DIMENSION,
    "image.quality_start": DEFAULT_IMAGE_QUALITY_START,
    "image.quality_floor": DEFAULT_IMAGE_QUALITY_FLOOR,
    "content.max_content_length": DEFAULT_CONTENT_MAX_LENGTH,
    "content.llm_summarize": True,
    "llm.model": DEFAULT_LLM_MODEL,
    "llm.temperature": DEFAULT_LLM_TEMPERATURE,
    "llm.max_tokens": DEFAULT_LLM_MAX_TOKENS,
    "llm.summarize_prompt_template": DEFAULT_SUMMARIZE_PROMPT_TEMPLATE,
    "alt_text.max_images": DEFAULT_ALT_MAX_IMAGES,
    "alt_text.min_dimension": DEFAULT_ALT_MIN_DIMENSION,
    "alt_text.model": DEFAULT_ALT_MODEL,
    "alt_text.prompt": DEFAULT_ALT_TEXT_PROMPT,
    "alt_text.cache_size": DEFAULT_ALT_CACHE_SIZE,
    "alt_text.image.target_image_size": 1024 * 1024,
    "alt_text.image.max_dimension": 2048,
    "alt_text.image.max_quality": 80,
    "alt_text.image.min_quality": 10,
    "alt_text.image.animated_policy": "keep_animated",
    "alt_text.image.max_animation_frames": 512,
    "alt_text.image.convert_format": "webp",
}

# 1.6.0 迁移时保留语义性空值，不还原为占位
_STRIP_SKIP_PATHS = frozenset({
    "fetch.proxy",
    "fetch.cookies",
    "jina.api_key",
})

# 与旧版 config.toml 写死的默认模板比对后还原为 ""
_STRIP_TO_EMPTY_STRING_PATHS = frozenset({
    "llm.summarize_prompt_template",
    "alt_text.prompt",
})


def _placeholder_for_baked_default(dotted: str, baked: Any) -> Any:
    if dotted in _STRIP_TO_EMPTY_STRING_PATHS:
        return ""
    return None


def _strip_baked_defaults_to_placeholders(config: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    """将仍等于 1.5.0 写死默认值的字段还原为占位空值，便于升级后跟随代码内置默认。"""
    notes: list[str] = []
    changed = False
    for dotted, baked in _CURRENT_DEFAULTS.items():
        if dotted in _STRIP_SKIP_PATHS:
            continue
        current = _get_nested_config(config, dotted)
        if current is _CONFIG_MISSING or current != baked:
            continue
        placeholder = _placeholder_for_baked_default(dotted, baked)
        _set_nested_config(config, dotted, placeholder)
        notes.append(f"{dotted}: 已还原为占位默认")
        changed = True
    return config, changed, notes


def _migrate_legacy_baked_defaults(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """config_version 已为最新但字段仍写死旧默认时，再次还原为占位。"""
    migrated, changed, _ = _strip_baked_defaults_to_placeholders(config)
    if changed:
        plugin_section = migrated.get("plugin")
        if isinstance(plugin_section, dict):
            plugin_section["config_version"] = CURRENT_CONFIG_VERSION
    return migrated, changed


class _ConfigMissing:
    pass


_CONFIG_MISSING = _ConfigMissing()


def _config_version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "0").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _config_version_less_than(left: str, right: str) -> bool:
    return _config_version_tuple(left) < _config_version_tuple(right)


def _get_nested_config(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return _CONFIG_MISSING
        node = node[key]
    return node


def _set_nested_config(config: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = config
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def _reset_if_legacy_default(
    config: dict[str, Any],
    dotted: str,
    *,
    before_version: str,
    new_value: Any,
    notes: list[str],
) -> None:
    current = _get_nested_config(config, dotted)
    if current is _CONFIG_MISSING:
        return
    legacy_map = _LEGACY_DEFAULTS.get(dotted, {})
    old_default = legacy_map.get(before_version) or legacy_map.get(f"<{before_version}")
    if old_default is None or current != old_default:
        return
    _set_nested_config(config, dotted, new_value)
    notes.append(f"{dotted}: {old_default!r} -> {new_value!r}")


def _rename_alt_image_field_if_present(
    config: dict[str, Any],
    section: str,
    old_key: str,
    new_key: str,
    notes: list[str],
) -> None:
    section_data = config.get(section)
    if not isinstance(section_data, dict):
        return
    nested = section_data.get("image")
    if not isinstance(nested, dict) or old_key not in nested or new_key in nested:
        return
    nested[new_key] = nested.pop(old_key)
    notes.append(f"{section}.image.{old_key} 重命名为 {new_key}")


def _migrate_plugin_config_data(config: dict[str, Any], from_version: str) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    if not _config_version_less_than(from_version, CURRENT_CONFIG_VERSION):
        return config, notes

    if _config_version_less_than(from_version, "1.4.0"):
        _rename_alt_image_field_if_present(config, "alt_text", "max_image_size", "target_image_size", notes)
        _rename_alt_image_field_if_present(config, "alt_text", "quality_start", "max_quality", notes)
        _rename_alt_image_field_if_present(config, "alt_text", "quality_floor", "min_quality", notes)

    if _config_version_less_than(from_version, "1.5.0"):
        _reset_if_legacy_default(
            config,
            "fetch.max_download_size",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["fetch.max_download_size"],
            notes=notes,
        )
        _reset_if_legacy_default(
            config,
            "image.max_image_size",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["image.max_image_size"],
            notes=notes,
        )
        _reset_if_legacy_default(
            config,
            "image.max_dimension",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["image.max_dimension"],
            notes=notes,
        )
        image_section = config.get("image")
        if isinstance(image_section, dict):
            for obsolete in ("animated_policy", "max_animation_frames"):
                if obsolete in image_section:
                    image_section.pop(obsolete, None)
                    notes.append(f"image.{obsolete}: 已移除（动图策略仅保留在 alt_text.image）")

        alt_section = config.get("alt_text")
        if isinstance(alt_section, dict):
            nested = alt_section.get("image")
            if not isinstance(nested, dict):
                nested = {}
                alt_section["image"] = nested
            if "max_image_size" in nested and "target_image_size" not in nested:
                nested["target_image_size"] = nested.pop("max_image_size")
                notes.append("alt_text.image.max_image_size 重命名为 target_image_size")
            if "quality_start" in nested and "max_quality" not in nested:
                nested["max_quality"] = nested.pop("quality_start")
                notes.append("alt_text.image.quality_start 重命名为 max_quality")
            if "quality_floor" in nested and "min_quality" not in nested:
                nested["min_quality"] = nested.pop("quality_floor")
                notes.append("alt_text.image.quality_floor 重命名为 min_quality")
            if nested.get("target_image_size") == 512 * 1024:
                nested["target_image_size"] = _CURRENT_DEFAULTS["alt_text.image.target_image_size"]
                notes.append("alt_text.image.target_image_size: 512KB -> 1MB（仍为旧默认）")
            if nested.get("animated_policy") == "first_frame":
                nested["animated_policy"] = _CURRENT_DEFAULTS["alt_text.image.animated_policy"]
                notes.append("alt_text.image.animated_policy: first_frame -> keep_animated")
            if nested.get("max_dimension") == 1024:
                nested["max_dimension"] = _CURRENT_DEFAULTS["alt_text.image.max_dimension"]
                notes.append("alt_text.image.max_dimension: 1024 -> 2048（仍为旧默认）")

    if _config_version_less_than(from_version, "1.6.0"):
        _, strip_changed, strip_notes = _strip_baked_defaults_to_placeholders(config)
        notes.extend(strip_notes)

    plugin_section = config.get("plugin")
    if isinstance(plugin_section, dict):
        plugin_section["config_version"] = CURRENT_CONFIG_VERSION

    return config, notes


def _normalize_fetch_url_config(
    raw_config: Mapping[str, Any] | None,
    default_config: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, list[str]]:
    raw: dict[str, Any] = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    if not raw:
        merged = rebuild_plugin_config_data(default_config, {})
        return merged, True, ["空配置，已填充全部默认值"]

    from_version = extract_plugin_config_version(raw)
    latest_version = extract_plugin_config_version(default_config)

    if _config_version_less_than(from_version, latest_version):
        working = rebuild_plugin_config_data(default_config, raw)
        working, notes = _migrate_plugin_config_data(working, from_version)
        return working, True, notes

    working, changed = merge_plugin_config_data(default_config, raw)
    return working, changed, []


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")
    always_visible_for_planner: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "false"},
        description=(
            "是否让 fetch_url 工具始终对 Planner 可见（等同 core_tool）。"
            "开启后无需 tool_search 即可直接调用；修改后需重新加载插件才能生效。"
        ),
    )


class FetchSectionConfig(PluginConfigBase):
    """HTTP 抓取相关配置。"""

    __ui_label__ = "抓取"
    __ui_icon__ = "globe"
    __ui_order__ = 1

    timeout: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": "15.0"},
        description="直接抓取 URL 的 HTTP 超时时间（秒）。",
    )
    proxy: str = Field(
        default="",
        description="抓取内容时使用的代理地址（例如 http://127.0.0.1:7890）；留空表示不使用代理。",
    )
    user_agent: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_USER_AGENT},
        description="抓取时使用的 User-Agent。",
    )
    max_download_size: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_FETCH_MAX_DOWNLOAD_SIZE)},
        description="单次下载的最大字节数（默认 64 MB），超过即中止并报错。",
    )
    allow_private_networks: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "false"},
        description="是否允许抓取内网 / 环回 / 保留地址。默认关闭（SSRF 防护），仅在确有需要时开启。",
    )
    cookies: str = Field(
        default="",
        description="对所有域名发送的全局 Cookie 字符串（例如 key1=value1; key2=value2）。请勿提交到版本库。",
    )
    domain_cookies: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "按域名配置的 Cookie，键为域名（匹配自身及子域名），值为 Cookie 字符串。"
            "直接抓取时通过 Cookie 头发送，jina 抓取时通过 X-Set-Cookie 转发。请勿提交到版本库。"
        ),
    )


class JinaSectionConfig(PluginConfigBase):
    """jina.ai Reader 相关配置。"""

    __ui_label__ = "Jina Reader"
    __ui_icon__ = "book-open"
    __ui_order__ = 2

    enabled: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="是否优先使用 jina.ai Reader 抓取非图片内容（支持网页 / PDF，质量更好）。",
    )
    api_key: str = Field(
        default="",
        description="jina.ai API Key（可选）。不填也能用但有更严格的频率限制。请勿提交到版本库。",
    )
    timeout: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": "30.0"},
        description="jina.ai 请求超时时间（秒）。超时视为提供方故障，自动回退到本地 markdownify 抓取。",
    )
    engine: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_JINA_ENGINE},
        description="jina.ai 渲染引擎（X-Engine 头），browser 渲染质量最好；留空表示由 jina 自动选择。",
    )
    with_generated_alt: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description=(
            "是否在配置了 jina API Key 时让 jina 为缺失 alt 的图片生成描述"
            "（X-With-Generated-Alt 头）。未配置 API Key 时不会发送该头。"
        ),
    )


class ImageSectionConfig(PluginConfigBase):
    """直接抓取图片 URL 时的入站转码 / 预处理配置。"""

    __ui_label__ = "图片"
    __ui_icon__ = "image"
    __ui_order__ = 3

    acceptable_formats: list[str] | None = Field(
        default=None,
        json_schema_extra={"placeholder": json.dumps(list(DEFAULT_IMAGE_ACCEPTABLE_FORMATS))},
        description="可直接原样回传的图片格式；不在列表内或超过 max_image_size 时触发预处理编码。",
    )
    convert_format: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_IMAGE_CONVERT_FORMAT},
        description="预处理编码的目标格式，可选 webp / jpeg / png / gif。",
    )
    max_image_size: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_IMAGE_MAX_IMAGE_SIZE)},
        description=(
            "原样回传的体积上限（默认 16 MB）。超过则无论格式一律按 convert_format 预处理编码，"
            "并以该值为体积压缩目标；不支持格式转码时同理。"
        ),
    )
    max_dimension: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_IMAGE_MAX_DIMENSION)},
        description="原样回传的最长边上限（像素，默认 4096）；超过则触发预处理编码，编码时也会预缩到该尺寸。",
    )
    quality_start: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_IMAGE_QUALITY_START)},
        description="预处理编码时 webp / jpeg 的初始质量。",
    )
    quality_floor: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_IMAGE_QUALITY_FLOOR)},
        description="预处理编码时 webp / jpeg 的最低质量；到达下限仍超标时改为缩小图片尺寸。",
    )


class ContentSectionConfig(PluginConfigBase):
    """文本内容长度相关配置。"""

    __ui_label__ = "内容"
    __ui_icon__ = "file-text"
    __ui_order__ = 4

    max_content_length: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_CONTENT_MAX_LENGTH)},
        description="单次返回文本内容的最大字符数；超过时按 llm_summarize 设置进行总结或截断。",
    )
    llm_summarize: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="超长内容是否默认调用 LLM 总结；关闭后超长内容一律截断。",
    )


class LLMSectionConfig(PluginConfigBase):
    """LLM 总结相关配置。"""

    __ui_label__ = "LLM 总结"
    __ui_icon__ = "sparkles"
    __ui_order__ = 5

    model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_LLM_MODEL},
        description="总结时使用的 LLM 模型任务名（planner / replyer 等 Host 任务名，不是原始模型 ID）。",
    )
    temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LLM_TEMPERATURE)},
        description="总结时的采样温度。",
    )
    max_tokens: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_LLM_MAX_TOKENS)},
        description=(
            "总结 LLM 调用的最大 token 数；0 表示自动按 max_content_length 的四倍计算。"
            "若小于 max_content_length 会在日志中告警，输出可能被截断。"
        ),
    )
    summarize_prompt_template: str = Field(
        default="",
        description=(
            "总结提示词模板。占位符：{nickname}、{personality}、{reply_style}、{url}、"
            "{total}、{max_length}、{focus_section}、{content}。"
        ),
    )


class AltTextImageSectionConfig(PluginConfigBase):
    """VLM alt 图片描述前的图片转码 / 压缩配置（与 ``[image]`` 独立，共用同一压缩管道）。"""

    __ui_label__ = "VLM 图片压缩"
    __ui_icon__ = "image"
    __ui_order__ = 0

    convert_format: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_IMAGE_CONVERT_FORMAT},
        description="送入 VLM 前的转换目标格式，可选 webp / jpeg / png / gif。",
    )
    target_image_size: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(1024 * 1024)},
        description="体积估算算法的目标字节数（默认 1 MB）；输出允许在目标附近上下浮动。",
    )
    max_dimension: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": "2048"},
        description="压缩时的最长边上限（像素）；超大图会先缩到该尺寸再做体积估算编码。",
    )
    max_quality: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": "80"},
        description="webp / jpeg 压缩的最高质量（体积估算起点）。",
    )
    min_quality: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": "10"},
        description="webp / jpeg 压缩的最低质量；到达下限仍超标时改为缩小图片尺寸。",
    )
    animated_policy: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": "keep_animated"},
        description=(
            "动图处理策略，语义与 [image] 相同；"
            "VLM 路径始终 force_compress，skip 策略也不会原样放行。"
        ),
    )
    max_animation_frames: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": "512"},
        description="保留动画时的帧数上限，超过则退化为首帧静态图；0 表示不限制。",
    )


class AltTextSectionConfig(PluginConfigBase):
    """VLM 图片描述（alt 文本替换）相关配置。"""

    __ui_label__ = "图片描述"
    __ui_icon__ = "eye"
    __ui_order__ = 6

    max_images: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_ALT_MAX_IMAGES)},
        description=(
            "每次抓取最多用 VLM 描述的图片数量；页面图片超过该值时优先描述最大的几张"
            "（按 HEAD Content-Length 排序）。0 表示关闭 VLM 描述（保留 jina / 原始 alt）。"
        ),
    )
    min_dimension: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_ALT_MIN_DIMENSION)},
        description="参与 VLM 描述的图片最短边下限（像素），用于跳过图标 / Logo。",
    )
    model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_ALT_MODEL},
        description="生成图片描述使用的模型任务名（默认 Host 的 vlm 任务）。",
    )
    prompt: str = Field(
        default="",
        description="生成图片描述的提示词。",
    )
    cache_size: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_ALT_CACHE_SIZE)},
        description="持久化描述缓存的条目上限（LRU，按图片内容哈希命中），0 表示不缓存。",
    )
    image: AltTextImageSectionConfig = Field(default_factory=AltTextImageSectionConfig)


class FetchUrlConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fetch: FetchSectionConfig = Field(default_factory=FetchSectionConfig)
    jina: JinaSectionConfig = Field(default_factory=JinaSectionConfig)
    image: ImageSectionConfig = Field(default_factory=ImageSectionConfig)
    content: ContentSectionConfig = Field(default_factory=ContentSectionConfig)
    llm: LLMSectionConfig = Field(default_factory=LLMSectionConfig)
    alt_text: AltTextSectionConfig = Field(default_factory=AltTextSectionConfig)


# --------------------------------------------------------------------------- #
# 配置解析（空值 = 使用代码内置默认，便于版本升级后自动跟随新默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveFetchUrlConfig:
    """运行时生效的插件配置（已解析占位空值）。"""

    always_visible_for_planner: bool
    timeout: float
    user_agent: str
    max_download_size: int
    allow_private_networks: bool
    jina_enabled: bool
    jina_timeout: float
    jina_engine: str
    jina_generated_alt: bool
    acceptable_formats: frozenset[str]
    convert_format: str
    max_image_size: int
    max_dimension: int
    quality_start: int
    quality_floor: int
    max_content_length: int
    llm_summarize: bool
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    summarize_template: str
    alt_max_images: int
    alt_min_dimension: int
    alt_model: str
    alt_prompt: str
    alt_cache_size: int
    alt_image_convert_format: str
    alt_image_target_size: int
    alt_image_max_dimension: int
    alt_image_max_quality: int
    alt_image_min_quality: int
    alt_image_animated_policy: str
    alt_image_max_animation_frames: int


def _effective_bool(value: bool | None, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _effective_str(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def _effective_float(value: float | None, default: float, *, minimum: float = 0.0) -> float:
    if value is None:
        return default
    return max(minimum, float(value))


def _effective_int(value: int | None, default: int, *, minimum: int = 0) -> int:
    if value is None:
        return default
    return max(minimum, int(value))


def _effective_jina_engine(value: str | None) -> str:
    if value is None:
        return DEFAULT_JINA_ENGINE
    return str(value).strip()


def resolve_effective_fetch_url_config(cfg: FetchUrlConfig) -> EffectiveFetchUrlConfig:
    image = cfg.image
    formats = {_normalize_image_format(fmt) for fmt in (image.acceptable_formats or DEFAULT_IMAGE_ACCEPTABLE_FORMATS)}
    acceptable = frozenset(fmt for fmt in formats if fmt) or frozenset(DEFAULT_IMAGE_ACCEPTABLE_FORMATS)
    convert_format = _normalize_image_format(image.convert_format or DEFAULT_IMAGE_CONVERT_FORMAT)
    if convert_format not in {"webp", "jpeg", "png", "gif"}:
        convert_format = DEFAULT_IMAGE_CONVERT_FORMAT

    alt_image = cfg.alt_text.image
    alt_convert = _normalize_image_format(alt_image.convert_format or DEFAULT_IMAGE_CONVERT_FORMAT)
    if alt_convert not in {"webp", "jpeg", "png", "gif"}:
        alt_convert = DEFAULT_IMAGE_CONVERT_FORMAT
    alt_animated_policy = (alt_image.animated_policy or "keep_animated").strip().lower()
    if alt_animated_policy not in _VALID_ANIMATED_POLICIES:
        alt_animated_policy = "keep_animated"

    quality_start = min(100, max(1, _effective_int(image.quality_start, DEFAULT_IMAGE_QUALITY_START, minimum=1)))
    quality_floor = min(quality_start, max(1, _effective_int(image.quality_floor, DEFAULT_IMAGE_QUALITY_FLOOR, minimum=1)))
    alt_max_quality = min(100, max(1, _effective_int(alt_image.max_quality, 80, minimum=1)))
    alt_min_quality = min(
        alt_max_quality, max(1, _effective_int(alt_image.min_quality, 10, minimum=1))
    )

    user_agent = (cfg.fetch.user_agent or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT

    return EffectiveFetchUrlConfig(
        always_visible_for_planner=_effective_bool(cfg.plugin.always_visible_for_planner, False),
        timeout=_effective_float(cfg.fetch.timeout, DEFAULT_FETCH_TIMEOUT, minimum=1.0),
        user_agent=user_agent,
        max_download_size=_effective_int(cfg.fetch.max_download_size, DEFAULT_FETCH_MAX_DOWNLOAD_SIZE, minimum=1024),
        allow_private_networks=_effective_bool(cfg.fetch.allow_private_networks, False),
        jina_enabled=_effective_bool(cfg.jina.enabled, True),
        jina_timeout=_effective_float(cfg.jina.timeout, DEFAULT_JINA_TIMEOUT, minimum=1.0),
        jina_engine=_effective_jina_engine(cfg.jina.engine),
        jina_generated_alt=_effective_bool(cfg.jina.with_generated_alt, True),
        acceptable_formats=acceptable,
        convert_format=convert_format,
        max_image_size=_effective_int(cfg.image.max_image_size, DEFAULT_IMAGE_MAX_IMAGE_SIZE, minimum=8 * 1024),
        max_dimension=_effective_int(cfg.image.max_dimension, DEFAULT_IMAGE_MAX_DIMENSION, minimum=64),
        quality_start=quality_start,
        quality_floor=quality_floor,
        max_content_length=_effective_int(cfg.content.max_content_length, DEFAULT_CONTENT_MAX_LENGTH, minimum=256),
        llm_summarize=_effective_bool(cfg.content.llm_summarize, True),
        llm_model=_effective_str(cfg.llm.model, DEFAULT_LLM_MODEL),
        llm_temperature=float(cfg.llm.temperature if cfg.llm.temperature is not None else DEFAULT_LLM_TEMPERATURE),
        llm_max_tokens=max(0, _effective_int(cfg.llm.max_tokens, DEFAULT_LLM_MAX_TOKENS)),
        summarize_template=cfg.llm.summarize_prompt_template or DEFAULT_SUMMARIZE_PROMPT_TEMPLATE,
        alt_max_images=max(0, _effective_int(cfg.alt_text.max_images, DEFAULT_ALT_MAX_IMAGES)),
        alt_min_dimension=max(1, _effective_int(cfg.alt_text.min_dimension, DEFAULT_ALT_MIN_DIMENSION, minimum=1)),
        alt_model=_effective_str(cfg.alt_text.model, DEFAULT_ALT_MODEL),
        alt_prompt=cfg.alt_text.prompt or DEFAULT_ALT_TEXT_PROMPT,
        alt_cache_size=max(0, _effective_int(cfg.alt_text.cache_size, DEFAULT_ALT_CACHE_SIZE)),
        alt_image_convert_format=alt_convert,
        alt_image_target_size=max(8 * 1024, _effective_int(alt_image.target_image_size, 1024 * 1024, minimum=8 * 1024)),
        alt_image_max_dimension=max(64, _effective_int(alt_image.max_dimension, 2048, minimum=64)),
        alt_image_max_quality=alt_max_quality,
        alt_image_min_quality=alt_min_quality,
        alt_image_animated_policy=alt_animated_policy,
        alt_image_max_animation_frames=max(
            0, _effective_int(alt_image.max_animation_frames, 512)
        ),
    )


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


class FetchUrlPlugin(MaiBotPlugin):
    """fetch_url 插件主体。"""

    config_model = FetchUrlConfig

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._alt_cache: AltTextCache | None = None
        # VLM 可用性：None=未知（尝试调用），True/False=已确认
        self._vlm_available: bool | None = None
        # 配置派生缓存，on_load / on_config_update 时刷新
        self._timeout = 15.0
        self._proxy = ""
        self._user_agent = DEFAULT_USER_AGENT
        self._max_download_size = 64 * 1024 * 1024
        self._allow_private_networks = False
        self._global_cookies = ""
        self._domain_cookies: dict[str, str] = {}
        self._jina_enabled = True
        self._jina_api_key = ""
        self._jina_timeout = 30.0
        self._jina_engine = "browser"
        self._jina_generated_alt = True
        self._acceptable_formats: set[str] = {"jpeg", "png", "gif", "webp"}
        self._convert_format = "webp"
        self._max_image_size = 16 * 1024 * 1024
        self._max_dimension = 4096
        self._quality_start = 80
        self._quality_floor = 10
        self._max_content_length = 8192
        self._llm_summarize = True
        self._llm_model = "planner"
        self._llm_temperature = 0.3
        self._llm_max_tokens = 0
        self._summarize_template = DEFAULT_SUMMARIZE_PROMPT_TEMPLATE
        self._alt_max_images = 3
        self._alt_min_dimension = 128
        self._alt_model = "vlm"
        self._alt_prompt = DEFAULT_ALT_TEXT_PROMPT
        self._alt_cache_size = 1024
        self._alt_image_convert_format = "webp"
        self._alt_image_target_size = 1024 * 1024
        self._alt_image_max_dimension = 2048
        self._alt_image_max_quality = 80
        self._alt_image_min_quality = 10
        self._alt_image_animated_policy = "keep_animated"
        self._alt_image_max_animation_frames = 512
        self._always_visible_for_planner = False
        self._config_initialized = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：刷新配置并初始化描述缓存。"""
        self._refresh_config()
        cache_path = self._plugin_dir / "data" / "alt_text_cache.json"
        self._alt_cache = AltTextCache(cache_path, self._alt_cache_size)
        self._alt_cache.load()
        self.ctx.logger.info(
            "fetch_url 插件已加载：jina=%s, 总结=%s, VLM描述上限=%d 张, 描述缓存=%d 条, Planner常显=%s",
            "开" if self._jina_enabled else "关",
            "开" if self._llm_summarize else "关",
            self._alt_max_images,
            self._alt_cache_size,
            "开" if self._always_visible_for_planner else "关",
        )

    async def on_unload(self) -> None:
        """插件卸载：刷盘描述缓存。"""
        if self._alt_cache is not None:
            await self._alt_cache.aclose()
            self._alt_cache = None
        self.ctx.logger.info("fetch_url 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：刷新派生缓存。"""
        if scope == "self":
            self._refresh_config()
            self._vlm_available = None
            if self._alt_cache is not None:
                self._alt_cache.set_max_entries(self._alt_cache_size)
            self.ctx.logger.info("fetch_url 插件配置已更新: version=%s", version)

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        """补齐默认字段，并在 ``config_version`` 升级时迁移仍为旧默认值的配置项。"""
        default_config = type(self).build_default_config()
        merged, changed, notes = _normalize_fetch_url_config(config_data, default_config)
        validated = validate_plugin_config(FetchUrlConfig, merged)
        normalized = validated.model_dump(mode="python")
        if notes and hasattr(self, "ctx"):
            try:
                self.ctx.logger.info("fetch_url 配置迁移 (%d 项): %s", len(notes), "; ".join(notes))
            except RuntimeError:
                pass
        raw = dict(config_data) if isinstance(config_data, Mapping) else {}
        migrated, migrated_changed = _migrate_legacy_baked_defaults(normalized)
        return migrated, changed or normalized != raw or migrated_changed

    def get_components(self) -> list[dict[str, Any]]:
        """收集组件声明，并按配置决定 fetch_url 是否对 Planner 常显。

        Runner 在 ``on_load`` 之前调用本方法，因此直接读取 ``self.config``，
        不依赖 ``_refresh_config`` 已执行的派生缓存。
        """
        components = super().get_components()
        try:
            always_visible = resolve_effective_fetch_url_config(self.config).always_visible_for_planner
        except RuntimeError:
            always_visible = self._always_visible_for_planner
        if not always_visible:
            return components
        for component in components:
            if component.get("name") != "fetch_url":
                continue
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata["core_tool"] = True
                metadata["visibility"] = "visible"
        return components

    def _refresh_config(self) -> None:
        """从强类型配置刷新派生缓存。"""
        effective = resolve_effective_fetch_url_config(self.config)
        next_always_visible = effective.always_visible_for_planner
        if self._config_initialized and next_always_visible != self._always_visible_for_planner:
            self.ctx.logger.warning(
                "always_visible_for_planner 已变更，请禁用后重新启用插件以使 Planner 工具可见性生效"
            )
        self._always_visible_for_planner = next_always_visible
        self._config_initialized = True

        fetch = self.config.fetch
        self._timeout = effective.timeout
        self._proxy = (fetch.proxy or "").strip()
        self._user_agent = effective.user_agent
        self._max_download_size = effective.max_download_size
        self._allow_private_networks = effective.allow_private_networks
        self._global_cookies = (fetch.cookies or "").strip()
        self._domain_cookies = {
            str(domain).strip().lstrip(".").lower(): str(cookie).strip()
            for domain, cookie in (fetch.domain_cookies or {}).items()
            if str(domain).strip() and str(cookie).strip()
        }

        self._jina_enabled = effective.jina_enabled
        self._jina_api_key = (self.config.jina.api_key or "").strip()
        self._jina_timeout = effective.jina_timeout
        self._jina_engine = effective.jina_engine
        self._jina_generated_alt = effective.jina_generated_alt

        self._acceptable_formats = set(effective.acceptable_formats)
        self._convert_format = effective.convert_format
        self._max_image_size = effective.max_image_size
        self._max_dimension = effective.max_dimension
        self._quality_start = effective.quality_start
        self._quality_floor = effective.quality_floor

        self._max_content_length = effective.max_content_length
        self._llm_summarize = effective.llm_summarize

        self._llm_model = effective.llm_model
        self._llm_temperature = effective.llm_temperature
        self._llm_max_tokens = effective.llm_max_tokens
        self._summarize_template = effective.summarize_template

        self._alt_max_images = effective.alt_max_images
        self._alt_min_dimension = effective.alt_min_dimension
        self._alt_model = effective.alt_model
        self._alt_prompt = effective.alt_prompt
        self._alt_cache_size = effective.alt_cache_size

        self._alt_image_convert_format = effective.alt_image_convert_format
        self._alt_image_target_size = effective.alt_image_target_size
        self._alt_image_max_dimension = effective.alt_image_max_dimension
        self._alt_image_max_quality = effective.alt_image_max_quality
        self._alt_image_min_quality = effective.alt_image_min_quality
        alt_animated_policy = effective.alt_image_animated_policy
        if alt_animated_policy not in _VALID_ANIMATED_POLICIES:
            self.ctx.logger.warning("无效的 VLM 动图策略 %r，回退为 keep_animated", alt_animated_policy)
            alt_animated_policy = "keep_animated"
        self._alt_image_animated_policy = alt_animated_policy
        self._alt_image_max_animation_frames = effective.alt_image_max_animation_frames

        self._warn_if_summarize_max_tokens_too_low(self._resolve_summarize_max_tokens())

    def _resolve_summarize_max_tokens(self) -> int:
        """解析总结调用的 max_tokens；``llm.max_tokens=0`` 时按 ``max_content_length × 4`` 自动计算。"""
        if self._llm_max_tokens > 0:
            return self._llm_max_tokens
        return self._max_content_length * 4

    def _warn_if_summarize_max_tokens_too_low(self, max_tokens: int) -> None:
        """当 max_tokens 不足以容纳目标摘要长度时记录告警。"""
        if max_tokens < self._max_content_length:
            self.ctx.logger.warning(
                "总结 max_tokens(%d) 小于 max_content_length(%d)，摘要输出可能因 token 上限被截断",
                max_tokens,
                self._max_content_length,
            )

    # ------------------------------------------------------------------ #
    # HTTP 基础设施
    # ------------------------------------------------------------------ #
    def _build_client(self, timeout: float | None = None) -> httpx.AsyncClient:
        """构建带 SSRF 防护钩子的 HTTP 客户端。"""
        effective_timeout = timeout if timeout is not None else self._timeout
        return httpx.AsyncClient(
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json,image/*,*/*",
            },
            timeout=httpx.Timeout(effective_timeout, connect=min(effective_timeout, 10.0)),
            follow_redirects=True,
            proxy=self._proxy or None,
            event_hooks={"request": [self._ssrf_request_hook]},
        )

    async def _ssrf_request_hook(self, request: httpx.Request) -> None:
        """请求钩子：每一跳（含重定向目标）都执行内网地址检查。"""
        await self._assert_host_allowed(str(request.url.host or ""))

    async def _assert_host_allowed(self, host: str) -> None:
        """校验目标主机不属于内网 / 保留地址（除非配置放行）。"""
        if self._allow_private_networks:
            return
        if not host:
            raise FetchUrlError("无法解析目标主机名")
        stripped_host = host.strip("[]")
        try:
            ipaddress.ip_address(stripped_host)
            addresses = [stripped_host]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(stripped_host, None)
            except OSError as exc:
                raise FetchUrlError(f"域名解析失败：{stripped_host}（{_format_exception(exc)}）") from exc
            addresses = [str(info[4][0]) for info in infos]
        for address in addresses:
            if _is_private_address(address):
                raise FetchUrlError(
                    f"目标地址 {stripped_host} 解析到内网/保留地址，已被安全策略拦截"
                    "（可在配置 fetch.allow_private_networks 中放行）"
                )

    def _cookie_header_for(self, url: str) -> str:
        """根据全局与按域名 Cookie 配置，拼出适用于该 URL 的 Cookie 字符串。"""
        host = (urlparse(url).hostname or "").lower()
        parts: list[str] = []
        if self._global_cookies:
            parts.append(self._global_cookies)
        for domain, cookie in self._domain_cookies.items():
            if host == domain or host.endswith("." + domain):
                parts.append(cookie)
        return "; ".join(part.strip().strip(";") for part in parts if part.strip())

    # ------------------------------------------------------------------ #
    # 抓取与内容类型探测
    # ------------------------------------------------------------------ #
    async def _probe_url(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        """流式 GET 目标 URL 并按响应头 + 魔数判定内容类型。

        Returns:
            dict: ``kind`` 为 ``image`` 时带完整 ``data``；
            ``text``（jina 关闭时）带完整 ``data`` 与 ``encoding``；
            ``defer``（jina 开启的非图片内容）不带数据，由调用方走 jina。
        """
        headers: dict[str, str] = {}
        cookie_header = self._cookie_header_for(url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            final_url = str(response.url)
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()

            iterator = response.aiter_bytes()
            first_chunk = b""
            async for chunk in iterator:
                first_chunk = chunk
                break

            sniffed_mime = _sniff_image_mime(first_chunk)
            is_image = content_type.startswith("image/") or (
                bool(sniffed_mime) and (not content_type or content_type == "application/octet-stream")
            )
            is_pdf = content_type == "application/pdf" or first_chunk.startswith(_PDF_MAGIC)

            if is_image:
                buffer = bytearray(first_chunk)
                async for chunk in iterator:
                    buffer.extend(chunk)
                    if len(buffer) > self._max_download_size:
                        raise FetchUrlError(
                            f"图片超过下载大小上限（{self._max_download_size} 字节），已中止下载"
                        )
                return {
                    "kind": "image",
                    "data": bytes(buffer),
                    "content_type": content_type or sniffed_mime,
                    "final_url": final_url,
                }

            if is_pdf:
                return {"kind": "pdf", "content_type": content_type, "final_url": final_url}

            if self._jina_enabled:
                return {"kind": "defer", "content_type": content_type, "final_url": final_url}

            buffer = bytearray(first_chunk)
            async for chunk in iterator:
                buffer.extend(chunk)
                if len(buffer) > self._max_download_size:
                    raise FetchUrlError(
                        f"页面超过下载大小上限（{self._max_download_size} 字节），已中止下载"
                    )
            return {
                "kind": "text",
                "data": bytes(buffer),
                "content_type": content_type,
                "final_url": final_url,
                "encoding": response.charset_encoding or "utf-8",
            }

    async def _fetch_via_jina(self, url: str) -> str:
        """通过 jina.ai Reader 抓取并返回 Markdown 文本。"""
        headers: dict[str, str] = {
            "Accept": "text/plain",
            "X-Return-Format": "markdown",
        }
        if self._jina_api_key:
            headers["Authorization"] = f"Bearer {self._jina_api_key}"
        if self._jina_engine:
            headers["X-Engine"] = self._jina_engine
        if self._jina_generated_alt and self._jina_api_key:
            headers["X-With-Generated-Alt"] = "true"
        cookie_header = self._cookie_header_for(url)
        if cookie_header:
            headers["X-Set-Cookie"] = cookie_header

        async with self._build_client(timeout=self._jina_timeout) as client:
            response = await client.get(f"{JINA_READER_BASE}{url}", headers=headers)
            response.raise_for_status()
            if len(response.content) > self._max_download_size:
                raise FetchUrlError("jina 返回内容超过下载大小上限")
            return response.text

    async def _fetch_direct_markdown(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        """直接抓取页面并用 markdownify 转为 Markdown。

        Returns:
            tuple: ``(markdown, final_url)``。
        """
        probe = await self._probe_url_force_text(client, url)
        content_type: str = probe["content_type"]
        final_url: str = probe["final_url"]
        text = probe["data"].decode(probe["encoding"], errors="replace")

        if "html" in content_type or content_type in {"", "application/octet-stream"}:
            markdown = await asyncio.to_thread(self._html_to_markdown_blocking, text, final_url)
            return markdown, final_url
        if content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
            return text, final_url
        raise FetchUrlError(f"不支持直接解析的内容类型：{content_type or '未知'}（可尝试启用 jina）")

    async def _probe_url_force_text(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        """强制按文本下载完整正文（jina 回退路径使用）。"""
        headers: dict[str, str] = {}
        cookie_header = self._cookie_header_for(url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > self._max_download_size:
                    raise FetchUrlError(
                        f"页面超过下载大小上限（{self._max_download_size} 字节），已中止下载"
                    )
            return {
                "data": bytes(buffer),
                "content_type": content_type,
                "final_url": str(response.url),
                "encoding": response.charset_encoding or "utf-8",
            }

    @staticmethod
    def _html_to_markdown_blocking(html: str, base_url: str) -> str:
        """剔除噪声标签、补全相对链接后将 HTML 转为 Markdown（阻塞实现）。"""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        for anchor in soup.find_all("a", href=True):
            anchor["href"] = urljoin(base_url, anchor["href"])
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(base_url, src)
        return html_to_markdown(str(soup), heading_style="ATX").strip()

    async def _download_image_bytes(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        """下载单张图片字节（带大小上限），失败返回 None。"""
        headers: dict[str, str] = {}
        cookie_header = self._cookie_header_for(url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > self._max_download_size:
                        return None
                return bytes(buffer)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # VLM 图片描述（alt 文本替换）
    # ------------------------------------------------------------------ #
    async def _check_vlm_available(self) -> bool:
        """检查 VLM 模型任务是否可用（结果按会话缓存）。"""
        if self._vlm_available is not None:
            return self._vlm_available
        try:
            models = await self.ctx.llm.get_available_models()
        except Exception:
            models = []
        if models:
            self._vlm_available = self._alt_model in models
        else:
            # 查询失败时不下结论，先尝试调用
            self._vlm_available = True
        return self._vlm_available

    async def _describe_image_with_vlm(self, image_payload: dict[str, Any]) -> str:
        """调用 VLM 为单张图片生成描述；失败返回空字符串。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self._alt_prompt},
                    {
                        "type": "image",
                        "image_format": image_payload["format"],
                        "image_base64": image_payload["base64"],
                    },
                ],
            }
        ]
        try:
            result = await self.ctx.llm.generate(prompt=messages, model=self._alt_model)
        except Exception as exc:
            self.ctx.logger.warning("VLM 图片描述调用异常：%s", _format_exception(exc))
            return ""
        if not result.get("success"):
            self.ctx.logger.warning("VLM 图片描述失败：%s", result.get("error") or "未知错误")
            return ""
        return (result.get("response") or "").strip()

    async def _rank_image_candidates(self, client: httpx.AsyncClient, srcs: list[str]) -> list[str]:
        """通过并发 HEAD 请求按 Content-Length 从大到小排序候选图片。"""
        candidates = srcs[:_ALT_TEXT_HEAD_CANDIDATE_LIMIT]
        semaphore = asyncio.Semaphore(_ALT_TEXT_HEAD_CONCURRENCY)

        async def head_size(src: str) -> tuple[str, int | None]:
            async with semaphore:
                try:
                    response = await client.head(src)
                    length = response.headers.get("content-length")
                    return src, int(length) if length and length.isdigit() else None
                except Exception:
                    return src, None

        results = await asyncio.gather(*(head_size(src) for src in candidates))
        ranked = sorted(results, key=lambda item: (item[1] is None, -(item[1] or 0)))
        return [src for src, _ in ranked]

    async def _apply_vlm_alt_text(self, markdown: str, base_url: str) -> tuple[str, int]:
        """对窗口内 Markdown 图片应用 VLM 描述替换。

        优先级：VLM 描述 > jina 生成 alt > 原始 alt。仅对按大小排名前
        ``max_images`` 且最短边不小于 ``min_dimension`` 的图片调用 VLM。

        Returns:
            tuple: ``(替换后的 Markdown, 成功描述的图片数)``。
        """
        if self._alt_max_images <= 0 or self._alt_cache is None:
            return markdown, 0

        matches = list(_MD_IMAGE_RE.finditer(markdown))
        if not matches:
            return markdown, 0
        if not await self._check_vlm_available():
            return markdown, 0

        seen: set[str] = set()
        ordered_srcs: list[str] = []
        for match in matches:
            src = urljoin(base_url, match.group("src"))
            if src.startswith(("http://", "https://")) and src not in seen:
                seen.add(src)
                ordered_srcs.append(src)
        if not ordered_srcs:
            return markdown, 0

        descriptions: dict[str, str] = {}
        async with self._build_client() as client:
            ranked_srcs = await self._rank_image_candidates(client, ordered_srcs)
            for src in ranked_srcs:
                if len(descriptions) >= self._alt_max_images:
                    break
                data = await self._download_image_bytes(client, src)
                if data is None:
                    continue
                cache_key = sha256(data).hexdigest()
                cached = self._alt_cache.get(cache_key)
                if cached:
                    descriptions[src] = cached
                    continue
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
                    continue
                description = await self._describe_image_with_vlm(payload)
                if not description:
                    # 单次调用失败大概率是模型故障，本次抓取不再继续尝试
                    break
                self._alt_cache.put(cache_key, description)
                descriptions[src] = description

        if not descriptions:
            return markdown, 0

        def replace(match: re.Match[str]) -> str:
            src = urljoin(base_url, match.group("src"))
            description = descriptions.get(src)
            if not description:
                return match.group(0)
            title = match.group("title") or ""
            return f"![{_sanitize_alt_text(description)}]({match.group('src')}{title})"

        return _MD_IMAGE_RE.sub(replace, markdown), len(descriptions)

    # ------------------------------------------------------------------ #
    # LLM 总结
    # ------------------------------------------------------------------ #
    async def _summarize(self, text: str, url: str, focus: str) -> str:
        """调用 LLM 将超长内容总结到 max_content_length 以内；失败返回空字符串。"""
        nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        personality = await self.ctx.config.get("personality.personality", "") or ""
        reply_style = await self.ctx.config.get("personality.reply_style", "") or ""

        input_text = text
        if len(input_text) > _MAX_SUMMARIZE_INPUT_CHARS:
            input_text = input_text[:_MAX_SUMMARIZE_INPUT_CHARS]
        focus_section = f"\n本次总结请特别关注：{focus}" if focus.strip() else ""
        prompt = _render(
            self._summarize_template,
            nickname=nickname,
            personality=personality,
            reply_style=reply_style,
            url=url,
            total=len(text),
            max_length=self._max_content_length,
            focus_section=focus_section,
            content=input_text,
        )
        max_tokens = self._resolve_summarize_max_tokens()
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=self._llm_model,
                temperature=self._llm_temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self.ctx.logger.warning("LLM 总结调用异常：%s", _format_exception(exc))
            return ""
        if not result.get("success"):
            self.ctx.logger.warning("LLM 总结失败：%s", result.get("error") or "未知错误")
            return ""
        return (result.get("response") or "").strip()

    # ------------------------------------------------------------------ #
    # fetch_url 工具
    # ------------------------------------------------------------------ #
    @Tool(
        "fetch_url",
        brief_description="抓取任意 URL：网页/PDF 转为 Markdown 文本返回，图片直接以图像形式返回，支持分页读取与超长内容自动总结。",
        detailed_description=(
            "抓取一个 URL 并返回其内容。会自动根据响应类型分流：图片直接以图像形式回传供你观察；"
            "网页 / PDF 等转为 Markdown 文本。\n"
            "参数说明：\n"
            "- url：string，必填。要抓取的完整 URL（http/https）。\n"
            "- start_char：integer，可选，默认 0。返回文本窗口的起始字符位置。\n"
            "- end_char：integer，可选，默认 -1（文档末尾）。返回文本窗口的结束字符位置。\n"
            "- on_exceed：string，可选，summarize 或 truncate，默认 summarize。窗口内容超过长度上限时，"
            "summarize 表示调用 LLM 总结，truncate 表示直接截断返回原文。\n"
            "- summary_focus：string，可选。总结时需要特别关注的方面。\n"
            "结果中会标注文档总长与本次返回的窗口范围；当内容被总结或截断时，"
            "可通过 start_char / end_char 指定不超过长度上限的窗口来分段获取未删改的原文。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要抓取的完整 URL（http/https）",
                required=True,
            ),
            ToolParameterInfo(
                name="start_char",
                param_type=ToolParamType.INTEGER,
                description="返回文本窗口的起始字符位置，默认 0",
                required=False,
                default=0,
            ),
            ToolParameterInfo(
                name="end_char",
                param_type=ToolParamType.INTEGER,
                description="返回文本窗口的结束字符位置，默认 -1 表示文档末尾",
                required=False,
                default=-1,
            ),
            ToolParameterInfo(
                name="on_exceed",
                param_type=ToolParamType.STRING,
                description="窗口内容超过长度上限时的处理方式：summarize=LLM 总结（默认），truncate=截断返回原文",
                required=False,
                enum_values=["summarize", "truncate"],
                default="summarize",
            ),
            ToolParameterInfo(
                name="summary_focus",
                param_type=ToolParamType.STRING,
                description="总结时需要特别关注的方面（可选）",
                required=False,
                default="",
            ),
        ],
    )
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
        try:
            return await self._fetch_url_impl(
                url=str(url or "").strip(),
                start_char=_safe_int(start_char, 0),
                end_char=_safe_int(end_char, -1),
                on_exceed=str(on_exceed or "summarize").strip().lower(),
                summary_focus=str(summary_focus or ""),
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

    async def _fetch_url_impl(
        self,
        *,
        url: str,
        start_char: int,
        end_char: int,
        on_exceed: str,
        summary_focus: str,
    ) -> dict[str, Any]:
        """fetch_url 的主流程。"""
        if not url:
            raise FetchUrlError("缺少必要参数 url")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise FetchUrlError("仅支持 http/https 协议的 URL")
        if not parsed.hostname:
            raise FetchUrlError("URL 缺少有效的主机名")
        await self._assert_host_allowed(parsed.hostname)

        async with self._build_client() as client:
            probe = await self._probe_url(client, url)

            if probe["kind"] == "image":
                return await self._build_image_result(url, probe)

            markdown, provider, final_url = await self._fetch_text_content(client, url, probe)

        return await self._build_text_result(
            markdown=markdown,
            provider=provider,
            url=url,
            final_url=final_url,
            start_char=start_char,
            end_char=end_char,
            on_exceed=on_exceed,
            summary_focus=summary_focus,
        )

    async def _fetch_text_content(
        self,
        client: httpx.AsyncClient,
        url: str,
        probe: dict[str, Any],
    ) -> tuple[str, str, str]:
        """获取非图片内容的 Markdown 文本。

        Returns:
            tuple: ``(markdown, provider, final_url)``。
        """
        is_pdf = probe["kind"] == "pdf"

        if self._jina_enabled:
            try:
                markdown = await self._fetch_via_jina(url)
                if markdown.strip():
                    return markdown.strip(), "jina", probe["final_url"]
                raise FetchUrlError("jina 返回了空内容")
            except FetchUrlError as exc:
                self.ctx.logger.warning("jina 抓取失败，回退本地抓取：url=%s, error=%s", url, exc)
            except Exception as exc:
                self.ctx.logger.warning(
                    "jina 抓取失败，回退本地抓取：url=%s, error=%s", url, _format_exception(exc)
                )

        if is_pdf:
            raise FetchUrlError(
                "该 URL 是 PDF 文件，本地解析器不支持 PDF；请启用 jina（或检查 jina 可用性）后重试"
            )

        if probe["kind"] == "text":
            text = probe["data"].decode(probe["encoding"], errors="replace")
            content_type = probe["content_type"]
            if "html" in content_type or content_type in {"", "application/octet-stream"}:
                markdown = await asyncio.to_thread(
                    self._html_to_markdown_blocking, text, probe["final_url"]
                )
            else:
                markdown = text
            return markdown, "markdownify", probe["final_url"]

        markdown, final_url = await self._fetch_direct_markdown(client, url)
        return markdown, "markdownify", final_url

    async def _build_image_result(self, url: str, probe: dict[str, Any]) -> dict[str, Any]:
        """图片路径：转码 / 压缩后通过 content_items 回传。"""
        try:
            info = await asyncio.to_thread(
                _normalize_inbound_image_blocking,
                probe["data"],
                acceptable_formats=self._acceptable_formats,
                convert_format=self._convert_format,
                max_image_size=self._max_image_size,
                max_dimension=self._max_dimension,
                quality_start=self._quality_start,
                quality_floor=self._quality_floor,
            )
        except FetchUrlError:
            raise
        except Exception as exc:
            raise FetchUrlError(f"图片解析失败，可能不是有效的图片文件（{_format_exception(exc)}）") from exc

        digest = sha256(info["data"]).hexdigest()[:8]
        notes: list[str] = []
        if info["converted"]:
            notes.append(f"已从 {info['original_format']} 转换为 {info['format']}")
            if info["quality"] is not None:
                notes.append(f"压缩质量 {info['quality']}")
        if info["downscaled"]:
            notes.append("已缩小尺寸")
        note_text = f"（{'，'.join(notes)}）" if notes else ""

        content = (
            f"已获取图片：{probe['final_url']}\n"
            f"格式 {info['format']}，尺寸 {info['width']}x{info['height']}，"
            f"大小 {len(info['data'])} 字节{note_text}。图片内容见对应的媒体消息。"
        )
        return {
            "success": True,
            "content": content,
            "final_url": probe["final_url"],
            "content_items": [
                {
                    "content_type": "image",
                    "data": b64encode(info["data"]).decode("ascii"),
                    "mime_type": f"image/{info['format']}",
                    "name": f"fetch_{digest}.{info['format']}",
                    "description": f"通过 fetch_url 抓取的图片：{url}",
                    "metadata": {
                        "source_url": url,
                        "final_url": probe["final_url"],
                        "original_format": info["original_format"],
                        "original_size": info["original_size"],
                        "output_size": len(info["data"]),
                        "width": info["width"],
                        "height": info["height"],
                        "converted": info["converted"],
                        "animation_preserved": info["animation_preserved"],
                    },
                }
            ],
        }

    async def _build_text_result(
        self,
        *,
        markdown: str,
        provider: str,
        url: str,
        final_url: str,
        start_char: int,
        end_char: int,
        on_exceed: str,
        summary_focus: str,
    ) -> dict[str, Any]:
        """文本路径：窗口切片 + VLM alt 替换 + 超长总结 / 截断。"""
        total_chars = len(markdown)
        start = max(0, min(start_char, total_chars))
        end = total_chars if end_char < 0 else max(start, min(end_char, total_chars))
        window = markdown[start:end]

        if not window:
            return {
                "success": True,
                "content": (
                    f"【fetch_url】来源：{final_url}（{provider}）\n"
                    f"文档总长 {total_chars} 字符，但请求的窗口 [{start}, {end}) 为空。"
                    f"请调整 start_char / end_char 后重试。"
                ),
                "total_chars": total_chars,
                "returned_range": [start, end],
                "processed": "empty",
                "provider": provider,
                "final_url": final_url,
            }

        window, described_count = await self._apply_vlm_alt_text(window, final_url)

        processed = "full"
        body = window
        notice_lines = [
            f"【fetch_url】来源：{final_url}（{provider}）",
            f"文档总长 {total_chars} 字符；本次窗口 [{start}, {end})。",
        ]
        if described_count:
            notice_lines.append(f"已用视觉模型为 {described_count} 张图片生成描述并替换 alt 文本。")

        if len(window) > self._max_content_length:
            use_summarize = self._llm_summarize and on_exceed != "truncate"
            summary = ""
            if use_summarize:
                summary = await self._summarize(window, final_url, summary_focus)
            if summary:
                processed = "summarized"
                body = summary
                notice_lines.append(
                    f"窗口内容超过上限 {self._max_content_length} 字符，以下为 LLM 总结的摘要；"
                    f"如需未删改原文，请用 start_char / end_char 指定不超过 "
                    f"{self._max_content_length} 字符的窗口分段获取。"
                )
            else:
                processed = "truncated"
                truncate_end = start + self._max_content_length
                body = window[: self._max_content_length]
                end = min(end, truncate_end)
                reason = "" if use_summarize is False else "（LLM 总结失败，已回退为截断）"
                notice_lines.append(
                    f"窗口内容超过上限 {self._max_content_length} 字符，已截断至 [{start}, {end})"
                    f"{reason}；可用 start_char={end} 继续获取后续内容。"
                )
                notice_lines[1] = f"文档总长 {total_chars} 字符；本次窗口 [{start}, {end})。"

        content = "\n".join(notice_lines) + "\n\n" + body
        return {
            "success": True,
            "content": content,
            "total_chars": total_chars,
            "returned_range": [start, end],
            "processed": processed,
            "provider": provider,
            "final_url": final_url,
        }


def _safe_int(value: Any, default: int) -> int:
    """宽容地将 LLM 传入的参数转换为整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def create_plugin() -> FetchUrlPlugin:
    """创建插件实例。"""
    return FetchUrlPlugin()
