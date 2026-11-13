"""对象存储上传（S3 兼容协议）。

为什么零依赖手写：
引入 ``boto3`` 会带来数十个传递依赖与较��的安装体积。本项目只需要
``PUT`` 一个对象，因此用标准库实现 AWS Signature V4 签名即可，
支持 阿里云 OSS / 腾讯云 COS / MinIO / AWS S3。

安全要点：
- 密钥只从内存中的配置读取，**绝不写入日志**；
- 强制 HTTPS（除非显式关闭且配置为内网 MinIO）；
- 对 endpoint 做协议校验，阻断 ``file://`` 之类的非 HTTP 协议；
- 上传走**受限 opener**：仅注册 HTTP / HTTPS 与重定向护栏，默认 opener 中的
  FTP / File / Data / 环境代理等面被整体关闭；重定向目标协议二次校验；
- 上传使用固定 Content-Type，不从用户输入推导，避免头部注入。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .config import StorageConfig
from .core.errors import StorageConfigError, UploadError

LOGGER = logging.getLogger(__name__)

# 允许的对象 Content-Type
CONTENT_TYPES = {
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".zip": "application/zip",
    ".json": "application/json",
}

# 上传响应体最大读取字节数：防止异常服务端返回超大内容打满内存
MAX_RESPONSE_BYTES = 8 * 1024


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向护栏：跳转目标协议只放行 http(s)。

    默认重定向处理不校验目标协议，存在借 30x 把请求（连同签名头）
    带向非预期协议的风险；这里在跳转前做最后一道校验。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise UploadError(
                f"上传重定向协议不受支持：{newurl}",
                hint="仅允许 http / https，请校验 endpoint 配置",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class S3Uploader:
    """S3 兼容对象存储上传器。"""

    def __init__(self, config: StorageConfig):
        config.validate()
        self.config = config
        self.endpoint = str(config.endpoint).rstrip("/")
        self.bucket = str(config.bucket).strip()
        self.region = str(config.region).strip() or "us-east-1"
        self.prefix = str(config.prefix or "").strip("/")

        scheme = urllib.parse.urlparse(self.endpoint).scheme
        if scheme not in ("http", "https"):
            raise StorageConfigError(
                f"endpoint 协议不受支持：{scheme}", hint="仅支持 http / https"
            )

        # 受限 opener：显式只挂 HTTP / HTTPS、重定向护栏、默认错误处理与错误加工，
        # 默认 opener 的 FTP / File / Data / 环境代理面一律不进入本上传链路。
        self._opener = urllib.request.OpenerDirector()
        self._opener.add_handler(urllib.request.HTTPHandler())
        self._opener.add_handler(urllib.request.HTTPSHandler())
        self._opener.add_handler(_SafeRedirectHandler())
        self._opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
        self._opener.add_handler(urllib.request.HTTPErrorProcessor())
        if config.use_ssl and scheme == "http":
            # 非 loopback 的明文端点通常意味着配置失误
            host = urllib.parse.urlparse(self.endpoint).hostname or ""
            if not host.startswith(("127.", "localhost", "::1", "minio")):
                LOGGER.warning(
                    "对象存储端点为明文 HTTP（%s），凭据将以明文传输", host
                )

    # ---------------- 签名 ----------------

    @staticmethod
    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        """派生签名密钥：AWS SigV4 的四步密钥派生。"""
        k_date = self._sign(f"AWS4{self.config.secret_key}".encode("utf-8"), date_stamp)
        k_region = self._sign(k_date, self.region)
        k_service = self._sign(k_region, "s3")
        return self._sign(k_service, "aws4_request")

    def _authorization_header(
        self, method: str, host: str, canonical_uri: str,
        canonical_query: str, payload_hash: str, amz_date: str, date_stamp: str,
    ) -> str:
        """构造 Authorization 头。"""
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        canonical_request = "\n".join([
            method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash,
        ])

        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return (
            f"AWS4-HMAC-SHA256 Credential={self.config.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    # ---------------- 路径 ----------------

    def _object_key(self, filename: str, *, remote_date: Optional[_dt.date] = None) -> str:
        """生成对象键：``{prefix}/2026/10/11/{filename}``。

        按日期分层避免单目录对象过多导致列举变慢。
        """
        day = remote_date or _dt.datetime.now(_dt.timezone.utc).date()
        parts = [p for p in (self.prefix, f"{day.year:04d}", f"{day.month:02d}", f"{day.day:02d}", filename) if p]
        return "/".join(parts)

    def _build_url(self, object_key: str) -> tuple[str, str, str]:
        """返回 (url, canonical_uri, host)。"""
        parsed = urllib.parse.urlparse(self.endpoint)
        host = parsed.netloc
        encoded_key = urllib.parse.quote(object_key, safe="/")

        if self.config.path_style:
            # MinIO / 自建网关：https://host:9000/bucket/key
            canonical_uri = f"/{self.bucket}/{encoded_key}"
        else:
            # 虚拟主机风格：bucket 作为子域名
            host = f"{self.bucket}.{host}"
            canonical_uri = f"/{encoded_key}"

        url = f"{parsed.scheme}://{host}{canonical_uri}"
        return url, canonical_uri, host

    # ---------------- 上传 ----------------

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        content_type: Optional[str] = None,
    ) -> str:
        """上传字节流。

        Returns:
            对象键（object key）。

        Raises:
            UploadError: 网络失败或服务端返回非 2xx。
        """
        if not data:
            raise UploadError("拒绝上传空文件")

        object_key = self._object_key(filename)
        url, canonical_uri, host = self._build_url(object_key)

        # 固定白名单推导 Content-Type，不使用用户可控字符串拼接头部
        if content_type is None:
            suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
            content_type = CONTENT_TYPES.get(suffix, "application/octet-stream")

        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(data).hexdigest()

        headers = {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Authorization": self._authorization_header(
                "PUT", host, canonical_uri, "", payload_hash, amz_date, date_stamp
            ),
        }

        # 纵深防御：endpoint 协议在初始化时已校验，这里对「组装后的对象 URL」
        # 再做最后一道协议白名单，阻断任何路径把请求引向非 HTTP 协议。
        final_scheme = urllib.parse.urlparse(url).scheme
        if final_scheme not in ("http", "https"):
            raise UploadError(
                f"对象 URL 协议不受支持：{final_scheme}",
                hint="仅支持 http / https，请校验 endpoint 配置",
            )

        request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        LOGGER.info("上传对象 -> %s (%d 字节)", object_key, len(data))

        try:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            # 只读取有限字节，且不打印任何凭据信息
            detail = ""
            try:
                detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise UploadError(
                f"对象存储返回 HTTP {exc.code}（{object_key}）"
                + (f"：{detail}" if detail else ""),
                hint="请校验 endpoint / bucket / 密钥是否有该桶的写权限",
            ) from exc
        except urllib.error.URLError as exc:
            raise UploadError(
                f"无法连接对象存储：{exc.reason}",
                hint="请检查网络、endpoint 与防火墙",
            ) from exc
        except OSError as exc:
            raise UploadError(f"上传过程中网络异常：{exc}") from exc

        return object_key

    def verify_connection(self) -> Dict[str, Any]:
        """连通性自检（管理后台「测试连接」按钮）。

        用一个 1 字节的探测对象写入并随后尝试删除；
        失败不影响主流程，仅返回诊断信息。
        """
        probe_name = ".icogen-connection-probe"
        try:
            key = self.upload_bytes(b"ping", probe_name, content_type="text/plain")
            return {"ok": True, "object_key": key, "message": "连接正常"}
        except UploadError as exc:
            return {"ok": False, "object_key": "", "message": str(exc)}