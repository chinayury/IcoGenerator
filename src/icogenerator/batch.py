"""批量转换引擎（供桌面版批量入口与后续 CLI 重构共用）。

设计要点（借鉴 duma520/Image_To_Icon_Converter 的 ConversionThread 模式）：
- 逐文件转换，**失败隔离**：单个文件失败不影响其余文件，错误随结果返回；
- **进度回调**：调用方（桌面版后台线程）通过回调刷新界面，引擎自身零 GUI 依赖；
- 所有产物经 ``LocalStore`` 登记，保持「本地 store 是真相源」的既定约定；
- 生成参数与 CLI ``convert`` 完全一致（同一套 core 函数），保证各入口产物一致。

本模块为纯逻辑：不创建线程、不碰任何 GUI 对象——线程与界面由调用方负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .core.assets import SITE_ASSET_SETS, build_site_assets_zip
from .core.errors import IcogenError
from .core.icons import BITMAP_FORMATS, build_ico, read_ico_sizes, validate_sizes
from .storage import LocalStore
from .core import png_bytes

# 允许的图片输入扩展名（与 imaging.SUPPORTED_INPUT_FORMATS 保持一致来源）
from .core.imaging import SUPPORTED_INPUT_FORMATS

ProgressCallback = Callable[[str, Dict[str, object]], None]


def collect_image_files(root: Path, *, recursive: bool = False) -> List[Path]:
    """收集目录下受支持的图片文件（与 CLI convert 的扫描规则一致）。

    目录输入一律按扩展名过滤，无关文件直接跳过而非逐个报错；
    结果按路径排序保证确定性。
    """
    if root.is_file():
        return [root]
    candidates = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        p for p in candidates
        if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_FORMATS
    )


@dataclass
class BatchOptions:
    """一次批量转换的全部生成参数（与 CLI convert 参数一一对应）。"""

    sizes: List[Tuple[int, int]] = field(default_factory=lambda: [(16, 16), (32, 32), (48, 48)])
    fit_mode: str = "cover"
    safe_zone: float = 0.8
    background: Tuple[int, int, int] = (255, 255, 255)
    opaque_pad: bool = False
    padding: float = 0.0
    bitmap_format: str = "bmp"
    png_sizes: Optional[List[Tuple[int, int]]] = None
    site_assets: bool = False
    asset_set: str = "full"
    app_name: str = "My App"
    theme_color: str = "#ffffff"
    merge_manifest_text: Optional[str] = None
    dry_run: bool = False


@dataclass
class BatchFileResult:
    """单个文件的处理结果。"""

    path: Path
    ok: bool = False
    filename: str = ""
    size: int = 0
    extra: List[str] = field(default_factory=list)  # 附加产物（PNG / 物料包）描述
    error: str = ""


@dataclass
class BatchResult:
    """整轮批量转换汇总。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    total_bytes: int = 0
    dry_run: bool = False
    files: List[BatchFileResult] = field(default_factory=list)


def convert_batch(
    store: LocalStore,
    files: Sequence[Path],
    options: BatchOptions,
    *,
    progress: Optional[ProgressCallback] = None,
) -> BatchResult:
    """逐文件执行转换并登记产物；失败隔离，汇总返回。

    Args:
        store: 本地制品仓库（dry_run 时仅用于读取根目录信息，不写入）。
        files: 待处理的图片路径列表（由 ``collect_image_files`` 收集）。
        options: 生成参数。
        progress: 可选进度回调，事件名：
            ``start``（files 数）、``file``（单文件结果）、``done``（汇总）。

    Returns:
        批量汇总结果。
    """
    def emit(event: str, payload: Dict[str, object]) -> None:
        if progress is not None:
            progress(event, payload)

    # 参数前置校验：非法值在此处统一转为 IcogenError，交由失败隔离逻辑呈现
    if options.bitmap_format not in BITMAP_FORMATS:
        raise IcogenError(f"未知的 ICO 帧格式：{options.bitmap_format}")
    if options.asset_set not in SITE_ASSET_SETS:
        raise IcogenError(f"未知的站点物料包档位：{options.asset_set}")
    validate_sizes(options.sizes)
    for width, height in options.png_sizes or ():
        if not (1 <= width <= 1024 and 1 <= height <= 1024):
            raise IcogenError(f"PNG 尺寸必须在 1-1024 之间：{width}x{height}")

    result = BatchResult(total=len(files), dry_run=options.dry_run)
    emit("start", {"total": len(files), "dry_run": options.dry_run})

    for path in files:
        item = BatchFileResult(path=path)
        try:
            from .core.imaging import open_image_file

            image = open_image_file(str(path))
            data = build_ico(
                image, options.sizes, fit_mode=options.fit_mode,
                safe_zone=options.safe_zone, background=options.background,
                opaque_pad=options.opaque_pad, padding=options.padding,
                bitmap_format=options.bitmap_format,
            )
            produced = read_ico_sizes(data)
            item.filename = f"{path.stem}.ico"
            item.size = len(data)

            if options.dry_run:
                item.ok = True
                item.extra.append(
                    f"预演 {item.size:,} 字节, {len(produced)} 个尺寸"
                )
            else:
                record = store.save_bytes(
                    data,
                    filename=item.filename,
                    source_name=path.name,
                    fit_mode=options.fit_mode,
                    sizes=[f"{w}x{h}" for w, h in options.sizes],
                )
                item.filename = record.filename
                item.size = record.size
                item.ok = True

            if options.png_sizes:
                for width, height in options.png_sizes:
                    png_data = png_bytes(
                        image, (width, height), options.fit_mode,
                        safe_zone=options.safe_zone, background=options.background,
                        opaque_pad=options.opaque_pad, padding=options.padding,
                    )
                    if options.dry_run:
                        item.extra.append(f"PNG {width}x{height} 预演 {len(png_data):,} 字节")
                    else:
                        png_record = store.save_bytes(
                            png_data,
                            filename=f"{path.stem}-{width}x{height}.png",
                            source_name=path.name,
                            fit_mode=options.fit_mode,
                            sizes=[f"{width}x{height}"],
                        )
                        item.extra.append(
                            f"{png_record.filename} ({png_record.size:,} 字节)"
                        )

            if options.site_assets and not options.dry_run:
                source_svg = path.read_bytes() if path.suffix.lower() == ".svg" else None
                zip_data = build_site_assets_zip(
                    image,
                    ico_sizes=options.sizes,
                    fit_mode=options.fit_mode,
                    include_maskable=options.asset_set != "minimal",
                    name=options.app_name,
                    theme_color=options.theme_color,
                    safe_zone=options.safe_zone,
                    background=options.background,
                    opaque_pad=options.opaque_pad,
                    padding=options.padding,
                    source_svg=source_svg,
                    existing_manifest=options.merge_manifest_text,
                    asset_set=options.asset_set,
                )
                bundle_record = store.save_bundle(
                    zip_data, base_name=f"{path.stem}-site-assets"
                )
                item.extra.append(
                    f"站点物料包 {bundle_record.filename} ({bundle_record.size:,} 字节)"
                )

            if options.dry_run:
                result.total_bytes += item.size
            else:
                result.total_bytes += item.size
        except IcogenError as exc:
            item.ok = False
            item.error = str(exc)
        except OSError as exc:
            item.ok = False
            item.error = str(exc)

        if item.ok:
            result.success += 1
        else:
            result.failed += 1
        result.files.append(item)
        emit("file", {
            "path": str(path),
            "ok": item.ok,
            "filename": item.filename,
            "size": item.size,
            "error": item.error,
            "extra": list(item.extra),
        })

    emit("done", {
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
        "total_bytes": result.total_bytes,
        "dry_run": result.dry_run,
    })
    return result


__all__ = [
    "BatchOptions",
    "BatchFileResult",
    "BatchResult",
    "collect_image_files",
    "convert_batch",
    "ProgressCallback",
]
