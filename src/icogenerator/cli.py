"""统一命令行入口。

用法::

    icogen desktop              启动桌面版 GUI
    icogen serve                启动本地后台服务 + 管理控制台
    icogen convert IN OUT       命令行批量转换（适合服务器 / 脚本）
    icogen info                 显示当前配置与输出目录
    icogen --version
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_config, resolve_output_dir
from .core import (
    BITMAP_FORMATS,
    ICO_SIZES,
    MASKABLE_SAFE_ZONE,
    MAX_PADDING,
    MAX_SAFE_ZONE,
    MIN_PADDING,
    MIN_SAFE_ZONE,
    PRESET_SIZE_SETS,
    SUPPORTED_INPUT_FORMATS,
    build_site_assets_zip,
    build_ico,
    estimate_ico_cost,
    png_bytes,
    quality_hints,
    read_ico_sizes,
)
from .core.errors import IcogenError
from .core.imaging import open_image_file
from .storage import LocalStore

LOGGER = logging.getLogger("icogenerator.cli")

# Windows 控制台默认 GBK，直接打印 ✓ / ✗ / 中文会抛 UnicodeEncodeError。
# 这里把标准输出切到「有损可显示」的 UTF-8 包装，保证 CLI 在任何区域设置下都能跑。
def _make_console_safe() -> None:
    """让 stdout/stderr 在 GBK 控制台下也能输出中文与符号。

    每次输出前调用，因此必须**幂等**：重复包装同一个 buffer 会让前一层
    wrapper 被回收并连带关闭底层流，表现为后续 ``I/O operation on closed file``。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or getattr(stream, "_icogen_wrapped", False):
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        # 已是能表示这些字符的编码就不要再包一层
        if encoding in ("utf8", "utf16", "utf32", "cp65001"):
            continue
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            # 没有 buffer（测试替身 / 内存流）：交给 _print 的 replace 兜底
            continue
        try:
            wrapper = io.TextIOWrapper(
                buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
            wrapper._icogen_wrapped = True  # noqa: SLF001 - 标记避免重复包装
            setattr(sys, stream_name, wrapper)
        except (ValueError, OSError, AttributeError):
            pass


def _print(text: str = "", *, err: bool = False) -> None:
    """安全打印：任何编码环境下都不会抛 UnicodeEncodeError。

    ``err=True`` 输出到 stderr。这里刻意不收 ``file=`` 流对象——调用方在
    ``_make_console_safe()`` 替换 ``sys.stderr`` 之前拿到的旧对象会被一起失效，
    曾导致错误分支本身抛 ``TypeError`` 而不是打印错误。改为在函数内部
    （包装之后）再解析目标流。
    """
    _make_console_safe()
    stream = sys.stderr if err else sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=stream)
    except ValueError:
        # 流已被调用方关闭：静默跳过，不能让日志输出破坏主流程
        pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)


def _parse_sizes(text: str) -> list[tuple[int, int]]:
    """解析 ``16,32,48`` 或 ``16x16,32x32`` 形式的尺寸串。"""
    sizes: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip().lower().replace("×", "x")
        if not part:
            continue
        if "x" in part:
            left, _, right = part.partition("x")
            sizes.append((int(left), int(right)))
        else:
            side = int(part)
            sizes.append((side, side))
    if not sizes:
        raise IcogenError("未指定任何尺寸")
    return sizes


def _parse_color(text: str) -> tuple[int, int, int]:
    """解析 ``#rrggbb`` / ``rrggbb`` 颜色。

    非法颜色在生成前就失败，而不是等产出不可用的结果（docs/09 要点 6.2）。
    """
    value = (text or "").strip().lstrip("#")
    if len(value) != 6:
        raise IcogenError(f"颜色必须是 6 位十六进制，如 #ffffff：{text}")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise IcogenError(f"不是合法的十六进制颜色：{text}") from exc


def _parse_safe_zone(text: str) -> float:
    """解析安全区比例并做范围校验。"""
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise IcogenError(f"安全区比例必须是数字：{text}") from exc
    if not (MIN_SAFE_ZONE <= value <= MAX_SAFE_ZONE):
        raise IcogenError(
            f"安全区比例必须在 {MIN_SAFE_ZONE} 至 {MAX_SAFE_ZONE} 之间：{text}"
        )
    return value


def _parse_padding(text: str) -> float:
    """解析通用留白比例并做范围校验（借鉴要点 A1）。"""
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise IcogenError(f"留白比例必须是数字：{text}") from exc
    if not (MIN_PADDING <= value <= MAX_PADDING):
        raise IcogenError(
            f"留白比例必须在 {MIN_PADDING} 至 {MAX_PADDING} 之间：{text}"
        )
    return value


def cmd_convert(args: argparse.Namespace) -> int:
    """命令行转换：支持单文件与目录批量。"""
    config = load_config(args.config)
    output_root = resolve_output_dir(config, args.output_dir)
    store = LocalStore(output_root)
    sizes = _parse_sizes(args.sizes)
    background = _parse_color(args.background)
    safe_zone = _parse_safe_zone(args.safe_zone)
    fit_mode = args.fit
    padding = _parse_padding(args.padding)

    png_sizes = None
    if args.png_sizes:
        png_sizes = _parse_sizes(args.png_sizes)
        for width, height in png_sizes:
            if not (1 <= width <= 1024 and 1 <= height <= 1024):
                raise IcogenError(
                    f"PNG 尺寸必须在 1-1024 之间：{width}x{height}"
                )

    source = Path(args.input)
    if not source.exists():
        _print(f"错误：路径不存在 {source}", err=True)
        return 1

    if source.is_dir():
        # 目录输入只处理支持的图片格式：递归扫描项目目录时不再把无关文件
        # 逐个报错，而是直接跳过（同类批量转换均按格式过滤）
        candidates = source.rglob("*") if args.recursive else source.iterdir()
        files = sorted(
            p for p in candidates
            if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_FORMATS
        )
    else:
        files = [source]
    if not files:
        _print(f"错误：{source} 下没有可处理的图片文件", err=True)
        return 1

    failures = 0
    total_bytes = 0

    if args.dry_run:
        _print("预演模式：只生成到内存并报告，不写盘。")
        _print(f"  适配策略 {fit_mode}　安全区 {safe_zone:.2f}　留白比例 {padding:.2f}　"
              f"颜色 #{background[0]:02x}{background[1]:02x}{background[2]:02x}"
              f"（不透明：{'是' if args.pad_opaque else '否'}）")
        if png_sizes:
            _print("  将额外输出独立 PNG："
                   + ", ".join(f"{w}x{h}" for w, h in png_sizes))

    # --site-assets 所需的一次性参数（与 ICO 共用同一套适配参数）
    merge_manifest_text = None
    if args.site_assets and args.merge_manifest:
        merge_path = Path(args.merge_manifest)
        if not merge_path.is_file():
            _print(f"错误：--merge-manifest 文件不存在 {merge_path}", err=True)
            return 1
        merge_manifest_text = merge_path.read_text(encoding="utf-8")

    for path in files:
        try:
            image = open_image_file(str(path))
            data = build_ico(
                image, sizes, fit_mode=fit_mode, safe_zone=safe_zone,
                background=background, opaque_pad=args.pad_opaque,
                padding=padding, bitmap_format=args.bitmap_format,
            )
            produced = read_ico_sizes(data)

            if args.dry_run:
                # 体积提示与警告一起给出，用户在写盘前就能判断值不值得生成
                hints = quality_hints(image, sizes, data)
                _print(f"· {path.name} -> {path.stem}.ico "
                      f"({len(data):,} 字节, {len(produced)} 个尺寸)"
                      f"　{estimate_ico_cost(len(data), sizes)}")
                for hint in hints:
                    if hint.level in ("warning", "error"):
                        _print(f"    [{hint.level}] {hint.message}")
                total_bytes += len(data)
                continue

            record = store.save_bytes(
                data,
                filename=f"{path.stem}.ico",
                source_name=path.name,
                fit_mode=fit_mode,
                sizes=[f"{w}x{h}" for w, h in sizes],
            )
            total_bytes += record.size
            _print(f"✓ {path.name} -> {record.filename} "
                  f"({record.size:,} 字节, {len(produced)} 个尺寸)")

            if png_sizes:
                # 独立 PNG 输出（借鉴要点：同类普遍提供 png 尺寸集）
                for width, height in png_sizes:
                    png_data = png_bytes(
                        image, (width, height), fit_mode,
                        safe_zone=safe_zone, background=background,
                        opaque_pad=args.pad_opaque, padding=padding,
                    )
                    png_record = store.save_bytes(
                        png_data,
                        filename=f"{path.stem}-{width}x{height}.png",
                        source_name=path.name,
                        fit_mode=fit_mode,
                        sizes=[f"{width}x{height}"],
                    )
                    total_bytes += png_record.size
                    _print(f"  ↳ {png_record.filename} ({png_record.size:,} 字节)")

            if args.site_assets:
                # 站点物料包与 ICO 共用同一套参数（借鉴要点 B1：CLI-first）
                source_svg = path.read_bytes() if path.suffix.lower() == ".svg" else None
                zip_data = build_site_assets_zip(
                    image,
                    ico_sizes=sizes,
                    fit_mode=fit_mode,
                    include_maskable=not args.no_maskable,
                    name=args.app_name,
                    theme_color=args.theme_color,
                    safe_zone=safe_zone,
                    background=background,
                    opaque_pad=args.pad_opaque,
                    padding=padding,
                    source_svg=source_svg,
                    existing_manifest=merge_manifest_text,
                )
                bundle_record = store.save_bundle(
                    zip_data, base_name=f"{path.stem}-site-assets"
                )
                total_bytes += bundle_record.size
                _print(f"  ↳ 站点物料包 {bundle_record.filename} "
                      f"({bundle_record.size:,} 字节)")
        except IcogenError as exc:
            failures += 1
            _print(f"✗ {path.name}: {exc}", err=True)
        except OSError as exc:
            failures += 1
            _print(f"✗ {path.name}: {exc}", err=True)

    if args.dry_run:
        _print(f"\n预演完成：{len(files) - failures} / {len(files)} 个文件可生成，"
              f"预计合计 {total_bytes:,} 字节（未写盘）")
        return 1 if failures == len(files) else 0

    _print(f"\n完成：成功 {len(files) - failures} / 共 {len(files)}，"
          f"输出目录 {output_root}")
    return 1 if failures == len(files) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    """启动后台服务。"""
    from .server import serve_forever

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    output_root = resolve_output_dir(config)
    store = LocalStore(output_root)
    serve_forever(config, store)
    return 0


def cmd_desktop(_args: argparse.Namespace) -> int:
    """启动桌面版。"""
    from .desktop import main as desktop_main

    return desktop_main()


def cmd_info(args: argparse.Namespace) -> int:
    """打印当前配置（凭据脱敏）。"""
    config = load_config(args.config)
    output_root = resolve_output_dir(config)

    _print(f"IcoGenerator v{__version__}")
    _print(f"配置文件      : {args.config or '(默认路径)'}")
    _print(f"输出目录      : {output_root}")
    _print(f"服务地址      : http://{config.host}:{config.port}")
    _print(f"对象存储      : {'已启用' if config.storage.enabled else '未启用'}")
    _print(f"定时上传      : {'已启用' if config.scheduler.enabled else '未启用'}")
    _print("\n可用尺寸预设：")
    for key, (label, sizes) in PRESET_SIZE_SETS.items():
        _print(f"  {key:<10} {label} -> {','.join(str(s[0]) for s in sizes)}")
    _print(f"  全尺寸      {','.join(str(s[0]) for s in ICO_SIZES)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """运行前自检（docs/09 要点 5.2）。

    借鉴 alonw0/web-asset-generator 的 check_dependencies.py：把「环境缺什么」
    在生成之前说清楚，而不是等用户点了生成才看到一句看不懂的 ImportError。
    """
    import importlib.util

    config = load_config(args.config)
    output_root = resolve_output_dir(config)
    problems = 0

    _print(f"IcoGenerator v{__version__} 环境自检")
    _print("=" * 46)

    # 1) Pillow：唯一必需运行时依赖
    try:
        from PIL import __version__ as pillow_version

        _print(f"[正常] Pillow {pillow_version}")
    except Exception as exc:  # noqa: BLE001
        problems += 1
        _print(f"[错误] Pillow 不可用：{exc}")

    # 2) 输出目录可写
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".icogen-doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _print(f"[正常] 输出目录可写：{output_root}")
    except OSError as exc:
        problems += 1
        _print(f"[错误] 输出目录不可写：{output_root}（{exc}）")

    # 3) 可选依赖：装了增强，没装降级，两者都算正常
    optional = (
        ("tkinterdnd2", "桌面版拖拽"),
        ("ttkthemes", "桌面版现代主题"),
        ("cairosvg", "SVG 输入（还需系统 Cairo）"),
    )
    for module, purpose in optional:
        if importlib.util.find_spec(module) is not None:
            _print(f"[正常] 可选依赖 {module} 已安装（{purpose}）")
        else:
            _print(f"[提示] 可选依赖 {module} 未安装，{purpose}不可用（不影响核心功能）")

    # 4) 尺寸配置合法性
    try:
        sizes = _parse_sizes(args.sizes)
        from .core import validate_sizes

        validate_sizes(sizes)
        _print(f"[正常] 尺寸配置合法：{','.join(f'{w}x{h}' for w, h in sizes)}")
    except IcogenError as exc:
        problems += 1
        _print(f"[错误] 尺寸配置非法：{exc}")

    # 5) 支持的输入格式
    _print(f"[提示] 支持输入：{' / '.join(SUPPORTED_INPUT_FORMATS)}")

    _print("=" * 46)
    if problems:
        _print(f"发现 {problems} 个需要处理的问题。")
        return 1
    _print("未发现阻断性问题。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icogen",
        description=f"IcoGenerator —— 多尺寸 ICO 图标生成器 v{__version__}",
    )
    parser.add_argument("--version", action="version", version=f"icogen {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--config", type=Path, default=None, help="指定配置文件路径")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("desktop", help="启动桌面版 GUI").set_defaults(func=cmd_desktop)

    p_serve = sub.add_parser("serve", help="启动本地后台服务 + 管理控制台")
    p_serve.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    p_serve.set_defaults(func=cmd_serve)

    p_convert = sub.add_parser("convert", help="命令行批量转换")
    p_convert.add_argument("input", help="输入文件或目录")
    p_convert.add_argument("-o", "--output-dir", default=None, help="输出目录")
    p_convert.add_argument("-s", "--sizes", default="16,32,48,256",
                           help="尺寸列表，如 16,32,48 或 16x16,32x32")
    p_convert.add_argument("--fit", choices=("cover", "contain", "maskable", "stretch"),
                           default="cover", help="非正方形图片的适配方式")
    p_convert.add_argument("--safe-zone", default=str(MASKABLE_SAFE_ZONE),
                           help=f"maskable 模式安全区比例（{MIN_SAFE_ZONE}-{MAX_SAFE_ZONE}）")
    p_convert.add_argument("--padding", default=str(MIN_PADDING),
                           help=f"内容留白比例，0 即铺满（{MIN_PADDING}-{MAX_PADDING}，如 0.1）")
    p_convert.add_argument("--background", default="#ffffff",
                           help="留白填充色，6 位十六进制，如 #ffffff")
    p_convert.add_argument("--pad-opaque", action="store_true",
                           help="留白使用不透明背景色（默认透明留边）")
    p_convert.add_argument("--recursive", action="store_true",
                           help="目录输入时递归扫描子目录（仅处理支持的图片格式）")
    p_convert.add_argument("--png-sizes", default=None,
                           help="额外输出独立 PNG，尺寸逗号分隔，如 16,32,256（1-1024）")
    p_convert.add_argument("--bitmap-format", choices=BITMAP_FORMATS, default="bmp",
                           help="ICO 帧编码格式：bmp（默认/最大兼容）或 png（体积更小，"
                                "Windows XP 等旧系统不识别）")
    p_convert.add_argument("--site-assets", action="store_true",
                           help="同时输出完整站点物料包 ZIP（favicon/manifest/磁贴等）")
    p_convert.add_argument("--no-maskable", action="store_true",
                           help="站点物料包不包含 maskable 安全区变体")
    p_convert.add_argument("--app-name", default="My App",
                           help="站点物料包 manifest 中的应用名")
    p_convert.add_argument("--theme-color", default="#ffffff",
                           help="站点物料包主题色，6 位十六进制")
    p_convert.add_argument("--merge-manifest", default=None,
                           help="既有 manifest.json 路径：只合并本项目 icons 条目，其余字段保留")
    p_convert.add_argument("--dry-run", action="store_true",
                           help="预演：生成到内存并报告体积与警告，不写盘")
    p_convert.set_defaults(func=cmd_convert)

    sub.add_parser("info", help="显示当前配置").set_defaults(func=cmd_info)

    p_doctor = sub.add_parser("doctor", help="运行前环境自检")
    p_doctor.add_argument("-s", "--sizes", default="16,32,48,256",
                          help="一并校验的尺寸列表")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except IcogenError as exc:
        _print(f"错误：{exc}", err=True)
        return 1
    except KeyboardInterrupt:
        _print("\n已取消", err=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())