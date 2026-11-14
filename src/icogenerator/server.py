"""本地 HTTP 后台服务。

架构定位：**桌面版与网页版共用的同一套后端**。

安全设计（对照旧版「前端直传第三方图床」缺陷）：
- 默认仅绑定 127.0.0.1，不监听外网；设置非回环地址时强制要求管理员令牌；
- 写操作（管理接口）需携带 Bearer 令牌，令牌随机生成并只存内存；
- 上传凭据从不由前端回显（配置接口走脱敏视图）；
- 前台生成接口无鉴权但仅限本机，且只写本地磁盘；
- 全量请求体大小限制，防止内存耗尽；
- 路径参数一律经过净化，杜绝路径穿越。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__ as VERSION
from .config import AppConfig, save_config
from .core import (
    BITMAP_FORMATS,
    ICO_SIZES,
    MASKABLE_SAFE_ZONE,
    MAX_PADDING,
    MAX_SAFE_ZONE,
    MIN_PADDING,
    MIN_SAFE_ZONE,
    PRESET_SIZE_SETS,
    analyze_source,
    build_ico,
    build_ico_bundle,
    build_site_assets_zip,
    decode_image,
    describe_site_assets,
    estimate_ico_cost,
    hints_to_dicts,
    quality_hints,
    worst_level,
)
from .core.errors import IcogenError
from .queue import UploadQueue
from .storage import LocalStore, sanitize_filename

LOGGER = logging.getLogger(__name__)

# 请求体上限：源图 + 多份 ICO 预览，20MB 足够且能挡住内存炸弹
MAX_BODY_BYTES = 20 * 1024 * 1024

# 拒绝请求前排空请求体的最长时间（秒），防止被当作慢速攻击面
DRAIN_TIMEOUT_SECONDS = 5.0

# 静态资源白名单：固定文件名 + 固定 MIME。
# 不接受用户传入路径，杜绝 ../ 穿越读取任意文件。
_STATIC_FILES: Dict[str, str] = {
    "/style.css": "text/css; charset=utf-8",
    "/admin.js": "text/javascript; charset=utf-8",
    "/client.css": "text/css; charset=utf-8",
    "/client.js": "text/javascript; charset=utf-8",
}

# 站点图标：由本项目自己的引擎在内存中生成（"吃自己的狗粮"），
# 避免额外维护一个二进制素材文件。首次请求时构建，之后复用。
_FAVICON_SIZES: Tuple[Tuple[int, int], ...] = ((16, 16), (32, 32), (48, 48))
_FAVICON_BRAND = (14, 125, 104)      # 青瓷绿，与 --primary 同值
_FAVICON_INK = (244, 239, 228)       # 暖砂，与 --bg 同值
_favicon_cache: Optional[bytes] = None
_favicon_lock = threading.Lock()


def _build_favicon() -> bytes:
    """绘制品牌图标（圆角方块 + 对勾）并编码为多尺寸 ICO。

    图案与页面左栏 brand-mark 的内联 SVG 保持一致：圆角方块内一个对勾。
    完全在内存中完成，不落临时文件。
    """
    from PIL import Image, ImageDraw

    canvas = 64
    pad = 4
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [pad, pad, canvas - pad - 1, canvas - pad - 1],
        radius=16,
        fill=_FAVICON_BRAND + (255,),
    )
    draw.line([(20, 33), (29, 42), (45, 23)], fill=_FAVICON_INK + (255,),
              width=6, joint="curve")
    return build_ico(image, list(_FAVICON_SIZES))


def _get_favicon() -> bytes:
    """惰性构建并缓存站点图标；失败返回空串，由调用方降级为 404。"""
    global _favicon_cache
    if _favicon_cache is not None:
        return _favicon_cache
    with _favicon_lock:
        if _favicon_cache is None:
            try:
                _favicon_cache = _build_favicon()
            except Exception:  # noqa: BLE001 - 图标失败不影响任何业务接口
                LOGGER.exception("站点图标生成失败")
                return b""
    return _favicon_cache


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _web_dir() -> Path:
    """管理页面静态资源目录。"""
    return Path(__file__).parent / "web"


class IcoGenHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    server_version = f"IcoGenerator/{VERSION}"
    sys_version = ""  # 不暴露 Python 版本

    # 由 create_server 注入
    store: LocalStore
    queue: UploadQueue
    config: AppConfig
    admin_token: str
    require_auth: bool

    # GET 路由表，由 _register_get_routes() 在实例上填充
    _GET_ROUTES: Dict[str, Callable[[Dict[str, Any]], None]]

    # ---------------- 基础工具 ----------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """访问日志走 logging，且不记录令牌等敏感值。"""
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> Dict[str, Any]:
        """读取并解析 JSON 请求体。

        解析失败必须转成 400 而不是让 ``JSONDecodeError`` 冒泡成 500 ——
        客户端发来的畸形请求属于客户端错误，不是服务端故障。
        """
        raw = self._read_body().decode("utf-8", "replace").strip()
        if not raw:
            raise IcogenError("请求体为空", hint="请提交 JSON 对象")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IcogenError(
                f"请求体不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）",
                hint="请检查请求格式",
            ) from exc
        if not isinstance(payload, dict):
            raise IcogenError("请求体必须是 JSON 对象")
        return payload

    def _drain_body(self) -> None:
        """在返回错误响应前把请求体读干净（内存有界、时长有界）。

        为什么必须读：HTTP 服务端若在请求体尚未收完时就复用连接，
        客户端仍在发送数据会收到 TCP RST（Windows 表现为
        ``WinError 10053/10054``），表现为「服务莫名断连」而不是应有的 400/415。

        两道限制，避免被当成慢速攻击面：
        - 内存：64KB 分块读取并**立即丢弃**，峰值内存恒定，与请求体大小无关；
        - 时长：最多排空 DRAIN_TIMEOUT_SECONDS 秒，超时则直接关闭连接。
        """
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return

        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        while remaining > 0:
            if time.monotonic() > deadline:
                # 排空超时：标记连接关闭，避免复用这条半读状态的连接
                LOGGER.warning("请求体排空超时，关闭连接")
                self.close_connection = True
                return
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                # 客户端提前断开
                return
            remaining -= len(chunk)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 安全响应头
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = HTTPStatus.BAD_REQUEST,
                         hint: str = "") -> None:
        payload: Dict[str, Any] = {"ok": False, "error": message}
        if hint:
            payload["hint"] = hint
        self._send_json(payload, status)

    def _send_binary(self, data: bytes, content_type: str,
                     filename: str = "", *, download: bool = False) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if download and filename:
            safe = sanitize_filename(filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        """读取并校验请求体大小。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise IcogenError("Content-Length 无效") from exc

        if length <= 0:
            raise IcogenError("请求体为空")
        if length > MAX_BODY_BYTES:
            # 超限时只读到上限即可，避免为拒绝一个请求分配 20MB+
            self._drain_body()
            raise IcogenError(
                f"请求体过大（{length} 字节），上限 {MAX_BODY_BYTES // (1024 * 1024)}MB",
                hint="请压缩源图后重试",
            )
        return self.rfile.read(length)

    def _check_auth(self) -> bool:
        """管理接口鉴权：Bearer 令牌比对，使用常数时间比较。"""
        header = self.headers.get("Authorization") or ""
        authorized = False
        if header.startswith("Bearer "):
            # secrets.compare_digest 避免时序侧信道
            authorized = secrets.compare_digest(header[7:].strip(), self.admin_token)

        if not authorized:
            # 拒绝前先读完请求体，否则客户端仍在发送数据就会收到 RST，
            # 表现为「连接被中止」而不是本应返回的 401。
            self._drain_body()
            message = "缺少管理令牌" if not header.startswith("Bearer ") else "管理令牌无效"
            self._send_error_json(message, HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _check_origin(self) -> bool:
        """拒绝跨站来源，防止恶意网页借用户浏览器调用本机服务。

        ``/api/generate`` 无需令牌，若不校验来源，任意网站都能让用户浏览器
        往本机服务发请求（CSRF / DNS rebinding）。凡是带 Origin 且不是
        同源（127.0.0.1 / localhost / 本机主机名）的请求一律拒绝。
        """
        origin = self.headers.get("Origin")
        if not origin:
            # 非浏览器客户端（curl / 桌面版 / 测试）不带 Origin，放行
            return True

        parsed = urlparse(origin)
        hostname = (parsed.hostname or "").lower()
        if hostname in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"):
            return True

        LOGGER.warning("拒绝跨站请求，Origin=%s", origin)
        self._drain_body()
        self._send_error_json("拒绝跨站请求", HTTPStatus.FORBIDDEN)
        return False

    def _check_content_type(self) -> bool:
        """要求 POST 使用 JSON。

        浏览器发起的简单表单请求（text/plain / form-urlencoded / 不带头）
        无需预检，若不校验 Content-Type 就可能被跨站触发。
        因此这里**不放行空 Content-Type**，一律要求显式声明 JSON。
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            return True
        self._drain_body()
        self._send_error_json(
            "仅接受 application/json 请求"
            + (f"，当前为 {ctype}" if ctype else "，未声明 Content-Type"),
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        )
        return False

    def _parse_sizes(self, query: Dict[str, Any], payload: Dict[str, Any]) -> list:
        """从查询串或 JSON 体解析尺寸列表。

        兼容两种写法：``sizes=16,32,48`` 与 ``sizes[]=16&sizes[]=32``。
        """
        raw = payload.get("sizes") if isinstance(payload, dict) else None
        if raw is None:
            raw = query.get("sizes", [])

        values: list[str] = []
        if isinstance(raw, str):
            values = [part for part in raw.replace(" ", "").split(",") if part]
        elif isinstance(raw, (list, tuple)):
            values = [str(part) for part in raw]

        sizes: list[Tuple[int, int]] = []
        for value in values:
            text = value.strip().lower().replace("×", "x")
            if "x" in text:
                left, _, right = text.partition("x")
                try:
                    sizes.append((int(left), int(right)))
                    continue
                except ValueError:
                    pass
            try:
                side = int(text)
                sizes.append((side, side))
            except ValueError:
                raise IcogenError(f"无法解析尺寸：{value}")

        if not sizes:
            raise IcogenError("未指定任何尺寸", hint="请传入 sizes=16,32,48")
        return sizes

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """GET 分发。

        每个路由一个命名方法，主函数只负责解析与分发。
        这样新增路由不必再往一个百来行的方法里塞分支，
        阅读时也能按名字直接跳到对应处理函数。
        """
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            handler = self._GET_ROUTES.get(route)
            if handler is not None:
                handler(self, query)
                return

            if route.startswith("/api/artifacts/") and route.endswith("/download"):
                self._handle_artifact_download(route)
                return

            if route in ("/admin", "/admin/"):
                self._serve_page("index.html", "管理页面未安装")
                return

            if route in ("/app", "/app/"):
                self._serve_page("client.html", "网页版未安装")
                return

            if route == "/favicon.ico":
                self._handle_favicon()
                return

            if route in _STATIC_FILES:
                self._serve_static(route)
                return

            self._send_error_json("接口不存在", HTTPStatus.NOT_FOUND)

        except IcogenError as exc:
            self._send_error_json(exc.message, HTTPStatus.BAD_REQUEST, exc.hint)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程崩溃
            LOGGER.exception("GET %s 处理失败", route)
            self._send_error_json(f"服务内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- GET 各路由处理（由 _GET_ROUTES 注册）--

    def _handle_health(self, _query: Dict[str, Any]) -> None:
        self._send_json({
            "ok": True,
            "service": "icongenerator",
            "version": VERSION,
            "storage_enabled": self.config.storage.enabled,
            "scheduler_enabled": self.config.scheduler.enabled,
        })

    def _handle_presets(self, _query: Dict[str, Any]) -> None:
        self._send_json({
            "ok": True,
            "presets": {
                key: {"label": label, "sizes": [list(s) for s in value]}
                for key, (label, value) in PRESET_SIZE_SETS.items()
            },
            "all_sizes": [list(s) for s in ICO_SIZES],
        })

    def _handle_site_assets_describe(self, _query: Dict[str, Any]) -> None:
        self._send_json({
            "ok": True,
            "assets": describe_site_assets(),
        })

    def _handle_artifacts(self, query: Dict[str, Any]) -> None:
        limit = _safe_int(query.get("limit", ["100"])[0], 100)
        self._send_json({
            "ok": True,
            "artifacts": [r.to_dict() for r in self.store.list_artifacts(limit=limit)],
            "stats": self.store.stats(),
        })

    def _handle_config_get(self, _query: Dict[str, Any]) -> None:
        if not self._check_auth():
            return
        self._send_json({
            "ok": True,
            # 脱敏视图：凭据不回显
            "config": self.config.storage.public_dict(),
            "scheduler": {
                "enabled": self.config.scheduler.enabled,
                "interval_minutes": self.config.scheduler.interval_minutes,
                "batch_size": self.config.scheduler.batch_size,
                "max_retries": self.config.scheduler.max_retries,
            },
            "output_dir": str(self.store.root),
            "queue": self.queue.status(),
        })

    def _handle_queue_status(self, _query: Dict[str, Any]) -> None:
        if not self._check_auth():
            return
        self._send_json({"ok": True, "queue": self.queue.status()})

    def _handle_artifact_download(self, route: str) -> None:
        if not self._check_auth():
            return
        artifact_id = route.split("/")[3]
        try:
            data = self.store.read_artifact_bytes(artifact_id)
        except IcogenError as exc:
            self._send_error_json(exc.message, HTTPStatus.NOT_FOUND, exc.hint)
            return
        record = self.store.get_artifact(artifact_id)
        name = record.filename if record else "icon.ico"
        self._send_binary(data, "application/octet-stream", name, download=True)

    def _serve_page(self, filename: str, missing_message: str) -> None:
        """返回管理页或网页版 HTML。"""
        page = _web_dir() / filename
        if page.exists():
            self._send_binary(page.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send_error_json(missing_message, HTTPStatus.NOT_FOUND)

    def _serve_static(self, route: str) -> None:
        """返回白名单内的静态资源。

        白名单固定文件名 + 固定 MIME，不接受用户传入路径，
        杜绝 ../ 穿越读取任意文件。
        """
        asset = _web_dir() / route.lstrip("/")
        if asset.exists():
            self._send_binary(asset.read_bytes(), _STATIC_FILES[route])
        else:
            self._send_error_json("资源不存在", HTTPStatus.NOT_FOUND)

    def _handle_favicon(self) -> None:
        """返回站点图标（由本项目引擎在内存中生成）。

        浏览器访问 /app 与 /admin 时会自动请求该路径；
        缺失时浏览器只是在控制台留一条 404，不影响任何功能，
        但一个图标生成器自己的页面没有图标说不过去。
        """
        data = _get_favicon()
        if data:
            self._send_binary(data, "image/x-icon")
        else:
            self._send_error_json("站点图标不可用", HTTPStatus.NOT_FOUND)

    def _register_get_routes(self) -> None:
        """在实例上绑定 GET 路由表。

        用实例属性而非类属性：处理方法需要 ``self``，
        而类体里还拿不到最终绑定关系。
        """
        self._GET_ROUTES = {
            "/api/health": self._handle_health,
            "/api/presets": self._handle_presets,
            "/api/site-assets/describe": self._handle_site_assets_describe,
            "/api/artifacts": self._handle_artifacts,
            "/api/config": self._handle_config_get,
            "/api/queue/status": self._handle_queue_status,
        }

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        # 所有 POST 先过来源与内容类型校验
        if not self._check_origin():
            return
        if not self._check_content_type():
            return

        try:
            # ---- 前台生成接口（无需鉴权，仅本机可达）----
            if route == "/api/generate":
                self._handle_generate()
                return

            # ---- 管理接口（需令牌）----
            if route == "/api/config":
                if not self._check_auth():
                    return
                self._handle_update_config()
                return

            if route == "/api/upload":
                if not self._check_auth():
                    return
                self._handle_manual_upload()
                return

            if route == "/api/upload/pending":
                if not self._check_auth():
                    return
                result = self.queue.upload_pending()
                self._send_json(result)
                return

            if route == "/api/scheduler":
                if not self._check_auth():
                    return
                self._handle_scheduler()
                return

            if route == "/api/storage/test":
                if not self._check_auth():
                    return
                self._handle_test_storage()
                return

            if route == "/api/site-assets":
                self._handle_site_assets()
                return

            if route == "/api/bundle":
                self._handle_bundle()
                return

            self._send_error_json("接口不存在", HTTPStatus.NOT_FOUND)

        except IcogenError as exc:
            self._send_error_json(exc.message, HTTPStatus.BAD_REQUEST, exc.hint)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("POST %s 处理失败", route)
            self._send_error_json(f"服务内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    # ---------------- 处理函数 ----------------

    def _handle_generate(self) -> None:
        """生成 ICO。

        请求体为 JSON：``{"image_base64": "...", "sizes": [16,32], "fit_mode": "cover"}``
        响应包含合并 ICO 的 base64 与本地保存路径。
        """
        payload = self._read_json()

        encoded = str(payload.get("image_base64") or "")
        if not encoded:
            raise IcogenError("缺少 image_base64 字段")

        # base64 体积校验，避免先解码再判断
        import base64

        if "," in encoded and encoded.strip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]

        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise IcogenError(f"image_base64 解码失败：{exc}") from exc

        image = decode_image(raw, filename=str(payload.get("filename") or "upload"))
        sizes = self._parse_sizes({}, payload)
        fit_mode = str(payload.get("fit_mode") or "cover")
        if fit_mode not in ("cover", "contain", "maskable", "stretch"):
            raise IcogenError(f"未知的 fit_mode：{fit_mode}")
        safe_zone, background, opaque_pad, padding, bitmap_format = self._parse_quality_params(payload)

        combined = build_ico(
            image, sizes, fit_mode=fit_mode, safe_zone=safe_zone,
            background=background, opaque_pad=opaque_pad, padding=padding,
            bitmap_format=bitmap_format,
        )
        analysis = analyze_source(image, sizes)
        hints = quality_hints(image, sizes, combined)
        record = self.store.save_bytes(
            combined,
            filename=str(payload.get("output_name") or "favicon.ico"),
            source_name=str(payload.get("filename") or ""),
            fit_mode=fit_mode,
            sizes=[f"{w}x{h}" for w, h in sizes],
        )

        self._send_json({
            "ok": True,
            "artifact_id": record.artifact_id,
            "filename": record.filename,
            "size": record.size,
            "sha256": record.sha256,
            "sizes": [list(s) for s in sizes],
            "ico_base64": base64.b64encode(combined).decode("ascii"),
            "upload_status": record.upload_status,
            "analysis": {
                "width": analysis["width"],
                "height": analysis["height"],
                "max_side": analysis["max_side"],
                "has_alpha": analysis["has_alpha"],
                "opaque_source": analysis["opaque_source"],
                "will_upscale": analysis["will_upscale"],
                "upscaled_targets": analysis["upscaled_targets"],
                "cost": estimate_ico_cost(record.size, sizes),
            },
            # 分级质量提示（docs/09 要点 5.1）：level/code/message 三字段，
            # 前端按 level 决定颜色，不靠匹配中文文案。
            "hints": hints_to_dicts(hints),
            "hint_level": worst_level(hints),
        })

    def _parse_quality_params(
        self, payload: Dict[str, Any]
    ) -> Tuple[float, Tuple[int, int, int], bool, float, str]:
        """解析安全区比例、留白颜色、补边透明度、通用留白比例与 ICO 帧格式。

        颜色与比例的非法值在这里就失败，而不是等生成完才发现问题
        （借鉴 altophp/favicon 的「生成前校验」，docs/09 要点 6.2）。
        """
        safe_zone = float(payload.get("safe_zone") or MASKABLE_SAFE_ZONE)
        if not (MIN_SAFE_ZONE <= safe_zone <= MAX_SAFE_ZONE):
            raise IcogenError(
                f"safe_zone 必须在 {MIN_SAFE_ZONE} 至 {MAX_SAFE_ZONE} 之间：{safe_zone}"
            )

        try:
            padding = float(payload.get("padding") or MIN_PADDING)
        except (TypeError, ValueError) as exc:
            raise IcogenError(
                f"padding 必须是数字：{payload.get('padding')}"
            ) from exc
        if not (MIN_PADDING <= padding <= MAX_PADDING):
            raise IcogenError(
                f"padding 必须在 {MIN_PADDING} 至 {MAX_PADDING} 之间：{padding}"
            )

        bitmap_format = str(payload.get("bitmap_format") or "bmp")
        if bitmap_format not in BITMAP_FORMATS:
            raise IcogenError(
                f"bitmap_format 仅支持 {' / '.join(BITMAP_FORMATS)}：{bitmap_format}"
            )

        color_text = str(payload.get("background") or "#ffffff").strip().lstrip("#")
        if len(color_text) != 6:
            raise IcogenError(
                f"background 必须是 6 位十六进制颜色：{payload.get('background')}"
            )
        try:
            background = tuple(int(color_text[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError as exc:
            raise IcogenError(
                f"background 不是合法十六进制颜色：{payload.get('background')}"
            ) from exc

        return safe_zone, background, bool(payload.get("opaque_pad")), padding, bitmap_format

    def _handle_update_config(self) -> None:
        """更新对象存储与调度配置。"""
        payload = self._read_json()

        storage = self.config.storage

        for key in ("endpoint", "region", "bucket", "prefix"):
            if key in payload:
                setattr(storage, key, str(payload[key]).strip())
        for key in ("path_style", "use_ssl"):
            if key in payload:
                setattr(storage, key, bool(payload[key]))
        if "timeout" in payload:
            storage.timeout = _safe_int(payload["timeout"], storage.timeout, low=1, high=300)

        # 凭据特殊处理：空字符串表示「保持原值」，避免前端因脱敏回写而清空密钥
        if "access_key" in payload and str(payload["access_key"]).strip():
            storage.access_key = str(payload["access_key"]).strip()
        if "secret_key" in payload and str(payload["secret_key"]).strip():
            storage.secret_key = str(payload["secret_key"]).strip()

        if "enabled" in payload:
            storage.enabled = bool(payload["enabled"])

        scheduler = self.config.scheduler
        if "scheduler_enabled" in payload:
            scheduler.enabled = bool(payload["scheduler_enabled"])
        for key, attr, low, high in (
            ("interval_minutes", "interval_minutes", 1, 1440),
            ("batch_size", "batch_size", 1, 1000),
            ("max_retries", "max_retries", 0, 10),
        ):
            if key in payload:
                setattr(scheduler, attr, _safe_int(payload[key], getattr(scheduler, attr), low, high))

        self.config.validate()
        save_config(self.config)

        # 配置变更后重启调度，使新间隔立即生效
        if scheduler.enabled:
            self.queue.restart()

        self._send_json({
            "ok": True,
            "message": "配置已保存",
            "storage": storage.public_dict(),
            "scheduler": {
                "enabled": scheduler.enabled,
                "interval_minutes": scheduler.interval_minutes,
                "batch_size": scheduler.batch_size,
                "max_retries": scheduler.max_retries,
            },
        })

    def _handle_manual_upload(self) -> None:
        """手动上传：指定 artifact_id 或上传全部待传。"""
        payload = self._read_json()

        if payload.get("artifact_id"):
            result = self.queue.upload_one(str(payload["artifact_id"]))
        else:
            result = self.queue.upload_pending(payload.get("limit"))

        self._send_json(result)

    def _handle_scheduler(self) -> None:
        """启动/停止定时上传。"""
        payload = self._read_json()
        action = str(payload.get("action") or "status").lower()

        if action == "start":
            self.config.scheduler.enabled = True
            started = self.queue.start()
            message = "定时上传已启动" if started else "定时上传已在运行"
        elif action == "stop":
            self.config.scheduler.enabled = False
            self.queue.stop()
            message = "定时上传已停止"
        else:
            message = "当前状态"

        save_config(self.config)
        self._send_json({
            "ok": True,
            "message": message,
            "enabled": self.config.scheduler.enabled,
            "queue": self.queue.status(),
        })

    def _handle_test_storage(self) -> None:
        """测试对象存储连通性。"""
        from .uploader import S3Uploader

        try:
            self.config.storage.validate()
            uploader = S3Uploader(self.config.storage)
        except IcogenError as exc:
            self._send_json({"ok": False, "message": exc.message})
            return

        result = uploader.verify_connection()
        self._send_json(result)

    def _handle_site_assets(self) -> None:
        """生成站点物料包 ZIP。"""
        payload = self._read_json()

        encoded = str(payload.get("image_base64") or "")
        if not encoded:
            raise IcogenError("缺少 image_base64 字段")
        if "," in encoded and encoded.strip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise IcogenError(f"image_base64 解码失败：{exc}") from exc

        image = decode_image(raw, filename=str(payload.get("filename") or "upload"))
        sizes = self._parse_sizes({}, payload)
        fit_mode = str(payload.get("fit_mode") or "cover")
        if fit_mode not in ("cover", "contain", "maskable", "stretch"):
            raise IcogenError(f"未知的 fit_mode：{fit_mode}")
        safe_zone, background, opaque_pad, padding, _bmp = self._parse_quality_params(payload)

        # 源图本身是 SVG 时把原始字节透传进物料包（favicon.svg，借鉴要点 C1）；
        # 调用方也可显式传入既有 manifest.json 文本，只合并 icons 条目（要点 A3）。
        # 物料包内 favicon.ico 保持 BMP 帧（兼容优先），bitmap_format 不透传。
        source_svg = raw if str(payload.get("filename") or "").lower().endswith(".svg") else None
        existing_manifest = payload.get("existing_manifest")
        if existing_manifest is not None and not isinstance(existing_manifest, str):
            existing_manifest = None

        payload_bytes = build_site_assets_zip(
            image,
            ico_sizes=sizes,
            fit_mode=fit_mode,
            include_maskable=bool(payload.get("include_maskable")),
            name=str(payload.get("name") or "My App"),
            theme_color=str(payload.get("theme_color") or "#ffffff"),
            safe_zone=safe_zone,
            background=background,
            opaque_pad=opaque_pad,
            padding=padding,
            source_svg=source_svg,
            existing_manifest=existing_manifest,
        )

        self._send_binary(payload_bytes, "application/zip", "site-assets.zip", download=True)

    def _handle_bundle(self) -> None:
        """把各尺寸独立 ICO 打包为 ZIP（对应桌面版「保存全部尺寸」）。

        与桌面版 `save_bundle()` 保持同一产物形态：ZIP 内每个文件名为
        ``icon_{宽}x{高}.ico``，按尺寸升序排列。

        旧版前台该按钮在浏览器端用合并 ICO 冒充 ZIP（为回避引入 JSZip），
        按钮名与产物不符。服务端本来就有 zipfile 与完整的尺寸流水线，
        由服务端打包是零新增依赖的正确解法，两端也自然保持一致。
        """
        payload = self._read_json()

        encoded = str(payload.get("image_base64") or "")
        if not encoded:
            raise IcogenError("缺少 image_base64 字段")
        if "," in encoded and encoded.strip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise IcogenError(f"image_base64 解码失败：{exc}") from exc

        image = decode_image(raw, filename=str(payload.get("filename") or "upload"))
        sizes = self._parse_sizes({}, payload)
        fit_mode = str(payload.get("fit_mode") or "cover")
        if fit_mode not in ("cover", "contain", "maskable", "stretch"):
            raise IcogenError(f"未知的 fit_mode：{fit_mode}")
        safe_zone, background, opaque_pad, padding, bitmap_format = self._parse_quality_params(payload)

        try:
            bundle = build_ico_bundle(
                image, sizes, fit_mode=fit_mode, safe_zone=safe_zone,
                background=background, opaque_pad=opaque_pad, padding=padding,
                bitmap_format=bitmap_format,
            )
        except IcogenError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IcogenError(f"打包全部尺寸失败：{exc}") from exc

        if not bundle:
            raise IcogenError("没有可打包的尺寸")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for (width, height), data in sorted(bundle.items()):
                archive.writestr(f"icon_{width}x{height}.ico", data)

        self._send_binary(buffer.getvalue(), "application/zip", "icons.zip", download=True)


def _safe_int(value: Any, default: int, low: int = 1, high: int = 100000) -> int:
    """安全整数解析，越界回退默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class IcoGenServer:
    """服务封装：负责启动、关闭与令牌管理。"""

    def __init__(self, config: AppConfig, store: LocalStore):
        self.config = config
        self.store = store
        self.queue = UploadQueue(store, config)
        self.admin_token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def require_auth(self) -> bool:
        """管理接口始终需要令牌。

        这里刻意**不**因为「只监听 127.0.0.1」就放宽：同机任意进程、以及
        用户浏览器里任意网页发出的跨站请求（CSRF / DNS rebinding）都能打到
        回环地址。回环不是鉴权边界，因此配置、上传、下载一律要求 Bearer 令牌。
        前台的 ``/api/generate`` 不需要令牌，它只写本地磁盘、不改配置。
        """
        return True

    def start(self) -> Tuple[str, int]:
        """启动服务，返回 (host, port)。"""
        handler = type("BoundHandler", (IcoGenHandler,), {
            "store": self.store,
            "queue": self.queue,
            "config": self.config,
            "admin_token": self.admin_token,
            "require_auth": self.require_auth,
            # 路由表存在类上，值是未绑定的函数；分发时由 do_GET 传 self。
            # BaseHTTPRequestHandler 每个连接新建一个实例，若在 __init__ 里
            # 注册会反复重建表，因此放类体 dict。
            "_GET_ROUTES": {
                "/api/health": IcoGenHandler._handle_health,
                "/api/presets": IcoGenHandler._handle_presets,
                "/api/site-assets/describe": IcoGenHandler._handle_site_assets_describe,
                "/api/artifacts": IcoGenHandler._handle_artifacts,
                "/api/config": IcoGenHandler._handle_config_get,
                "/api/queue/status": IcoGenHandler._handle_queue_status,
            },
        })

        self._httpd = ThreadingHTTPServer((self.config.host, self.config.port), handler)
        self._httpd.daemon_threads = True
        host, port = self._httpd.server_address[0], self._httpd.server_address[1]

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="icogen-http", daemon=True)
        self._thread.start()

        self.queue.start()
        LOGGER.info("服务已启动 http://%s:%d （管理鉴权：%s）", host, port, "开启" if self.require_auth else "关闭")
        return host, port

    def stop(self) -> None:
        """关闭服务与调度线程。"""
        self.queue.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        LOGGER.info("服务已关闭")


def serve_forever(config: AppConfig, store: LocalStore) -> None:
    """阻塞式启动（供 CLI 使用）。"""
    server = IcoGenServer(config, store)
    host, port = server.start()
    print(f"IcoGenerator 后台服务已启动：http://{host}:{port}")
    print(f"管理员令牌：{server.admin_token}")
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.stop()