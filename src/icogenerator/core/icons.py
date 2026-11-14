"""ICO 编码。

修复旧版最致命的一个缺陷：
``img.save(path, format='ICO', append_images=[...], save_all=True)`` 在
Pillow 10+ 会抛 ``KeyError: 'ICO'``（ICO 未注册进 ``SAVE_ALL``），
且异常被 GUI 的 ``except Exception`` 吞掉，用户只看到一句
「生成多尺寸ICO失败：'ICO'」。

本模块改用 Pillow 官方支持且已实测可用的 ``sizes=[...]`` 参数，
由 Pillow 在一次 save 中写入全部尺寸，无需 ``save_all``。
"""

from __future__ import annotations

import io
from typing import Dict, Iterable, List, Sequence, Tuple

from PIL import Image

from .errors import IcogenError, NoSizeSelectedError
from .imaging import (
    MASKABLE_SAFE_ZONE,
    MIN_PADDING,
    normalize_to_square,
    resize_for_ico,
)

# ICO 规范允许的尺寸上限；256x256 是 favicon 场景的实际最大值。
MAX_ICO_EDGE = 256

# 常用尺寸集合。ICO 头部的目录项只有 1 字节存宽高，256 需用 0 表示。
ICO_SIZES: Tuple[Tuple[int, int], ...] = (
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
)

# 预设方案：降低使用门槛，用户不必逐个勾选。
PRESET_SIZE_SETS: Dict[str, Tuple[str, Tuple[Tuple[int, int], ...]]] = {
    "favicon": (
        "网站 favicon（16/32/48）",
        ((16, 16), (32, 32), (48, 48)),
    ),
    "windows": (
        "Windows 程序图标（16/24/32/48/64/256）",
        ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)),
    ),
    "full": (
        "完整尺寸集",
        ICO_SIZES,
    ),
    "small": (
        "轻量（16/32）",
        ((16, 16), (32, 32)),
    ),
    "mobile": (
        "移动端 / PWA（48/64/128/256）",
        ((48, 48), (64, 64), (128, 128), (256, 256)),
    ),
}


def _validate_sizes(sizes: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """校验并归一化尺寸列表。

    Raises:
        NoSizeSelectedError: 未选择任何尺寸。
        IcogenError: 存在非法尺寸。
    """
    unique: List[Tuple[int, int]] = []
    seen = set()

    for item in sizes or ():
        try:
            width, height = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise IcogenError(f"非法尺寸：{item!r}") from exc

        if width <= 0 or height <= 0:
            raise IcogenError(f"尺寸必须为正数：{width}x{height}")
        if width > MAX_ICO_EDGE or height > MAX_ICO_EDGE:
            raise IcogenError(
                f"ICO 不支持大于 {MAX_ICO_EDGE}x{MAX_ICO_EDGE} 的尺寸：{width}x{height}"
            )
        if (width, height) not in seen:
            seen.add((width, height))
            unique.append((width, height))

    if not unique:
        raise NoSizeSelectedError(
            "请至少选择一个 ICO 尺寸", hint="可点击「全选」或选择一套预设方案"
        )

    return unique


def read_ico_sizes(data: bytes) -> List[Tuple[int, int]]:
    """解析 ICO 字节流中实际包含的尺寸（自检与测试用）。"""
    image = Image.open(io.BytesIO(data))
    return sorted(image.ico.sizes())


def validate_sizes(sizes: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """校验并归一化尺寸列表（对外公开入口）。

    ``build_ico`` / ``build_ico_bundle`` 内部共用同一套校验，
    供 assets 等模块复用，避免各处重复实现。
    """
    return _validate_sizes(sizes)


# ICO 帧编码格式。bmp 为 Pillow 默认（历史行为）；png 帧在同等画质下体积显著更小
# （借鉴 duma520/Image_To_Icon_Converter 的实现：现代 Windows 10/11 均支持 PNG 帧，
# 仅 Windows XP 等旧系统不识别），通过 bitmap_format 参数显式开启。
BITMAP_FORMATS: Tuple[str, str] = ("bmp", "png")


def build_ico(
    image: Image.Image,
    sizes: Sequence[Tuple[int, int]],
    *,
    fit_mode: str = "cover",
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
    bitmap_format: str = "bmp",
) -> bytes:
    """把单张图像编码为多尺寸 ICO。

    Args:
        image: 已解码的源图（任意模式）。
        sizes: 目标尺寸列表。
        fit_mode: 非正方形源的适配方式，见 ``normalize_to_square``。
        safe_zone: ``maskable`` 模式的安全区比例，见 ``normalize_to_square``。
        background: 留白填充色 RGB。
        opaque_pad: 留白是否不透明。
        padding: 通用内容留白比例，见 ``normalize_to_square``。
        bitmap_format: 帧编码格式，``bmp``（默认，历史行为）或 ``png``
            （同等画质体积更小，仅旧系统如 Windows XP 不识别）。

    Returns:
        完整的 ICO 文件字节。

    实现说明（两个实测确认的 Pillow 行为，缺一不可）：

    1. **必须用 ``sizes=``，不能用 ``save_all=True``。**
       后者要求格式注册进 ``SAVE_ALL``，而 ICO 没有注册，
       Pillow 10+ 会抛 ``KeyError: 'ICO'``。这是旧版核心功能 100% 崩溃的根因。

    2. **基准图必须不小于最大目标尺寸。**
       Pillow 的 ICO 插件会「静默跳过」所有大于基准图的尺寸：
       基准 16px 配 6 个尺寸，实测只写出 1 个，且不报错。
       因此先把基准图升采样到 max(sizes)，再交给 Pillow 缩放出全部尺寸。
    """
    if bitmap_format not in BITMAP_FORMATS:
        raise ValueError(f"未知的 ICO 帧格式：{bitmap_format}")
    target_sizes = _validate_sizes(sizes)
    square = normalize_to_square(
        image, mode=fit_mode, background=background,
        safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
    )

    # 关键：基准图升采样到最大目标边长，避免 Pillow 静默丢弃大尺寸。
    base_edge = max(max(s) for s in target_sizes)
    base = resize_for_ico(square, (base_edge, base_edge))

    buffer = io.BytesIO()
    try:
        base.save(buffer, format="ICO", sizes=target_sizes,
                  bitmap_format=bitmap_format)
    except IcogenError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转换为业务异常并保留原链
        raise IcogenError(
            f"ICO 编码失败：{exc}",
            hint="请更换源图后重试，若持续失败请反馈该文件",
        ) from exc

    produced = buffer.getvalue()

    # 自检：编码结果必须真的包含全部请求的尺寸，否则宁可报错也不交付残缺 ICO。
    try:
        actual = read_ico_sizes(produced)
    except Exception:  # noqa: BLE001 - 解析失败同样视为编码异常
        raise IcogenError("ICO 编码结果无法解析") from None

    missing = [s for s in target_sizes if s not in actual]
    if missing:
        raise IcogenError(
            "ICO 编码结果不完整，缺少尺寸："
            + "、".join(f"{w}x{h}" for w, h in missing),
            hint="请反馈该场景，便于定位 Pillow 版本差异",
        )

    return produced


def build_ico_bundle(
    image: Image.Image,
    sizes: Sequence[Tuple[int, int]],
    *,
    fit_mode: str = "cover",
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
    bitmap_format: str = "bmp",
) -> Dict[Tuple[int, int], bytes]:
    """为每个尺寸生成独立 ICO（旧版「下载全部」功能）。

    独立文件与合并文件用途不同：前者给需要单尺寸的场景，
    后者给 favicon。前台两个按钮都保留，功能不丢。
    """
    if bitmap_format not in BITMAP_FORMATS:
        raise ValueError(f"未知的 ICO 帧格式：{bitmap_format}")
    target_sizes = _validate_sizes(sizes)
    square = normalize_to_square(
        image, mode=fit_mode, background=background,
        safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
    )

    bundle: Dict[Tuple[int, int], bytes] = {}
    for size in target_sizes:
        buffer = io.BytesIO()
        try:
            resize_for_ico(square, size).save(
                buffer, format="ICO", sizes=[size],
                bitmap_format=bitmap_format,
            )
        except Exception as exc:  # noqa: BLE001
            raise IcogenError(
                f"生成 {size[0]}x{size[1]} ICO 失败：{exc}"
            ) from exc

        data = buffer.getvalue()
        if size not in read_ico_sizes(data):
            raise IcogenError(f"生成的 {size[0]}x{size[1]} ICO 未包含目标尺寸")
        bundle[size] = data
    return bundle


def make_png_preview(
    image: Image.Image,
    max_edge: int = 256,
    *,
    fit_mode: str = "contain",
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
) -> bytes:
    """生成预览用 PNG。

    旧版预览直接 ``img.save('temp_preview.png')`` 且不转模式，
    CMYK 源图会抛 ``OSError: cannot write mode CMYK as PNG``。
    这里先经 ``normalize_to_square`` 统一为 RGBA，从根上消除该崩溃。
    """
    square = normalize_to_square(
        image, mode=fit_mode, background=background,
        safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
    )
    side = min(max_edge, square.width)
    preview = square.resize((side, side), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    preview.save(buffer, format="PNG")
    return buffer.getvalue()