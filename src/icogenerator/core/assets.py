"""站点物料包生成。

调研发现（见 docs/08）：**所有同类项目都只输出 ICO**，但真实站点需要的远不止。
IconGenerator / dostow 等项目已开始输出 apple-touch-icon、android-chrome、
mstile、manifest.json 等物料，却没有一个把它们整合成「一次生成」。

本模块补齐这块：一次输入，产出可直接投入使用的完整站点物料。

产物清单::

    favicon.ico                        多尺寸 ICO（浏览器 / Windows）
    favicon-16x16.png                  浏览器标签（标准 DPI）
    favicon-32x32.png                  浏览器标签（HiDPI）
    apple-touch-icon.png               iOS 主屏（180）
    android-chrome-192x192.png         Android 自适应图标
    android-chrome-512x512.png         PWA 安装 / 启动画面
    android-chrome-512x512-maskable.png PWA 安全区变体
    mstile-150x150.png                 Windows 磁贴
    browserconfig.xml                  Windows 磁贴配置
    manifest.json                      PWA Web App Manifest
    site-icon.html                     可直接粘贴的 HTML 引用片段
    MANIFEST.txt                       本包的自说明清单（docs/09 要点 5.3）

尺寸取值依据多份格式资料的交叉验证（详见调研报告要点 3）。
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

from .imaging import (
    MASKABLE_SAFE_ZONE,
    MIN_PADDING,
    normalize_to_square,
    resize_for_ico,
)
from .icons import build_ico, validate_sizes

# 各平台的推荐尺寸（来源：favicon 尺寸对照表，多份资料交叉验证一致）
FAVICON_SIZES: Tuple[Tuple[int, int], ...] = ((16, 16), (32, 32), (48, 48))
APPLE_TOUCH_SIZE = 180
ANDROID_SIZES: Tuple[Tuple[int, int], ...] = ((192, 192), (512, 512))
MSTILE_SIZE = 150

# manifest 里的主题色 / 背景色默认值，可被调用方覆盖
DEFAULT_THEME_COLOR = "#2563eb"
DEFAULT_BACKGROUND_COLOR = "#ffffff"
DEFAULT_DISPLAY = "standalone"

# 物料包内清单文件名
MANIFEST_FILENAME = "MANIFEST.txt"

# 矢量 favicon 文件名（源图为 SVG 时随物料包附带，借鉴要点 C1）
SVG_FAVICON_FILENAME = "favicon.svg"


def png_bytes(
    image: Image.Image,
    size: Tuple[int, int],
    fit_mode: str,
    *,
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
) -> bytes:
    """生成指定尺寸的独立 PNG 字节（供站点物料包与 CLI --png-sizes 共用）。"""
    import io

    square = normalize_to_square(
        image, mode=fit_mode, background=background,
        safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
    )
    buffer = io.BytesIO()
    resize_for_ico(square, size).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_manifest(
    *,
    name: str = "My App",
    short_name: Optional[str] = None,
    theme_color: str = DEFAULT_THEME_COLOR,
    background_color: str = DEFAULT_BACKGROUND_COLOR,
    display: str = DEFAULT_DISPLAY,
) -> str:
    """生成 PWA Web App Manifest。

    同时输出 ``purpose: "any maskable"``：现代浏览器会自动在需要安全裁切时
    使用 maskable 版本，因此建议与普通版本同时提供。
    """
    short = short_name or name[:12]
    manifest = {
        "name": name,
        "short_name": short,
        "icons": [
            {
                "src": "android-chrome-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "android-chrome-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "android-chrome-512x512-maskable.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
        "theme_color": theme_color,
        "background_color": background_color,
        "display": display,
        "start_url": "/",
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False)


def merge_manifest(existing_text: str) -> str:
    """把本项目的图标物料合并进用户既有的 manifest.json（借鉴要点 A3）。

    设计原则（与 pwa-asset-generator 的 manifest 自动更新相比刻意保守）：
    - 只增改 ``icons`` 数组：移除指向本项目产物文件名的旧条目再追加新条目；
    - 其余字段（name / theme_color / start_url 等）**原样保留**，绝不覆盖用户配置；
    - 解析失败或根节点不是对象时返回原文，不产出半损坏的 manifest。

    Args:
        existing_text: 用户现有 manifest.json 的文本内容。

    Returns:
        合并后的 manifest.json 文本。
    """
    try:
        data = json.loads(existing_text)
    except (json.JSONDecodeError, ValueError):
        # 解析不了就不动它：宁可让用户手动合并，也不能毁掉现有配置
        return existing_text
    if not isinstance(data, dict):
        return existing_text

    ours = {f"android-chrome-{s[0]}x{s[1]}.png" for s in ANDROID_SIZES}
    ours.add("android-chrome-512x512-maskable.png")

    icons = [entry for entry in data.get("icons", []) if isinstance(entry, dict)]
    kept = [
        entry for entry in icons
        if not any(str(entry.get("src", "")).endswith(name) for name in ours)
    ]

    kept.extend([
        {
            "src": "android-chrome-192x192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "android-chrome-512x512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "android-chrome-512x512-maskable.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ])

    merged = dict(data)
    merged["icons"] = kept
    return json.dumps(merged, indent=2, ensure_ascii=False)


def build_browserconfig(tile_color: str = DEFAULT_THEME_COLOR) -> str:
    """生成 Windows 磁贴配置 browserconfig.xml。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<browserconfig>\n'
        "  <msapplication>\n"
        "    <tile>\n"
        '      <square150x150logo src="mstile-150x150.png"/>\n'
        f'      <TileColor>{tile_color}</TileColor>\n'
        "    </tile>\n"
        "  </msapplication>\n"
        "</browserconfig>\n"
    )


def build_html_snippet(*, include_manifest: bool = True, include_svg: bool = False) -> str:
    """生成可直接粘贴进 index.html 的引用片段。

    Android 尺寸由 ``manifest.json`` 声明，无需在 HTML 里重复 link
    （Android Chrome 只认 manifest 中的 icons，不读 <link rel="icon">）。
    源图为 SVG 时 ``include_svg`` 会追加矢量 favicon 引用：支持它的浏览器
    优先用矢量渲染，不支持的老浏览器回落到 ICO（借鉴要点 C1）。
    """
    lines = [
        '<!-- 将以下文件与你的页面放在同一目录 -->',
        '<link rel="icon" href="/favicon.ico" sizes="any">',
    ]
    if include_svg:
        lines.append('<link rel="icon" type="image/svg+xml" href="/favicon.svg">')
    lines.extend([
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">',
        '<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">',
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">',
        '<meta name="msapplication-config" content="/browserconfig.xml">',
        '<meta name="msapplication-TileColor" content="%s">' % DEFAULT_THEME_COLOR,
    ])
    if include_manifest:
        lines.insert(1, '<link rel="manifest" href="/manifest.json">')
        lines.append(
            '<meta name="theme-color" content="%s">' % DEFAULT_THEME_COLOR
        )
    return "\n".join(lines)


def build_manifest_text(
    *,
    source_size: Tuple[int, int] = (0, 0),
    fit_mode: str = "cover",
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
    ico_sizes: Sequence[Tuple[int, int]] = FAVICON_SIZES,
    include_maskable: bool = True,
    name: str = "My App",
    theme_color: str = DEFAULT_THEME_COLOR,
) -> str:
    """生成物料包内的 MANIFEST.txt。

    借鉴来源（docs/09 要点 5.3）：favigen 的应用图标包内嵌 README，
    localkit 的构建产物额外写一份 receipt.txt。物料包脱离生成环境后，
    接收方需要知道每个文件是干什么的、用什么参数生成的——这份清单就是物料包的
    自说明文件，也是排查「为什么我的图标不透明」时的第一手资料。

    文件清单与 ``describe_site_assets()`` 复用同一数据源，避免两处文案漂移。
    内容不含时间戳，同样输入的输出保持字节一致，便于测试与校验。
    """
    sizes_text = ", ".join(f"{w}x{h}" for w, h in ico_sizes)
    background_text = "#%02x%02x%02x" % background
    lines = [
        "IcoGenerator 站点物料包清单",
        "=" * 30,
        "",
        "生成参数",
        "-" * 30,
        f"源图尺寸    : {source_size[0]}x{source_size[1]}",
        f"适配策略    : {fit_mode}",
        f"安全区比例  : {safe_zone:.2f}（maskable 变体使用）",
        f"留白比例    : {padding:.2f}（cover / contain 内容留白）",
        f"留白颜色    : {background_text}（补边不透明："
        f"{'是' if opaque_pad else '否'}）",
        f"favicon.ico : {sizes_text}",
        f"maskable    : {'包含' if include_maskable else '不包含'}",
        f"应用名      : {name}",
        f"主题色      : {theme_color}",
        "",
        "文件清单",
        "-" * 30,
    ]
    for item in describe_site_assets():
        lines.append(f"{item['file']:<42} {item['purpose']}")
    lines += [
        f"{MANIFEST_FILENAME:<42} 本清单文件",
        "",
        "使用方式：把除本清单外的文件放到网站根目录（或同级静态目录），",
        "再按 site-icon.html 中的片段把引用粘到页面 <head>。",
    ]
    return "\n".join(lines) + "\n"


def _looks_like_svg(data: bytes) -> bool:
    """轻量判定字节流是否为 SVG（只查前 4KB，不引入 XML 解析依赖）。"""
    if not data:
        return False
    head = data[:4096].lstrip().lower()
    return head.startswith(b"<?xml") or b"<svg" in head[:1024]


def build_site_assets(
    image: Image.Image,
    *,
    ico_sizes: Sequence[Tuple[int, int]] = FAVICON_SIZES,
    fit_mode: str = "cover",
    include_maskable: bool = True,
    name: str = "My App",
    theme_color: str = DEFAULT_THEME_COLOR,
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
    source_svg: Optional[bytes] = None,
    existing_manifest: Optional[str] = None,
) -> Dict[str, bytes]:
    """生成完整站点物料包。

    Args:
        image: 已解码的源图。
        ico_sizes: favicon.ico 内含的尺寸。
        fit_mode: 非正方形适配策略（cover / contain / maskable / stretch）。
        include_maskable: 是否额外产出 maskable 变体（供 PWA 安全裁切）。
        name: manifest 中的应用名。
        theme_color: 主题色，同时用于 manifest 与磁贴色。
        safe_zone: maskable 变体的安全区比例。
        background: 留白填充色 RGB。
        opaque_pad: 留白是否不透明。
        padding: 通用内容留白比例（见 ``normalize_to_square``）。
        source_svg: 源图为 SVG 时的原始字节；校验通过则附带 ``favicon.svg``
            并在 HTML 片段中追加矢量引用（借鉴要点 C1）。
        existing_manifest: 用户既有 manifest.json 文本；提供时只合并本项目的
            icons 条目，其余字段原样保留（借鉴要点 A3）。

    Returns:
        ``{文件名: 字节内容}``，可直接写盘或打包 ZIP。

    Raises:
        IcogenError: 尺寸非法。
    """
    validate_sizes(ico_sizes)

    assets: Dict[str, bytes] = {}

    # 1) 多尺寸 ICO —— 浏览器标签、Windows 快捷方式
    assets["favicon.ico"] = build_ico(
        image, ico_sizes, fit_mode=fit_mode, safe_zone=safe_zone,
        background=background, opaque_pad=opaque_pad, padding=padding,
    )

    # 2) 独立 PNG 尺寸（部分场景仍需要单独文件引用）
    for size in FAVICON_SIZES:
        assets[f"favicon-{size[0]}x{size[1]}.png"] = png_bytes(
            image, size, fit_mode, safe_zone=safe_zone,
            background=background, opaque_pad=opaque_pad, padding=padding,
        )

    # 3) iOS 主屏
    assets["apple-touch-icon.png"] = png_bytes(
        image, (APPLE_TOUCH_SIZE, APPLE_TOUCH_SIZE), fit_mode,
        safe_zone=safe_zone, background=background, opaque_pad=opaque_pad,
        padding=padding,
    )

    # 4) Android / PWA
    for size in ANDROID_SIZES:
        assets[f"android-chrome-{size[0]}x{size[1]}.png"] = png_bytes(
            image, size, fit_mode, safe_zone=safe_zone,
            background=background, opaque_pad=opaque_pad, padding=padding,
        )

    # 5) maskable 变体：内容缩到中心安全区，系统裁切成任意形状都不丢关键信息
    if include_maskable:
        assets["android-chrome-512x512-maskable.png"] = png_bytes(
            image, (512, 512), "maskable", safe_zone=safe_zone,
            background=background,
        )

    # 6) Windows 磁贴
    assets["mstile-150x150.png"] = png_bytes(
        image, (MSTILE_SIZE, MSTILE_SIZE), fit_mode, safe_zone=safe_zone,
        background=background, opaque_pad=opaque_pad, padding=padding,
    )

    # 7) 矢量 favicon：源图本身是 SVG 时直接透传，现代浏览器优先用矢量渲染
    include_svg = bool(source_svg and _looks_like_svg(source_svg))
    if include_svg:
        assets[SVG_FAVICON_FILENAME] = source_svg

    # 8) 配置文件与引用片段
    assets["browserconfig.xml"] = build_browserconfig(theme_color).encode("utf-8")
    if existing_manifest:
        # 用户既有 manifest：只合并 icons，其余字段原样保留
        assets["manifest.json"] = merge_manifest(existing_manifest).encode("utf-8")
    else:
        assets["manifest.json"] = build_manifest(
            name=name, theme_color=theme_color,
            background_color=DEFAULT_BACKGROUND_COLOR,
        ).encode("utf-8")
    assets["site-icon.html"] = build_html_snippet(include_svg=include_svg).encode("utf-8")

    # 9) 自说明清单：脱离生成环境后仍能读懂每个文件与所用参数
    assets[MANIFEST_FILENAME] = build_manifest_text(
        source_size=image.size, fit_mode=fit_mode, safe_zone=safe_zone,
        background=background, opaque_pad=opaque_pad, padding=padding,
        ico_sizes=ico_sizes, include_maskable=include_maskable,
        name=name, theme_color=theme_color,
    ).encode("utf-8")

    return assets


def build_site_assets_zip(
    image: Image.Image,
    *,
    ico_sizes: Sequence[Tuple[int, int]] = FAVICON_SIZES,
    fit_mode: str = "cover",
    include_maskable: bool = True,
    name: str = "My App",
    theme_color: str = DEFAULT_THEME_COLOR,
    safe_zone: float = MASKABLE_SAFE_ZONE,
    background: Tuple[int, int, int] = (255, 255, 255),
    opaque_pad: bool = False,
    padding: float = MIN_PADDING,
    source_svg: Optional[bytes] = None,
    existing_manifest: Optional[str] = None,
) -> bytes:
    """生成站点物料包并打包为 ZIP（内存内完成，不落临时文件）。"""
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    assets = build_site_assets(
        image,
        ico_sizes=ico_sizes,
        fit_mode=fit_mode,
        include_maskable=include_maskable,
        name=name,
        theme_color=theme_color,
        safe_zone=safe_zone,
        background=background,
        opaque_pad=opaque_pad,
        padding=padding,
        source_svg=source_svg,
        existing_manifest=existing_manifest,
    )

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for filename, data in sorted(assets.items()):
            archive.writestr(filename, data)
    return buffer.getvalue()


def describe_site_assets() -> List[Dict[str, str]]:
    """返回物料清单说明，供界面展示（避免在前端硬编码文案）。"""
    return [
        {"file": "favicon.ico", "purpose": "浏览器标签、Windows 快捷方式（多尺寸）"},
        {"file": "favicon.svg", "purpose": "矢量 favicon（源图为 SVG 时附带，现代浏览器优先使用）"},
        {"file": "favicon-16x16.png / favicon-32x32.png", "purpose": "浏览器标签独立引用"},
        {"file": "apple-touch-icon.png", "purpose": f"iOS 主屏图标（{APPLE_TOUCH_SIZE}）"},
        {"file": "android-chrome-192x192.png", "purpose": "Android 自适应图标"},
        {"file": "android-chrome-512x512.png", "purpose": "PWA 安装 / 启动画面"},
        {"file": "android-chrome-512x512-maskable.png", "purpose": "PWA 安全区变体（可被裁成任意形状）"},
        {"file": "mstile-150x150.png", "purpose": "Windows 磁贴"},
        {"file": "browserconfig.xml", "purpose": "Windows 磁贴配置"},
        {"file": "manifest.json", "purpose": "PWA Web App Manifest"},
        {"file": "site-icon.html", "purpose": "可直接粘贴的 HTML 引用片段"},
        {"file": "MANIFEST.txt", "purpose": "本包的自说明清单（文件用途 + 生成参数）"},
    ]


def estimate_ico_cost(size_bytes: int, sizes: Sequence[Tuple[int, int]]) -> str:
    """生成体积提示文案，让用户理解「多选尺寸」的代价。

    调研实测：16/32/48 约 2.4KB，而 16/32/48/64/128/256 约 8.9KB——
    后者多出的 64 与 128 几乎不产生实际收益，却让体积接近 4 倍。
    """
    if len(sizes) <= 3:
        return "精简尺寸，体积友好"
    if len(sizes) <= 4:
        return f"中等体积，当前 {size_bytes:,} 字节"
    return (
        f"当前 {size_bytes:,} 字节；其中 64/128 尺寸极少被浏览器选中，"
        "如只用于网站可考虑仅保留 16/32/48"
    )
