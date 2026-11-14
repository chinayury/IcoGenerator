"""质量提示分级。

借鉴来源（docs/09 要点 5.1）：alonw0/web-asset-generator 把每次校验封装为
带级别的结果（success / warning / error / info），并对「接近上限」的情况提前预警
（文件大小超过上限的 80% 即给 warning）。本模块把同一思路落到 IcoGenerator：

- 源图判断（是否放大、是否不透明、是否逼近解码上限）与体积判断统一输出带级别提示；
- 界面层只负责按级别展示，不再各自拼接文案；
- 机器可读的 ``code`` 字段供测试与前端分流，避免靠匹配中文文案断言。

所有函数均为纯函数：不写磁盘、不依赖 GUI、不发起网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from PIL import Image

from .imaging import MAX_SOURCE_EDGE, MAX_TOTAL_PIXELS, analyze_source

# 提示级别。与 alonw0 的四级保持一致，前端可据此决定颜色与图标。
LEVEL_SUCCESS = "success"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVEL_INFO = "info"

# ICO 体积软预算。docs/08 实测：16/32/48 约 2.4KB，全尺寸集约 8.9KB。
# 这不是硬限制，只用于触发提示——favicon 场景下超过 32KB 几乎总是选多了尺寸。
SOFT_ICO_BUDGET_BYTES = 32 * 1024

# 提前预警线：达到上限的 80% 即提示，而不是等真的超限才说。
NEAR_LIMIT_RATIO = 0.8

# 极少被浏览器或操作系统选中的尺寸（docs/08 要点 3，多份资料交叉验证一致）。
LOW_VALUE_SIZES = ("64", "128")


@dataclass(frozen=True)
class QualityHint:
    """一条带级别的质量提示。

    Attributes:
        level: ``success`` / ``warning`` / ``error`` / ``info``。
        code: 稳定的机器可读标识，供测试与前端分流，不随文案改动而失效。
        message: 面向用户的说明文案。
    """

    level: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        """转为可 JSON 序列化的字典（服务端响应用）。"""
        return {"level": self.level, "code": self.code, "message": self.message}


def source_hints(
    image: Image.Image,
    target_sizes: Sequence[Tuple[int, int]] | None = None,
) -> List[QualityHint]:
    """基于源图本身给出提示。

    覆盖三类事实：逼近解码上限、需要放大、必然不透明。
    三者都不阻断生成——用户可能就是要一个小图标，阻断会让工具变难用。
    """
    info = analyze_source(image, target_sizes)
    width, height = info["width"], info["height"]
    max_side = info["max_side"]
    hints: List[QualityHint] = []

    # 1) 逼近单边上限：再大一点就会被 decode_image 拒绝，提前说明
    if max_side >= MAX_SOURCE_EDGE * NEAR_LIMIT_RATIO:
        hints.append(QualityHint(
            LEVEL_WARNING,
            "source_near_edge_limit",
            f"源图单边 {max_side}px 已接近上限 {MAX_SOURCE_EDGE}px，"
            f"再大的图将被拒绝解码，建议预先缩小。",
        ))

    # 2) 逼近总像素上限
    total_pixels = width * height
    if total_pixels >= MAX_TOTAL_PIXELS * NEAR_LIMIT_RATIO:
        hints.append(QualityHint(
            LEVEL_WARNING,
            "source_near_pixel_limit",
            f"源图总像素 {total_pixels:,} 已接近安全上限 "
            f"{int(MAX_TOTAL_PIXELS * NEAR_LIMIT_RATIO):,}，解码可能被拒绝。",
        ))

    # 3) 需要放大：docs/08 实测 16px 源图生成的 256 层比 256px 源图大 20 倍却更模糊
    if info["will_upscale"]:
        targets = "、".join(info["upscaled_targets"])
        hints.append(QualityHint(
            LEVEL_WARNING,
            "upscale",
            f"源图 {width}×{height} 小于目标尺寸（{targets}），"
            f"放大后将不清晰，建议使用更大的源图。",
        ))

    # 4) 必然不透明：JPEG 没有 alpha 通道
    if info["opaque_source"]:
        hints.append(QualityHint(
            LEVEL_WARNING,
            "opaque_source",
            "源图不含透明通道（JPEG 等格式不支持），生成的图标将是方形不透明背景；"
            "如需透明请使用 PNG。",
        ))

    if not hints:
        alpha = "有" if info["has_alpha"] else "无"
        hints.append(QualityHint(
            LEVEL_SUCCESS,
            "source_ok",
            f"源图 {width}×{height}，透明通道{alpha}，无需放大。",
        ))
    return hints


def cost_hints(size_bytes: int, sizes: Sequence[Tuple[int, int]]) -> List[QualityHint]:
    """基于生成结果体积给出提示。

    两条判据：尺寸数量偏多（64/128 收益极低）、体积超过或逼近软预算。
    """
    hints: List[QualityHint] = []
    count = len(sizes)

    if count > 4:
        low_value = "、".join(f"{s}×{s}" for s in LOW_VALUE_SIZES)
        hints.append(QualityHint(
            LEVEL_WARNING,
            "ico_many_sizes",
            f"当前 {size_bytes:,} 字节、{count} 个尺寸；其中 {low_value} "
            f"极少被浏览器选中，如只用于网站可考虑仅保留 16/32/48。",
        ))

    if size_bytes >= SOFT_ICO_BUDGET_BYTES:
        hints.append(QualityHint(
            LEVEL_WARNING,
            "ico_over_budget",
            f"当前 {size_bytes:,} 字节，已超过 favicon 场景的软预算 "
            f"{SOFT_ICO_BUDGET_BYTES // 1024}KB，请确认是否真的需要这些尺寸；"
            "如需压缩体积可用 PNG 帧编码（--bitmap-format png，"
            "Windows XP 等旧系统不识别）。",
        ))
    elif size_bytes >= SOFT_ICO_BUDGET_BYTES * NEAR_LIMIT_RATIO:
        hints.append(QualityHint(
            LEVEL_INFO,
            "ico_near_budget",
            f"当前 {size_bytes:,} 字节，已接近软预算 "
            f"{SOFT_ICO_BUDGET_BYTES // 1024}KB。",
        ))

    if not hints:
        hints.append(QualityHint(
            LEVEL_SUCCESS,
            "ico_size_ok",
            f"体积友好，当前 {size_bytes:,} 字节。",
        ))
    return hints


def quality_hints(
    image: Image.Image,
    target_sizes: Sequence[Tuple[int, int]],
    ico_bytes: bytes | None = None,
) -> List[QualityHint]:
    """汇总源图与体积两类提示，供界面层一次性展示。"""
    hints = source_hints(image, target_sizes)
    if ico_bytes is not None:
        hints.extend(cost_hints(len(ico_bytes), target_sizes))
    return hints


def hints_to_dicts(hints: Sequence[QualityHint]) -> List[Dict[str, str]]:
    """批量转字典，避免调用方各自写循环。"""
    return [hint.to_dict() for hint in hints]


def worst_level(hints: Sequence[QualityHint]) -> str:
    """返回提示中的最高级别，供界面决定整体配色。

    没有提示时返回 ``success``——调用方不应把「无提示」当成「有问题」。
    """
    for level in (LEVEL_ERROR, LEVEL_WARNING, LEVEL_INFO):
        if any(hint.level == level for hint in hints):
            return level
    return LEVEL_SUCCESS


def format_hints(hints: Sequence[QualityHint], *, prefix: str = "") -> str:
    """把提示拼成多行文本（桌面版状态区直接使用）。

    命令行与 GBK 控制台场景请改用 ``hints_to_dicts`` 或自行遍历，
    由调用方的安全打印函数处理编码。
    """
    if not hints:
        return ""
    marks = {
        LEVEL_ERROR: "[错误]",
        LEVEL_WARNING: "[注意]",
        LEVEL_INFO: "[提示]",
        LEVEL_SUCCESS: "[正常]",
    }
    lines = []
    for hint in hints:
        mark = marks.get(hint.level, "[提示]")
        lines.append(f"{prefix}{mark} {hint.message}")
    return "\n".join(lines)


__all__ = [
    "LEVEL_SUCCESS",
    "LEVEL_WARNING",
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "SOFT_ICO_BUDGET_BYTES",
    "NEAR_LIMIT_RATIO",
    "QualityHint",
    "source_hints",
    "cost_hints",
    "quality_hints",
    "hints_to_dicts",
    "worst_level",
    "format_hints",
]
