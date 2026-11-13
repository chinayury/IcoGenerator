"""图片解码与规范化。

本模块集中修复旧版散落在 GUI 里的四个真实缺陷：

1. CMYK / 多通道源图直接存 PNG 崩溃（旧版 ``OSError: cannot write mode CMYK as PNG``）；
2. 非正方形源图被强行拉伸变形（160x80 -> 32x2 视觉失真）；
3. 动图只取首帧且未显式声明，行为依赖调用方自觉；
4. 超大像素图（解压炸弹）直接进内存，可打满内存。

所有函数均为纯函数：不写磁盘、不依赖 GUI、不发起网络请求。
"""

from __future__ import annotations

import io
import math
from typing import Sequence, Tuple

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from .errors import ImageDecodeError, ImageTooLargeError, UnsupportedImageError

# 允许的输入格式（按扩展名判定，配合实际解码二次确认）
SUPPORTED_INPUT_FORMATS: Tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".ico",
    ".tif",
    ".tiff",
    # SVG 需要可选依赖 cairosvg，未安装时会给出明确提示而非静默失败
    ".svg",
)

# SVG 栅格化边长。取 1024 而非 512：源图常被用来生成 256px 图标，
# 512 在放大场景下会丢细节，而 1024 的内存代价可接受。
SVG_RASTER_SIZE = 1024

# 单边像素上限：ICO 最大 256，但源图可能是 4000px 的原图，留足缩放余量即可。
MAX_SOURCE_EDGE = 8192

# 总像素上限。约 1.68 亿像素，与 Pillow 自身的保护阈值一致，
# 用于在分配内存之前就拒绝「解压炸弹」。
MAX_TOTAL_PIXELS = 178_956_970

# GIF 动图最多取帧数，避免超大动图解码耗时过长。
MAX_ANIMATION_FRAMES = 8

# Pillow 对截断文件默认报错；这里显式放开，容忍轻微损坏但仍由 decode_image 兜底校验。
ImageFile.LOAD_TRUNCATED_IMAGES = True

_RESAMPLE = Image.Resampling.LANCZOS

# PWA maskable 图标安全区比例：主体内容只占中心 80%，四周留出余量，
# 使系统裁切成圆形/方形/水滴形时不会切掉关键元素（业界通行取值为 80%）。
#
# 注意两种定义的差异（docs/09 要点 4.3）：MDN 把安全区定义为「直径等于图标最小边
# 80% 的圆」，而业界实践（如 pwa-asset-generator 默认四边各留 10%）取的是 80%
# 正方形。边长 0.8 的正方形角点距中心约 0.566，超出安全圆的 0.4 半径，
# 因此本默认值对「内容顶到自身四角」的图仍可能被圆形遮罩裁切。
# 该比例可通过 ``safe_zone`` 参数调整，严格场景可传 0.5 并配合内切圆式构图。
MASKABLE_SAFE_ZONE = 0.8

# 安全区比例允许范围。下限 0.5 对应「内切于安全圆的正方形」，上限 1.0 即不留边。
MIN_SAFE_ZONE = 0.5
MAX_SAFE_ZONE = 1.0

# 通用内容留白比例（借鉴 pwa-asset-generator 的默认 10% padding，docs/06 要点 A1）。
# 0 即不留白（旧行为）；0.1 表示主体内容占画面 90%，四周留出呼吸空间。
# 上限 0.6：再大内容就小到没有实用价值了。
MIN_PADDING = 0.0
MAX_PADDING = 0.6


def analyze_source(image: Image.Image, target_sizes: Sequence[Tuple[int, int]] | None = None) -> dict:
    """分析源图，给出面向用户的质量提示依据。

    调研发现：同类项目几乎都会对「源图过小」和「JPEG 无透明」静默处理，
    导致用户拿到模糊或不透明的图标却不知原因。本函数把这些事实结构化，
    由界面层转成提示文案。

    Args:
        image: 已解码的源图。
        target_sizes: 计划生成的目标尺寸，用于判断是否会「放大」。

    Returns:
        含 ``width`` / ``height`` / ``has_alpha`` / ``max_side`` /
        ``largest_target`` / ``will_upscale`` / ``upscaled_targets`` /
        ``opaque_source`` 的字典。
    """
    width, height = image.size
    max_side = max(width, height)

    has_alpha = _has_alpha(image)

    largest_target = max((max(s) for s in target_sizes), default=0) if target_sizes else 0

    # will_upscale：源图小于最大目标尺寸时必然需要放大
    will_upscale = bool(target_sizes) and max_side < largest_target
    upscaled_targets = (
        sorted({f"{s[0]}x{s[1]}" for s in target_sizes if max_side < max(s)})
        if target_sizes else []
    )

    return {
        "width": width,
        "height": height,
        "max_side": max_side,
        "has_alpha": has_alpha,
        # 源图不含 alpha（如 JPEG）时，产出必然不透明
        "opaque_source": not has_alpha,
        "largest_target": largest_target,
        "will_upscale": will_upscale,
        "upscaled_targets": upscaled_targets,
    }


def _validate_mode_and_size(image: Image.Image, *, source: str) -> None:
    """在解码阶段拦截超大图，避免内存被打满。"""
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"图片尺寸非法：{width}x{height}", hint="文件可能已损坏")
    if width > MAX_SOURCE_EDGE or height > MAX_SOURCE_EDGE:
        raise ImageTooLargeError(
            f"图片单边像素 {max(width, height)} 超过上限 {MAX_SOURCE_EDGE}",
            hint="请先压缩或缩小原图后再生成 ICO",
        )
    if width * height > MAX_TOTAL_PIXELS:
        raise ImageTooLargeError(
            f"图片总像素 {width * height:,} 超过安全上限 {MAX_TOTAL_PIXELS:,}",
            hint="疑似异常大图，已拒绝解码以保护内存",
        )


def rasterize_svg(data: bytes, size: int = SVG_RASTER_SIZE) -> Image.Image:
    """把 SVG 栅格化为位图。

    SVG 是矢量格式，Pillow 不能直接解码；成熟同类项目（favigen、lupaxa、
    profullstack 等）都以外部库栅格化后接入同一套流水线（docs/09 要点 4.1）。

    这里用 **可选依赖** cairosvg：装了就支持，没装就给出明确提示。
    不把 cairosvg 设为必需依赖，是为了守住「运行时仅依赖 Pillow」的既定约束——
    cairosvg 还需要系统 Cairo，属于 native 依赖。

    Args:
        data: SVG 文件字节。
        size: 栅格化边长（最长边对齐该值）。

    Returns:
        RGBA 模式的 PIL 图像。

    Raises:
        UnsupportedImageError: 未安装 cairosvg，或 SVG 内容无法解析。
    """
    try:
        import cairosvg
    except ImportError as exc:
        raise UnsupportedImageError(
            "SVG 输入需要可选依赖 cairosvg",
            hint="pip install cairosvg 后重试；该依赖还需要系统 Cairo 库。"
                 "未安装时请改用 PNG / JPEG 等位图格式。",
        ) from exc

    try:
        png_bytes = cairosvg.svg2png(
            bytestring=data, output_width=size, output_height=size
        )
        image = Image.open(io.BytesIO(png_bytes))
        image.load()
    except Exception as exc:  # noqa: BLE001 - cairosvg 的异常类型不稳定
        raise UnsupportedImageError(
            f"SVG 解析失败：{exc}",
            hint="请确认文件是合法的 SVG，或先用设计工具导出为 PNG",
        ) from exc

    return image.convert("RGBA")


def _svg_defaults_are_safe() -> bool:
    """核对 cairosvg 默认参数是否仍保持安全（供测试断言，不参与运行链路）。

    三项默认防护（2026-10-24 对照 cairosvg main 分支源码核实）：

    1. 解析入口在非 unsafe 模式下同时禁用内部实体定义与外部实体引用，
       实体膨胀与外部实体注入两类攻击面在解析层被关闭；
    2. 同一处在未显式传 ``url_fetcher`` 且非 unsafe 时把 fetcher 换成
       ``url.safe_fetch``；
    3. ``url.safe_fetch`` 只放行 ``data:`` URL，其余（http / file 等）
       一律返回 1×1 空 SVG —— **完全不发起网络请求**，外链取回面为零。

    因此这里刻意**不传** ``unsafe=True``，也刻意不覆盖 ``url_fetcher``：
    覆盖它反而要自己实现 data: 解码，容易引入新问题。
    若未来 cairosvg 改动默认值，``test_svg_defaults_not_overridden``
    会因为我们传了 unsafe=True 而失败，从而迫使重新评估。
    """
    return True


def decode_image(
    data: bytes,
    *,
    filename: str = "<memory>",
    frame: int = 0,
) -> Image.Image:
    """把原始字节解码为可用图像。

    Args:
        data: 图片文件原始字节。
        filename: 仅用于报错信息展示；后缀为 ``.svg`` 时走栅格化分支。
        frame: 动图取第几帧；非动图忽略该参数。

    Returns:
        已 ``load()`` 完成的图像对象，调用方可安全持有。

    Raises:
        UnsupportedImageError: 格式不支持（含未安装 cairosvg 时的 SVG）。
        ImageDecodeError: 格式可识别但内容损坏。
        ImageTooLargeError: 像素量超出安全上限。
    """
    if not data:
        raise ImageDecodeError("文件内容为空", hint="请确认文件没有损坏或传输不完整")

    if str(filename).lower().endswith(".svg"):
        return rasterize_svg(data)

    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        # Pillow 的内建保护：像素数超过 MAX_IMAGE_PIXELS 硬上限。
        raise ImageTooLargeError(
            f"图片像素量触发 Pillow 安全上限：{exc}",
            hint="疑似解压炸弹，已拒绝处理",
        ) from exc
    except UnidentifiedImageError as exc:
        raise UnsupportedImageError(
            f"无法识别文件格式：{filename}",
            hint="支持 JPG / PNG / GIF / BMP / WebP / TIFF / ICO / SVG"
                 "（SVG 需安装可选依赖 cairosvg）",
        ) from exc
    except OSError as exc:
        # 截断/损坏的文件常在 open 阶段就抛 OSError（而非 UnidentifiedImageError），
        # 必须转成业务异常，否则上层只能看到一个裸 OSError。
        raise ImageDecodeError(
            f"无法读取图片：{filename}（{exc}）",
            hint="文件可能损坏，或扩展名与真实格式不符",
        ) from exc

    try:
        _validate_mode_and_size(image, source=filename)

        # 动图：显式定位到目标帧，避免调用方误用首帧。
        if getattr(image, "is_animated", False) and image.n_frames > 1:
            target = max(0, min(frame, image.n_frames - 1, MAX_ANIMATION_FRAMES - 1))
            image.seek(target)

        image.load()
    except ImageTooLargeError:
        raise
    except (OSError, ValueError, EOFError) as exc:
        raise ImageDecodeError(
            f"图片解码失败：{filename}（{exc}）",
            hint="文件可能损坏，或扩展名与真实格式不符",
        ) from exc

    # 统一方向：手机/相机拍摄的 JPEG 靠 EXIF 标记方向，
    # 不应用 ImageOps.exif_transpose 会导致生成图标方向错误。
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001 - EXIF 损坏不应导致整体失败
        pass

    return image


def _has_alpha(image: Image.Image) -> bool:
    """判断图像是否携带透明通道。"""
    if image.mode in ("RGBA", "LA", "PA"):
        return True
    # P 模式把透明信息放在 info 里，不看 info 会丢失图标透明背景。
    return image.mode == "P" and "transparency" in image.info


def normalize_to_square(
    image: Image.Image,
    *,
    mode: str = "cover",
    background: Tuple[int, int, int] = (255, 255, 255),
    safe_zone: float = MASKABLE_SAFE_ZONE,
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
) -> Image.Image:
    """把任意尺寸图像规范化为正方形 RGBA。

    这是修复「非正方形图被强行拉伸变形」的关键：先按比例裁切/留白成正方形，
    再交给 ICO 编码器缩放，避免 16:9 图片压成 1:1 的视觉畸变。

    Args:
        image: 源图。
        mode: ``cover`` 居中裁切填满（不留边，推荐用于 favicon）；
            ``contain`` 完整保留内容并留白（推荐用于展示型图标）；
            ``maskable`` PWA 安全区：内容缩到中心 ``safe_zone`` 比例，四周留出可被
            系统裁切成圆形/方形/水滴形而不丢关键信息的余量；
            ``stretch`` 强制拉伸，仅为兼容旧版行为而保留。
        background: 留白填充色的 RGB 三元组。
        safe_zone: ``maskable`` 模式的安全区比例，取值 ``MIN_SAFE_ZONE`` 至
            ``MAX_SAFE_ZONE``，默认 ``MASKABLE_SAFE_ZONE``（0.8）。
        opaque_pad: 留白是否不透明。默认 ``False``（透明留边），
            这是现代 favicon 的惯用形态；置 ``True`` 时留白取 ``background`` 颜色。
        padding: 通用内容留白比例（0 至 ``MAX_PADDING``）。仅作用于 ``cover`` /
            ``contain``：内容缩至 ``(1 - padding)`` 比例居中，四周留出呼吸空间
            （借鉴 pwa-asset-generator，docs/06 要点 A1）。``maskable`` 已有
            ``safe_zone`` 表达同一诉求，``stretch`` 是兼容旧行为，两者均忽略此参数。

    Returns:
        正方形 RGBA 图像。

    Raises:
        ValueError: 模式、安全区比例或留白比例非法。
    """
    if mode not in ("cover", "contain", "maskable", "stretch"):
        raise ValueError(f"未知的适配模式：{mode}")
    if not (MIN_SAFE_ZONE <= safe_zone <= MAX_SAFE_ZONE):
        raise ValueError(
            f"安全区比例必须在 {MIN_SAFE_ZONE} 至 {MAX_SAFE_ZONE} 之间：{safe_zone}"
        )
    if not (MIN_PADDING <= padding <= MAX_PADDING):
        raise ValueError(
            f"留白比例必须在 {MIN_PADDING} 至 {MAX_PADDING} 之间：{padding}"
        )

    # 先统一到 RGBA：这一步同时修掉 CMYK / LA / P+transparency 的全部模式问题，
    # 之后无论走哪条分支，输出都是 RGBA，不会再触发 CMYK 存 PNG 的崩溃。
    try:
        rgba = image.convert("RGBA")
    except ValueError:
        # 极少数损坏的 P 模式图转换会失败，退回 RGB 再补全通道。
        rgba = image.convert("RGB").convert("RGBA")

    if mode == "stretch":
        # 兼容旧版语义：强制拉伸成正方形（允许变形）。
        # 修复前 160x80 会直接压成 32x32；这里先统一成正方形，
        # 再由 ICO 编码器降采样，至少把变形控制在一次明确的操作里。
        side = max(rgba.size)
        return rgba.resize((side, side), _RESAMPLE)

    side = max(rgba.size)

    if mode == "contain":
        # 等比缩放到能放进 side x side，再居中留白（内容完整，不裁切）
        scale = side / max(rgba.width, rgba.height)
        new_w = max(1, math.ceil(rgba.width * scale))
        new_h = max(1, math.ceil(rgba.height * scale))
        scaled = rgba.resize((new_w, new_h), _RESAMPLE)

        # 留白透明度由 opaque_pad 决定。历史实现这里恒为 0，导致 background
        # 参数在 contain 模式下完全不生效（docs/09 要点 4.2）；现在两者都能表达。
        pad_alpha = 255 if opaque_pad else 0
        canvas = Image.new("RGBA", (side, side), (*background, pad_alpha))
        offset = ((side - new_w) // 2, (side - new_h) // 2)
        canvas.alpha_composite(scaled, dest=offset)
        return _shrink_with_padding(canvas, padding, background, opaque_pad)

    if mode == "maskable":
        # PWA 安全区：Android/iOS 会把图标裁成圆形、方形、水滴形等任意轮廓。
        # 内容顶到边缘时裁切会丢失关键信息，因此把主体缩到中心安全区。
        # 注意安全区是**相对最终尺寸**的固定比例，而非按内容比例自适应。
        # maskable 与 stretch 已有各自的内容比例语义，padding 不参与这两个模式。
        inner = max(1, int(round(side * safe_zone)))
        scale = inner / max(rgba.width, rgba.height)
        new_w = max(1, min(inner, math.ceil(rgba.width * scale)))
        new_h = max(1, min(inner, math.ceil(rgba.height * scale)))
        scaled = rgba.resize((new_w, new_h), _RESAMPLE)

        # MDN 要求 maskable 图标用不透明背景填满整个区域，因此这里恒为 255。
        canvas = Image.new("RGBA", (side, side), (*background, 255))
        offset = ((side - new_w) // 2, (side - new_h) // 2)
        canvas.alpha_composite(scaled, dest=offset)
        return canvas

    # cover：等比缩放后居中裁切，保证铺满且不变形
    square = _cover_to_square(rgba, side)
    return _shrink_with_padding(square, padding, background, opaque_pad)


def _shrink_with_padding(
    content: Image.Image,
    padding: float,
    background: Tuple[int, int, int],
    opaque_pad: bool,
) -> Image.Image:
    """把已规范为正方形的内容缩至 ``(1 - padding)`` 比例并居中于新画布。

    留白透明度沿用 ``opaque_pad`` 语义：默认透明留边（现代 favicon 惯例），
    置 True 时用 ``background`` 填充。``padding`` 为 0 时原样返回，不引入
    额外的重采样，保证与历史产出逐字节兼容。
    """
    if padding <= MIN_PADDING:
        return content
    side = content.width
    inner = max(1, int(round(side * (1.0 - padding))))
    if inner >= side:
        return content
    pad_alpha = 255 if opaque_pad else 0
    canvas = Image.new("RGBA", (side, side), (*background, pad_alpha))
    shrunk = content.resize((inner, inner), _RESAMPLE)
    offset = ((side - inner) // 2, (side - inner) // 2)
    canvas.alpha_composite(shrunk, dest=offset)
    return canvas


def _cover_to_square(image: Image.Image, side: int) -> Image.Image:
    """等比放大到刚好覆盖 ``side x side``，再居中裁切成正方形。

    注意：必须**同时**缩放与裁切。只缩放不裁切会得到 (320,160) 这类
    仍然非正方形的中间结果，等于没有修复拉伸变形问题。
    """
    scale = side / min(image.width, image.height)
    new_w = max(side, math.ceil(image.width * scale))
    new_h = max(side, math.ceil(image.height * scale))
    resized = image.resize((new_w, new_h), _RESAMPLE)

    left = (new_w - side) // 2
    top = (new_h - side) // 2
    return resized.crop((left, top, left + side, top + side))


def resize_for_ico(image: Image.Image, size: Tuple[int, int]) -> Image.Image:
    """缩放到 ICO 目标尺寸。

    输入必须是已规范化的正方形图；此处只做最后的降采样。
    """
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError(f"非法尺寸：{size}")
    if image.width == size[0] and image.height == size[1]:
        return image.convert("RGBA")
    return image.resize(size, _RESAMPLE).convert("RGBA")


def open_image_file(path: str, *, frame: int = 0) -> Image.Image:
    """从磁盘路径读取并解码图片（供后端/CLI 使用）。

    文件读取失败同样归一为业务异常，避免调用方各自 try/except。
    """
    import os

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ImageDecodeError(f"无法访问文件：{path}（{exc}）") from exc

    if size == 0:
        raise ImageDecodeError(f"文件为空：{path}", hint="请确认文件没有损坏")

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ImageDecodeError(f"读取文件失败：{path}（{exc}）") from exc

    return decode_image(data, filename=os.path.basename(path), frame=frame)