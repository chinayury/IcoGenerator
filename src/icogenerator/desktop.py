"""桌面版 ICO 图标生成器（Tkinter GUI）。

相对旧版的改动（详见 docs/03-BUG修复清单.md）：

1. **多尺寸合并 ICO 不再崩溃** —— 旧版用 ``save_all=True``，Pillow 10+ 抛
   ``KeyError: 'ICO'``；现统一走核心引擎的 ``build_ico``。
2. **拖拽含空格路径可用** —— 旧版用 ``shlex.split(path)[0]``，Windows 路径
   ``C:\\Users\\me\\My Pictures\\a.png`` 会被截成 ``C:UsersmeMy``；
   现改用 Tk 自带的 ``tk.splitlist``（同时正确处理多文件拖入）。
3. **CMYK / P 模式 / 动图不再崩溃** —— 全部交给核心引擎统一规范化。
4. **非正方形图不再变形** —— 提供 cover / contain / stretch 三种适配策略。
5. **不再往 CWD 写临时文件** —— 预览图走内存（``ImageTk.PhotoImage`` 直接吃
   BytesIO），彻底消除 ``temp_preview.png`` 污染工作目录与并发竞争。
6. **不再静默上传用户图片** —— 旧版每次选图都自动 POST 到第三方图床，
   且 uid/token 硬编码；现上传由管理员在后台显式配置与触发。
7. **tkinterdnd2 变为可选** —— 未安装时自动降级为「点击选择文件」，
   而不是 import 就崩溃。
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# 保证以源码方式直接运行 python -m icogenerator.desktop 时能导入核心包
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageTk

from . import __version__ as VERSION
from .config import AppConfig, load_config, resolve_output_dir, save_config
from .core import (
    ICO_SIZES,
    MASKABLE_SAFE_ZONE,
    MAX_PADDING,
    MAX_SAFE_ZONE,
    MIN_PADDING,
    MIN_SAFE_ZONE,
    PRESET_SIZE_SETS,
    build_ico,
    build_ico_bundle,
    build_site_assets_zip,
    estimate_ico_cost,
    format_hints,
    make_png_preview,
    quality_hints,
    worst_level,
)
from .core.errors import IcogenError
from .core.imaging import normalize_to_square, open_image_file
from .storage import LocalStore

LOGGER = logging.getLogger(__name__)

# 支持的输入格式（决定文件对话框过滤器）
FILETYPES = [
    ("图片文件", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.tif *.tiff *.ico *.svg"),
    ("JPEG", "*.jpg *.jpeg"),
    ("PNG", "*.png"),
    ("GIF", "*.gif"),
    ("BMP", "*.bmp"),
    ("WebP", "*.webp"),
    ("所有文件", "*.*"),
]

FIT_MODES = (
    ("cover（居中裁切铺满，推荐 favicon）", "cover"),
    ("contain（完整保留并留边）", "contain"),
    ("maskable（PWA 安全区，内容缩到 80%）", "maskable"),
    ("stretch（强制拉伸，兼容旧版）", "stretch"),
)


def _setup_logging() -> None:
    """配置日志：控制台 + 滚动文件。"""
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "icongenerator"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        file_handler = logging.FileHandler(
            log_dir / "desktop.log", encoding="utf-8"
        )
        handlers.append(file_handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            handlers=handlers,
        )
    except OSError:
        # 日志文件不可写不应阻断程序启动
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("PIL").setLevel(logging.WARNING)


class IcoGeneratorApp:
    """主窗口。"""

    def __init__(self, root: tk.Tk, *, config: AppConfig | None = None):
        self.root = root
        self.config = config or load_config()
        self.output_dir = resolve_output_dir(self.config)
        self.store = LocalStore(self.output_dir)

        # 运行时状态
        self.image: Image.Image | None = None
        self.image_path: str = ""
        self.merged_ico: bytes | None = None
        self.bundle: dict[tuple[int, int], bytes] = {}
        self.artifact_id: str = ""
        self.size_vars: dict[tuple[int, int], tk.BooleanVar] = {}
        self._photo_refs: list[ImageTk.PhotoImage] = []  # 防止 GC 回收图片

        self._init_window()
        self._init_styles()
        self._build_ui()

    # ---------------- 窗口与样式 ----------------

    def _init_window(self) -> None:
        self.root.title(f"ICO 图标生成器 v{VERSION}")
        self.root.geometry("960x680")
        self.root.minsize(820, 600)
        self.root.configure(bg="#f4efe4")

        # 图标资源可选：缺失不应影响启动
        icon_path = Path(__file__).parent / "assets" / "app.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        self._enable_drag_and_drop()

        # 关闭窗口时保存界面偏好（借鉴 Elephicon 的状态持久化）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _init_styles(self) -> None:
        style = ttk.Style()

        # 优先使用 ttkthemes 的现代主题（arc/adapta），告别默认灰暗观感。
        # 属于可选增强：未安装则回退到 clam，功能完全不受影响。
        # 注意必须在 theme_use("clam") 之前调用，否则 clam 会覆盖。
        self._themed = False
        try:
            import ttkthemes

            # 按优先级尝试，挑第一个可用的
            for theme_name in ("arc", "adapta", "sun"):
                try:
                    ttkthemes.use(theme_name)
                    self._themed = True
                    LOGGER.info("已启用 ttkthemes 主题：%s", theme_name)
                    break
                except Exception:  # noqa: BLE001 - 该主题在当前平台不可用
                    continue
        except ImportError:
            LOGGER.info("未安装 ttkthemes，使用内置主题（pip install ttkthemes 可启用现代主题）")

        if not self._themed and "clam" in style.theme_names():
            style.theme_use("clam")

        # 设计体系「青瓷暖砂」：与网页端（client.css / style.css）共用同一套配色。
        style.configure("TFrame", background="#f4efe4")
        style.configure("Card.TLabelframe", background="#ffffff",
                        bordercolor="#e6dcc8", relief="solid", borderwidth=1)
        style.configure("Card.TLabelframe.Label", background="#f4efe4",
                        foreground="#173f36", font=("等线", 11, "bold"))
        style.configure("Title.TLabel", background="#f4efe4", foreground="#123f34",
                        font=("等线", 20, "bold"))
        style.configure("Sub.TLabel", background="#f4efe4", foreground="#8a8371",
                        font=("等线", 9))
        style.configure("Body.TLabel", background="#f4efe4", foreground="#4a463d",
                        font=("等线", 9))
        style.configure("Status.TLabel", background="#f4efe4", foreground="#123f34",
                        font=("等线", 9))
        # 提示与警告样式：把「源图过小 / JPEG 无透明」显式告知用户
        style.configure("Hint.TLabel", background="#f4efe4", foreground="#8a8371",
                        font=("等线", 9))
        style.configure("Warn.TLabel", background="#f4efe4", foreground="#a8630f",
                        font=("等线", 9))
        style.configure("Primary.TButton", background="#0e7d68", foreground="#ffffff",
                        font=("等线", 10, "bold"), padding=(14, 8))
        style.map("Primary.TButton",
                  background=[("active", "#0a6351"), ("disabled", "#b3ac9a")])
        style.configure("Ghost.TButton", background="#ffffff", foreground="#4a463d",
                        font=("等线", 9), padding=(10, 6))
        style.map("Ghost.TButton", background=[("active", "#efe8d6")])
        style.configure("Accent.TButton", background="#cf6b2e", foreground="#ffffff",
                        font=("等线", 10, "bold"), padding=(14, 9))
        style.map("Accent.TButton",
                  background=[("active", "#b25420"), ("disabled", "#b3ac9a")])

    def _enable_drag_and_drop(self) -> bool:
        """尝试启用拖拽；不可用时优雅降级。

        旧版在 ``import tkinterdnd2`` 处直接失败，导致缺少该包的用户
        完全无法使用。现改为可选增强。
        """
        try:
            # TkinterDnD 需在 Tk() 之前导入以注册 dnd 命令；此处仅作存在性探测，
            # 真正启用在下方 drop_target_register / dnd_bind。
            from tkinterdnd2 import DND_FILES  # noqa: F401
        except ImportError:
            LOGGER.info("未安装 tkinterdnd2，拖拽功能不可用（可点击选择文件）")
            return False

        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
            self._dnd_files = DND_FILES
            LOGGER.info("拖拽功能已启用")
            return True
        except (tk.TclError, AttributeError) as exc:
            LOGGER.warning("拖拽初始化失败，已降级：%s", exc)
            return False

    # ---------------- 界面构建 ----------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text="ICO 图标生成器",
                  style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(outer, text="一张源图，一次生成网站 favicon 与桌面应用所需的全套图标尺寸",
                  style="Sub.TLabel").pack(anchor=tk.W, pady=(2, 14))

        body = ttk.Frame(outer)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body, style="TFrame")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 12))
        right = ttk.Frame(body, style="TFrame")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(12, 0))

        self._build_source_card(left)
        self._build_size_card(left)
        self._build_preview_card(right)
        self._restore_preferences()
        self._refresh_size_hint()

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status_var,
                  style="Status.TLabel").pack(anchor=tk.W, pady=(12, 0))
        LOGGER.info("界面初始化完成，输出目录：%s", self.output_dir)

    def _build_source_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=" ① 导入源图 ", style="Card.TLabelframe",
                              padding=14)
        card.pack(fill=tk.X, pady=(0, 12))

        self.drop_area = tk.Label(
            card,
            text="把图片拖进这里\n或点击下方按钮选择",
            bg="#faf6ec", fg="#8a8371", relief="groove", bd=1,
            font=("等线", 10), height=4, cursor="hand2",
        )
        self.drop_area.pack(fill=tk.X)
        self.drop_area.bind("<Button-1>", lambda _e: self.pick_image())

        self.choose_btn = ttk.Button(card, text="选择图片文件",
                                     command=self.pick_image, style="Ghost.TButton")
        self.choose_btn.pack(pady=(10, 0))

        self.file_label = ttk.Label(card, text="", style="Body.TLabel")
        self.file_label.pack(anchor=tk.W, pady=(8, 0))

    def _build_size_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=" ② 定制输出规格 ", style="Card.TLabelframe",
                              padding=14)
        card.pack(fill=tk.X, pady=(0, 12))

        # 预设方案：降低使用门槛
        preset_row = ttk.Frame(card, style="TFrame")
        preset_row.pack(fill=tk.X, pady=(0, 10))

        self.preset_var = tk.StringVar(value="")
        preset_box = ttk.Combobox(
            preset_row, textvariable=self.preset_var, state="readonly",
            values=[label for label, _ in PRESET_SIZE_SETS.values()], width=34,
        )
        preset_box.pack(side=tk.LEFT)
        preset_box.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Button(preset_row, text="应用", command=self._on_preset,
                   style="Ghost.TButton").pack(side=tk.LEFT, padx=(8, 0))

        grid = ttk.Frame(card, style="TFrame")
        grid.pack(fill=tk.X)
        for index, size in enumerate(ICO_SIZES):
            var = tk.BooleanVar(value=True)
            self.size_vars[size] = var
            ttk.Checkbutton(grid, text=f"{size[0]}x{size[1]}", variable=var).grid(
                row=index // 3, column=index % 3, sticky=tk.W, padx=8, pady=4)
            # 勾选变化即时刷新体积提示
            var.trace_add("write", lambda *_a: self._refresh_size_hint())

        ttk.Button(card, text="全选", command=lambda: self._set_all(True),
                   style="Ghost.TButton").pack(side=tk.LEFT, pady=(10, 0), padx=(0, 6))
        ttk.Button(card, text="取消全选", command=lambda: self._set_all(False),
                   style="Ghost.TButton").pack(side=tk.LEFT, pady=(10, 0))

        # 体积提示：让用户理解「多选尺寸」的代价（调研要点 3）
        self.size_hint_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.size_hint_var, style="Hint.TLabel",
                  wraplength=430).pack(anchor=tk.W, pady=(8, 0))

        # 适配策略
        fit_row = ttk.Frame(card, style="TFrame")
        fit_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(fit_row, text="非正方形图片处理：", style="Body.TLabel").pack(side=tk.LEFT)
        self.fit_var = tk.StringVar(value="cover")
        ttk.Combobox(fit_row, textvariable=self.fit_var, state="readonly",
                     values=[label for label, _ in FIT_MODES], width=34).pack(side=tk.LEFT)

        # maskable 安全区比例与 contain 补边颜色（docs/09 要点 4.2、4.3）
        param_row = ttk.Frame(card, style="TFrame")
        param_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(param_row, text="安全区比例：", style="Body.TLabel").pack(side=tk.LEFT)
        self.safe_zone_var = tk.StringVar(value=f"{MASKABLE_SAFE_ZONE:.2f}")
        ttk.Spinbox(param_row, from_=MIN_SAFE_ZONE, to=MAX_SAFE_ZONE, increment=0.05,
                    textvariable=self.safe_zone_var, width=6).pack(side=tk.LEFT)
        ttk.Label(param_row, text="留白颜色：", style="Body.TLabel").pack(
            side=tk.LEFT, padx=(12, 0))
        self.background_var = tk.StringVar(value="#ffffff")
        ttk.Entry(param_row, textvariable=self.background_var, width=9).pack(side=tk.LEFT)
        self.pad_opaque_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(param_row, text="补边不透明", variable=self.pad_opaque_var
                        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(param_row, text="留白比例：", style="Body.TLabel").pack(
            side=tk.LEFT, padx=(12, 0))
        self.padding_var = tk.StringVar(value=f"{MIN_PADDING:.2f}")
        ttk.Spinbox(param_row, from_=MIN_PADDING, to=MAX_PADDING, increment=0.05,
                    textvariable=self.padding_var, width=6).pack(side=tk.LEFT)

        # 一站式站点物料包（调研要点 3：同类项目均只输出 ICO）
        self.site_assets_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            card, text="同时生成站点物料包（apple-touch-icon / android / mstile / manifest 等 13 个文件）",
            variable=self.site_assets_var,
        ).pack(anchor=tk.W, pady=(12, 0))

        self.generate_btn = ttk.Button(
            card, text="生成 ICO 文件", command=self.generate,
            style="Accent.TButton", state=tk.DISABLED,
        )
        self.generate_btn.pack(fill=tk.X, pady=(14, 0), ipady=3)

    def _refresh_size_hint(self) -> None:
        """勾选尺寸变化时刷新体积提示。

        调研实测：16/32/48 约 2.4KB；再加 64/128/256 达 8.9KB（近 4 倍），
        而 64/128 极少被浏览器选中。用户应当知情后再决定。
        """
        if getattr(self, "size_hint_var", None) is None:
            return
        selected = self._selected_sizes()
        if not selected:
            self.size_hint_var.set("")
            return

        # 估算体积：按已生成结果优先，否则用同尺寸基准图片实测
        size_bytes = 0
        if self.merged_ico:
            size_bytes = len(self.merged_ico)
        else:
            try:
                from .core import build_ico
                probe = build_ico(normalize_to_square(self.image or Image.new("RGBA", (512, 512), (56, 130, 246, 255))), selected)
                size_bytes = len(probe)
            except Exception:  # noqa: BLE001 - 提示失败不应影响交互
                self.size_hint_var.set("")
                return

        self.size_hint_var.set("体积提示：" + estimate_ico_cost(size_bytes, selected))

    def _build_preview_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=" ③ 预览与下载 ", style="Card.TLabelframe",
                              padding=14)
        card.pack(fill=tk.BOTH, expand=True)

        self.preview_box = tk.Label(
            card, text="尚未生成", bg="#faf6ec", fg="#a89f8d", relief="groove", bd=1,
            font=("等线", 10), height=6,
        )
        self.preview_box.pack(fill=tk.BOTH, expand=True)

        self.result_label = ttk.Label(card, text="", style="Body.TLabel", wraplength=380)
        self.result_label.pack(anchor=tk.W, pady=(10, 0))

        export = ttk.Frame(card, style="TFrame")
        export.pack(fill=tk.X, pady=(10, 0))

        self.save_merged_btn = ttk.Button(
            export, text="保存多尺寸 ICO", command=self.save_merged,
            style="Primary.TButton", state=tk.DISABLED,
        )
        self.save_merged_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.save_bundle_btn = ttk.Button(
            export, text="保存全部尺寸 (ZIP)", command=self.save_bundle,
            style="Ghost.TButton", state=tk.DISABLED,
        )
        self.save_bundle_btn.pack(side=tk.LEFT, padx=(0, 8))

        # 站点物料包（调研要点 3：同类项目均无此能力）
        self.save_site_btn = ttk.Button(
            export, text="保存站点物料包 (ZIP)", command=self.save_site_assets,
            style="Ghost.TButton", state=tk.DISABLED,
        )
        self.save_site_btn.pack(side=tk.LEFT, padx=(0, 8))

        # 小尺寸可读性对照（docs/09 要点 6.1）
        self.readability_btn = ttk.Button(
            export, text="可读性检查", command=self.show_readability,
            style="Ghost.TButton", state=tk.DISABLED,
        )
        self.readability_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.open_dir_btn = ttk.Button(
            export, text="打开输出目录", command=self.open_output_dir,
            style="Ghost.TButton",
        )
        self.open_dir_btn.pack(side=tk.LEFT)

        # 质量提示区：源图过小 / JPEG 无透明（调研要点 2、8）
        self.warning_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.warning_var, style="Warn.TLabel",
                  wraplength=380).pack(anchor=tk.W, pady=(10, 0))

    # ---------------- 交互 ----------------

    def _on_close(self) -> None:
        """关闭窗口：先保存偏好，再退出。"""
        self._save_preferences()
        LOGGER.info("界面偏好已保存")
        self.root.destroy()

    def _set_all(self, value: bool) -> None:
        for var in self.size_vars.values():
            var.set(value)

    def _on_preset(self, _event=None) -> None:
        label = self.preset_var.get()
        for key, (preset_label, sizes) in PRESET_SIZE_SETS.items():
            if preset_label != label:
                continue
            selected = set(sizes)
            for size, var in self.size_vars.items():
                var.set(size in selected)
            LOGGER.info("应用预设 %s：%s", key, label)
            return

    def _selected_sizes(self) -> list[tuple[int, int]]:
        return [size for size, var in self.size_vars.items() if var.get()]

    def _current_fit_mode(self) -> str:
        label = self.fit_var.get()
        for item_label, value in FIT_MODES:
            if item_label == label:
                return value
        return "cover"

    def _quality_params(self) -> tuple[float, tuple[int, int, int], bool, float]:
        """读取安全区比例、留白颜色、补边透明度与通用留白比例。

        非法值在生成前就报错，而不是等产出不可用的结果（docs/09 要点 6.2）。
        """
        try:
            safe_zone = float(self.safe_zone_var.get())
        except (TypeError, ValueError) as exc:
            raise IcogenError(f"安全区比例必须是数字：{self.safe_zone_var.get()}") from exc
        if not (MIN_SAFE_ZONE <= safe_zone <= MAX_SAFE_ZONE):
            raise IcogenError(
                f"安全区比例必须在 {MIN_SAFE_ZONE} 至 {MAX_SAFE_ZONE} 之间：{safe_zone}"
            )

        try:
            padding = float(self.padding_var.get())
        except (TypeError, ValueError) as exc:
            raise IcogenError(f"留白比例必须是数字：{self.padding_var.get()}") from exc
        if not (MIN_PADDING <= padding <= MAX_PADDING):
            raise IcogenError(
                f"留白比例必须在 {MIN_PADDING} 至 {MAX_PADDING} 之间：{padding}"
            )

        text = (self.background_var.get() or "").strip().lstrip("#")
        if len(text) != 6:
            raise IcogenError(
                f"留白颜色必须是 6 位十六进制，如 #ffffff：{self.background_var.get()}"
            )
        try:
            background = tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError as exc:
            raise IcogenError(
                f"留白颜色不是合法十六进制：{self.background_var.get()}"
            ) from exc

        return safe_zone, background, bool(self.pad_opaque_var.get()), padding

    # ---------------- 偏好持久化 ----------------

    def _restore_preferences(self) -> None:
        """恢复上次的选择（借鉴 Elephicon 的 electron-store 做法）。

        配置损坏时静默跳过，不影响启动。
        """
        ui = getattr(self.config, "ui", None)
        if ui is None:
            return
        try:
            if ui.window_geometry:
                self.root.geometry(ui.window_geometry)

            if ui.fit_mode:
                for label, value in FIT_MODES:
                    if value == ui.fit_mode:
                        self.fit_var.set(label)
                        break

            if ui.sizes:
                wanted = {str(s) for s in ui.sizes}
                for size, var in self.size_vars.items():
                    var.set(str(size[0]) in wanted)
            elif ui.preset:
                # 只有记了 preset 才用预设覆盖默认全选
                label = PRESET_SIZE_SETS.get(ui.preset, ("", None))[0]
                if label:
                    self.preset_var.set(label)

            self.site_assets_var.set(bool(ui.site_assets))
            self.padding_var.set(f"{float(ui.padding or 0.0):.2f}")

            LOGGER.info(
                "已恢复界面偏好：preset=%s fit=%s sizes=%s",
                ui.preset, ui.fit_mode, ui.sizes,
            )
        except Exception as exc:  # noqa: BLE001 - 偏好恢复失败不应阻断启动
            LOGGER.warning("恢复界面偏好失败，使用默认值：%s", exc)

    def _parse_padding_pref(self) -> float:
        """把当前留白比例控件值转为可持久化的数字，非法值回退 0。"""
        try:
            value = float(self.padding_var.get())
        except (TypeError, ValueError):
            return 0.0
        return value if MIN_PADDING <= value <= MAX_PADDING else 0.0

    def _save_preferences(self) -> None:
        """保存当前选择，下次启动恢复。"""
        ui = getattr(self.config, "ui", None)
        if ui is None:
            return
        try:
            selected = self._selected_sizes()
            ui.fit_mode = self._current_fit_mode()
            ui.padding = self._parse_padding_pref()
            ui.sizes = [str(s[0]) for s in selected]
            ui.site_assets = bool(self.site_assets_var.get())
            ui.window_geometry = self.root.geometry()
            # 反查 preset key：仅当当前勾选与某预设完全一致时才记录，
            # 否则记空——避免下次启动用错误的预设覆盖用户的手动选择。
            ui.preset = ""
            for key, (label, sizes) in PRESET_SIZE_SETS.items():
                if self.preset_var.get() == label and set(sizes) == set(selected):
                    ui.preset = key
                    break
            save_config(self.config)
        except Exception as exc:  # noqa: BLE001 - 偏好保存失败不应影响使用
            LOGGER.warning("保存界面偏好失败：%s", exc)

    # ---------------- 质量提示 ----------------

    def _refresh_warnings(self) -> None:
        """按级别给出质量提示（调研要点 2、8；分级化见 docs/09 要点 5.1）。

        同类项目对这两点全部静默处理，导致用户拿到模糊/不透明图标却不知原因。
        实测：16px 源图生成的 256 层比 256px 源图大 20 倍却更模糊；
        JPEG 源图生成的 ICO 必然不透明（alpha 恒为 255）。

        现在统一走 ``quality_hints``：新增「逼近解码上限」「体积超软预算」两类，
        且每条提示带 level/code，界面只负责展示，不再自己拼文案。
        """
        if self.image is None or not hasattr(self, "warning_var"):
            return

        selected = self._selected_sizes()
        if not selected:
            self.warning_var.set("")
            return

        try:
            hints = quality_hints(self.image, selected, self.merged_ico)
        except IcogenError:
            self.warning_var.set("")
            return

        self.warning_var.set(format_hints(hints))
        self._hint_level = worst_level(hints)

    def pick_image(self) -> None:
        path = filedialog.askopenfilename(title="选择图片文件", filetypes=FILETYPES)
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> None:
        """加载源图。所有失败都以业务异常呈现，不抛裸栈。"""
        try:
            image = open_image_file(path)
        except IcogenError as exc:
            LOGGER.warning("加载失败 %s：%s", path, exc)
            self._set_status(f"加载失败：{exc}")
            messagebox.showerror("无法打开图片", str(exc), parent=self.root)
            return
        except OSError as exc:
            LOGGER.exception("读取文件失败 %s", path)
            self._set_status("读取文件失败")
            messagebox.showerror("无法读取文件", str(exc), parent=self.root)
            return

        self.image = image
        self.image_path = path
        # 换图后旧的生成结果失效
        self.merged_ico = None
        self.bundle = {}
        self.artifact_id = ""
        self._set_status(f"已载入 {Path(path).name}（{image.width}×{image.height}）")
        LOGGER.info("载入图片 %s (%sx%s)", path, image.width, image.height)

        self.file_label.configure(text=f"当前：{Path(path).name}  ({image.width}×{image.height})")
        self.generate_btn.configure(state=tk.NORMAL)
        self._reset_export_buttons()
        self._render_source_preview()
        self._refresh_warnings()
        self._refresh_size_hint()

    def _render_source_preview(self) -> None:
        """渲染源图预览。

        关键修复：预览图走内存，不再写 ``temp_preview.png`` 到 CWD。
        """
        if self.image is None:
            return
        try:
            # 关键：ImageTk.PhotoImage 只接受 PIL.Image 对象，不能直接吃 BytesIO。
            # 之前传 BytesIO 会抛 "'_io.BytesIO' object has no attribute 'mode'"，
            # 导致预览永远退化成「预览不可用」。
            preview = Image.open(io.BytesIO(make_png_preview(
                self.image, max_edge=220, fit_mode="contain"
            )))
            photo = ImageTk.PhotoImage(preview)
            self._photo_refs = [photo]
            self.preview_box.configure(image=photo, text="")
        except Exception as exc:  # noqa: BLE001 - 预览失败不应中断主流程
            LOGGER.warning("预览渲染失败：%s", exc)
            self.preview_box.configure(image="", text="预览不可用")

    def generate(self) -> None:
        """生成合并 ICO 与各尺寸文件，并落盘。"""
        if self.image is None:
            messagebox.showwarning("提示", "请先选择图片", parent=self.root)
            return

        sizes = self._selected_sizes()
        if not sizes:
            messagebox.showwarning("提示", "请至少选择一个 ICO 尺寸", parent=self.root)
            return

        self.generate_btn.configure(state=tk.DISABLED)
        self._set_status("生成中…")
        self.root.update_idletasks()

        try:
            fit_mode = self._current_fit_mode()
            safe_zone, background, opaque_pad, padding = self._quality_params()
            merged = build_ico(
                self.image, sizes, fit_mode=fit_mode, safe_zone=safe_zone,
                background=background, opaque_pad=opaque_pad, padding=padding,
            )
            bundle = build_ico_bundle(
                self.image, sizes, fit_mode=fit_mode, safe_zone=safe_zone,
                background=background, opaque_pad=opaque_pad, padding=padding,
            )

            record = self.store.save_bytes(
                merged, filename="favicon.ico",
                source_name=Path(self.image_path).name,
                fit_mode=fit_mode,
                sizes=[f"{w}x{h}" for w, h in sizes],
            )

            self.merged_ico = merged
            self.bundle = bundle
            self.artifact_id = record.artifact_id

            self._render_result_preview(sizes)
            self.save_merged_btn.configure(state=tk.NORMAL)
            self.save_bundle_btn.configure(state=tk.NORMAL)
            self.save_site_btn.configure(state=tk.NORMAL)
            self.readability_btn.configure(state=tk.NORMAL)

            extra = "　·　" + estimate_ico_cost(len(merged), sizes) if len(sizes) > 3 else ""
            self.result_label.configure(
                text=f"已生成 {len(sizes)} 个尺寸（合并 ICO {len(merged):,} 字节）{extra}\n"
                     f"已保存：{record.filename} → {self.output_dir}"
            )
            self._set_status("生成完成")
            self._refresh_warnings()
            self._refresh_size_hint()
            LOGGER.info("生成完成 sizes=%s fit=%s -> %s", sizes, fit_mode, record.filename)
        except IcogenError as exc:
            LOGGER.warning("生成失败：%s", exc)
            self._set_status("生成失败")
            messagebox.showerror("生成失败", str(exc), parent=self.root)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("生成过程出现未预期错误")
            self._set_status("生成失败")
            messagebox.showerror("生成失败", f"发生未预期错误：{exc}", parent=self.root)
        finally:
            self.generate_btn.configure(state=tk.NORMAL)

    def _render_result_preview(self, sizes: list[tuple[int, int]]) -> None:
        """把各尺寸结果拼成一张预览图（纯内存）。"""
        try:
            largest = max(s[0] for s in sizes)
            thumb_edge = max(24, min(64, 256 // max(1, len(sizes))))
            total_w = thumb_edge * len(sizes) + 8 * (len(sizes) + 1)
            total_h = thumb_edge + 8

            sheet = Image.new("RGB", (total_w, total_h), "#faf6ec")
            source = self.image
            if source is None:
                return

            from .core.imaging import normalize_to_square
            safe_zone, background, opaque_pad, padding = self._quality_params()
            square = normalize_to_square(
                source, mode=self._current_fit_mode(), background=background,
                safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
            )

            for index in range(len(sizes)):
                tile = square.resize((thumb_edge, thumb_edge), Image.Resampling.LANCZOS)
                sheet.paste(tile.convert("RGB"), (8 + index * (thumb_edge + 8), 4))

            buffer = io.BytesIO()
            sheet.save(buffer, format="PNG")
            # 同上：PhotoImage 需要 PIL.Image，不能直接传 BytesIO
            photo = ImageTk.PhotoImage(Image.open(io.BytesIO(buffer.getvalue())))
            self._photo_refs = [photo]
            self.preview_box.configure(
                image=photo,
                text=f"预览：{len(sizes)} 个尺寸（最大 {largest}px）",
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("结果预览失败：%s", exc)
            self.preview_box.configure(image="", text="预览不可用")

    def _reset_export_buttons(self) -> None:
        self.save_merged_btn.configure(state=tk.DISABLED)
        self.save_bundle_btn.configure(state=tk.DISABLED)
        self.save_site_btn.configure(state=tk.DISABLED)
        self.readability_btn.configure(state=tk.DISABLED)
        self.result_label.configure(text="")

    def show_readability(self) -> None:
        """按真实像素展示 16/32/48 的生成结果（docs/09 要点 6.1）。

        主预览区为了看得清会把图放大，用户因此看不到小尺寸下的真实观感。
        这里按**实际像素**渲染，同时给一份 4 倍最近邻放大图供细看——
        两者正好回答「这个图标缩到 16px 还能认出吗」。

        借鉴来源：qiaomu-icon-generator 的 favicon-readability-sheet。
        """
        if self.image is None:
            return

        try:
            safe_zone, background, opaque_pad, padding = self._quality_params()
            square = normalize_to_square(
                self.image, mode=self._current_fit_mode(), background=background,
                safe_zone=safe_zone, opaque_pad=opaque_pad, padding=padding,
            )
        except IcogenError as exc:
            messagebox.showwarning("参数有误", str(exc), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("可读性检查（实际像素）")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        ttk.Label(
            dialog, style="Body.TLabel", wraplength=460, justify=tk.LEFT,
            text="左列为真实像素（16 / 32 / 48），右列为 4 倍最近邻放大，"
                 "仅用于看清像素块。若左列糊成一团，说明该尺寸下不可用，"
                 "建议换更大的源图或减少细节。",
        ).pack(anchor=tk.W, padx=14, pady=(14, 10))

        board = ttk.Frame(dialog, style="TFrame")
        board.pack(padx=14, pady=(0, 14))
        photos: list[ImageTk.PhotoImage] = []

        for column, (size, zoom) in enumerate(
            ((16, 1), (32, 1), (48, 1), (16, 4), (32, 4), (48, 4))
        ):
            tile = square.resize((size, size), Image.Resampling.LANCZOS).convert("RGB")
            if zoom > 1:
                tile = tile.resize((size * zoom, size * zoom), Image.Resampling.NEAREST)
            photos.append(ImageTk.PhotoImage(tile))

            cell = ttk.Frame(board, style="TFrame")
            cell.grid(row=0, column=column, padx=8, pady=4)
            # 用 tk.Label 而非 ttk：背景色固定，方便看清小图边界
            tk.Label(cell, image=photos[-1], bg="#ffffff", relief="solid", bd=1
                     ).pack()
            label = f"{size}×{size}" + ("（放大 4 倍）" if zoom > 1 else "（实际像素）")
            ttk.Label(cell, text=label, style="Hint.TLabel").pack(pady=(4, 0))

        # 必须持有引用，否则 PhotoImage 被回收后 Label 变空白
        dialog._icogen_photos = photos  # noqa: SLF001 - 明确的生命周期管理
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def save_site_assets(self) -> None:
        """导出站点物料包（调研要点 3：同类项目均只输出 ICO）。

        产出 13 个文件：多尺寸 ICO、apple-touch-icon、android-chrome、
        maskable 变体、mstile、manifest.json、browserconfig.xml、
        可直接粘贴的 HTML 片段，以及记录全部参数与用途的 MANIFEST.txt。
        """
        if self.image is None:
            return

        path = filedialog.asksaveasfilename(
            title="保存站点物料包", defaultextension=".zip",
            filetypes=[("ZIP 压缩包", "*.zip")], initialfile="site-assets.zip",
        )
        if not path:
            return

        try:
            sizes = self._selected_sizes() or None
            safe_zone, background, opaque_pad, padding = self._quality_params()
            data = build_site_assets_zip(
                self.image,
                ico_sizes=sizes or [(16, 16), (32, 32), (48, 48)],
                fit_mode=self._current_fit_mode(),
                safe_zone=safe_zone,
                background=background,
                opaque_pad=opaque_pad,
                padding=padding,
            )
            Path(path).write_bytes(data)

            record = self.store.save_bundle(data, base_name="site-assets")
            self._set_status(f"已保存物料包：{Path(path).name}（{len(data):,} 字节）")
            LOGGER.info(
                "站点物料包 -> %s (%d 字节), 已登记 %s", path, len(data), record.filename,
            )
        except (IcogenError, OSError, ValueError) as exc:
            LOGGER.warning("物料包导出失败 %s：%s", path, exc)
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def save_merged(self) -> None:
        if self.merged_ico is None:
            return
        path = filedialog.asksaveasfilename(
            title="保存多尺寸 ICO", defaultextension=".ico",
            filetypes=[("ICO 文件", "*.ico")], initialfile="favicon.ico",
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self.merged_ico)
            self._set_status(f"已保存：{Path(path).name}")
            LOGGER.info("保存合并 ICO -> %s (%d 字节)", path, len(self.merged_ico))
        except OSError as exc:
            LOGGER.warning("保存失败 %s：%s", path, exc)
            messagebox.showerror("保存失败", str(exc), parent=self.root)

    def save_bundle(self) -> None:
        """把各尺寸 ICO 打包成 ZIP 保存。"""
        if not self.bundle:
            return
        path = filedialog.asksaveasfilename(
            title="保存全部尺寸", defaultextension=".zip",
            filetypes=[("ZIP 压缩包", "*.zip")], initialfile="icons.zip",
        )
        if not path:
            return
        try:
            from zipfile import ZIP_DEFLATED, ZipFile

            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                for (width, height), data in sorted(self.bundle.items()):
                    archive.writestr(f"icon_{width}x{height}.ico", data)
            self._set_status(f"已打包：{Path(path).name}（{len(self.bundle)} 个文件）")
            LOGGER.info("打包导出 -> %s (%d 个尺寸)", path, len(self.bundle))
        except (OSError, ValueError) as exc:
            LOGGER.warning("打包失败 %s：%s", path, exc)
            messagebox.showerror("打包失败", str(exc), parent=self.root)

    def open_output_dir(self) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(self.output_dir))  # noqa: S606 - Windows 打开目录
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", str(self.output_dir)], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", str(self.output_dir)], check=False)
        except (OSError, ValueError) as exc:
            LOGGER.warning("打开目录失败：%s", exc)
            messagebox.showerror("无法打开", str(exc), parent=self.root)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ---------------- 拖拽 ----------------

    def _on_drop(self, event) -> str:
        """处理拖拽放下。

        关键修复：使用 ``tk.splitlist`` 解析 Tk 的拖拽负载。

        旧版先剥花括号再 ``shlex.split(path)[0]``，会把
        ``C:\\Users\\me\\My Pictures\\a.png`` 截成 ``C:UsersmeMy``（文件不存在）；
        多文件拖入时也只处理第一个且路径同样可能被截断。
        """
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except (tk.TclError, ValueError, AttributeError) as exc:
            LOGGER.warning("拖拽数据解析失败：%s", exc)
            messagebox.showwarning("提示", "无法解析拖入的文件路径", parent=self.root)
            return event.action

        if not paths:
            return event.action

        if len(paths) > 1:
            self._set_status(f"检测到 {len(paths)} 个文件，仅处理第一个")
            LOGGER.info("拖入多个文件，取第一个：%s", paths[0])

        self.load_image(paths[0])
        return event.action


def _enable_windows_high_dpi() -> None:
    """Windows 高 DPI 感知（借鉴要点 D1）。

    必须在 ``tk.Tk()`` 之前调用：进程默认不声明 DPI 感知时，Windows 会对
    整个窗口做位图拉伸，文字与图标在高分屏上发虚。Tk 8.6 自身不处理，
    这里显式声明 per-monitor v2，失败逐级回落，最终失败也不阻断启动
    （无 shcore 的旧系统只是维持原状）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        windll = ctypes.windll.shcore  # noqa: SLF001 - Windows 专属 API
        try:
            windll.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:  # noqa: BLE001 - 旧系统不支持 per-monitor
            windll.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
    except Exception:  # noqa: BLE001 - 无 shcore（Win7 等）维持默认
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    """桌面版入口。"""
    _setup_logging()
    _enable_windows_high_dpi()
    LOGGER.info("IcoGenerator 桌面版 v%s 启动", VERSION)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # 无图形环境（SSH / 容器 / CI）
        print(f"无法创建图形界面：{exc}", file=sys.stderr)
        print("请改用网页版：python -m icogenerator.server", file=sys.stderr)
        return 1

    try:
        IcoGeneratorApp(root)
    except Exception as exc:  # noqa: BLE001 - 启动失败要给出可读原因
        LOGGER.exception("界面初始化失败")
        messagebox.showerror("启动失败", f"初始化界面时出错：{exc}", parent=root)
        return 1

    root.mainloop()
    LOGGER.info("桌面版退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())