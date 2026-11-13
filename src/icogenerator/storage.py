"""本地存储层。

本模块是生成结果的**真相源**：所有 ICO 先落本地，再由上传队列异步推送。
即使对象存储未配置或上传失败，本地文件始终完整可用。

安全修复点：
- 文件名净化，杜绝 ``../`` 路径穿越写入任意目录；
- 原子写入（临时文件 + replace），避免写到一半产生损坏 ICO；
- 路径逃逸检查：任何解析后越出根目录的路径直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core.errors import StorageError

LOGGER = logging.getLogger(__name__)

# 允许的扩展名白名单：任何非白名单扩展一律拒绝，防止写出可执行文件
ALLOWED_EXTENSIONS = (".ico", ".png", ".zip", ".json")

# 记录上限，防止元数据文件无限膨胀
MAX_METADATA_ENTRIES = 5000


def sanitize_filename(name: str, *, default: str = "icon.ico") -> str:
    """净化文件名，阻断路径穿越。

    旧版 ``download_all`` 直接用 ``os.path.join(temp_dir, filename)`` 拼接，
    filename 若可控即可写出目录外。净化后只保留安全字符。

    Args:
        name: 原始文件名。
        default: 净化后为空时使用的默认名。

    Returns:
        仅含 ``[A-Za-z0-9._-]`` 的文件名（不含目录分隔符）。
    """
    # 先截断目录部分：兼容 Windows 的 \ 与 POSIX 的 /
    base = os.path.basename(str(name).replace("\\", "/")).strip()

    # 去掉控制字符与路径分隔符
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = base.replace("/", "_").replace("\\", "_")

    # 只保留安全字符，其余替换为下划线
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)

    # 防止 "." / ".." 这类特殊名
    if base in ("", ".", ".."):
        base = default

    # 保留扩展名白名单
    stem, ext = os.path.splitext(base)
    if ext.lower() not in ALLOWED_EXTENSIONS:
        ext = ".ico" if default.endswith(".ico") else ""
    if not stem:
        stem = default.rsplit(".", 1)[0] or "icon"

    # 长度限制（兼容 Windows MAX_PATH 与常见文件系统限制）
    stem = stem[:80]
    return f"{stem}{ext}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节：先写同目录临时文件，再 replace。

    避免进程崩溃/磁盘满时留下半截 ICO 文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # 确保数据真正落盘，降低 replace 后文件损坏概率
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ArtifactRecord:
    """单个生成产物的元数据。"""

    artifact_id: str
    filename: str
    size: int
    sha256: str
    created_at: str
    source_name: str = ""
    fit_mode: str = "cover"
    sizes: List[str] = field(default_factory=list)
    # 上传状态：pending / uploaded / failed / disabled
    upload_status: str = "pending"
    upload_attempts: int = 0
    last_error: str = ""
    uploaded_at: str = ""
    object_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactRecord":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass 内部约定
        return cls(**{k: v for k, v in data.items() if k in known})


class LocalStore:
    """本地制品仓库。

    目录结构::

        <root>/
            icons/      生成的 ICO / PNG
            bundles/    打包的 ZIP
            .icogen/    元数据（index.json）
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.icons_dir = self.root / "icons"
        self.bundles_dir = self.root / "bundles"
        self.meta_dir = self.root / ".icogen"
        self.index_path = self.meta_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """创建目录结构；权限不足时抛出可读错误而非裸 PermissionError。"""
        try:
            for directory in (self.icons_dir, self.bundles_dir, self.meta_dir):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"无法初始化本地存储目录：{self.root}（{exc}）",
                hint="请更换 output_dir 或检查目录权限",
            ) from exc

    def _safe_path(self, base: Path, filename: str) -> Path:
        """把文件名解析为 base 下的绝对路径，并阻止越界。

        这是路径穿越的最后一道防线：即便 sanitize_filename 被绕过，
        解析后的路径不位于 base 之内就拒绝。
        """
        safe_name = sanitize_filename(filename)
        candidate = (base / safe_name).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError as exc:
            LOGGER.warning("拒绝越界路径访问：%s", filename)
            raise StorageError(f"非法的文件路径：{filename}") from exc
        return candidate

    # ---------------- 元数据 ----------------

    def _read_index(self) -> List[ArtifactRecord]:
        if not self.index_path.exists():
            return []
        try:
            raw = self.index_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
            if not isinstance(data, list):
                raise ValueError("元数据格式错误")
            return [ArtifactRecord.from_dict(item) for item in data if isinstance(item, dict)]
        except (OSError, ValueError) as exc:
            # 元数据损坏不应阻断使用：备份后重建
            LOGGER.error("元数据损坏，已备份并重建：%s", exc)
            try:
                if self.index_path.exists():
                    shutil.copy2(
                        self.index_path,
                        self.meta_dir / f"index.corrupt-{int(time.time())}.json",
                    )
            except OSError:
                pass
            return []

    def _write_index(self, records: List[ArtifactRecord]) -> None:
        # 控制文件体积：保留最近 N 条
        trimmed = records[-MAX_METADATA_ENTRIES:]
        payload = json.dumps([r.to_dict() for r in trimmed], indent=2, ensure_ascii=False)
        _atomic_write_bytes(self.index_path, payload.encode("utf-8"))

    # ---------------- 写入 ----------------

    def save_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        source_name: str = "",
        fit_mode: str = "cover",
        sizes: Optional[List[str]] = None,
    ) -> ArtifactRecord:
        """保存一个产物并登记元数据。

        Args:
            data: 文件内容。
            filename: 期望文件名（会被净化）。
            source_name: 源图文件名，仅作记录。
            fit_mode: 适配模式记录。
            sizes: 包含的尺寸列表记录。

        Returns:
            产物记录。

        Raises:
            StorageError: 写入失败或内容为空。
        """
        if not data:
            raise StorageError("拒绝保存空文件", hint="生成结果异常，请重新生成")

        safe_name = sanitize_filename(filename)
        target = self._safe_path(self.icons_dir, safe_name)

        # 同名冲突：追加短哈希，避免静默覆盖用户先前结果
        if target.exists():
            stem, ext = target.stem, target.suffix
            digest = hashlib.sha256(data).hexdigest()[:8]
            target = self._safe_path(self.icons_dir, f"{stem}-{digest}{ext}")

        try:
            _atomic_write_bytes(target, data)
        except OSError as exc:
            raise StorageError(f"写入文件失败：{target.name}（{exc}）") from exc

        record = ArtifactRecord(
            artifact_id=uuid.uuid4().hex[:16],
            filename=target.name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=_utc_now(),
            source_name=sanitize_filename(source_name, default=""),
            fit_mode=fit_mode,
            sizes=list(sizes or []),
            upload_status="pending",
        )

        records = self._read_index()
        records.append(record)
        self._write_index(records)
        LOGGER.info("已保存制品 %s (%d 字节)", record.filename, record.size)
        return record

    def save_bundle(
        self,
        data: bytes,
        *,
        base_name: str = "icons",
    ) -> ArtifactRecord:
        """保存 ZIP 打包文件到 bundles 目录。"""
        if not data:
            raise StorageError("拒绝保存空压缩包")

        name = sanitize_filename(f"{base_name}.zip", default="icons.zip")
        target = self._safe_path(self.bundles_dir, name)
        if target.exists():
            stem, ext = target.stem, target.suffix
            target = self._safe_path(
                self.bundles_dir, f"{stem}-{int(time.time())}{ext}"
            )

        try:
            _atomic_write_bytes(target, data)
        except OSError as exc:
            raise StorageError(f"写入压缩包失败：{target.name}（{exc}）") from exc

        record = ArtifactRecord(
            artifact_id=uuid.uuid4().hex[:16],
            filename=target.name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=_utc_now(),
            source_name="",
            fit_mode="bundle",
            upload_status="pending",
        )
        records = self._read_index()
        records.append(record)
        self._write_index(records)
        LOGGER.info("已保存打包文件 %s (%d 字节)", record.filename, record.size)
        return record

    # ---------------- 读取 ----------------

    def list_artifacts(self, *, limit: int = 100) -> List[ArtifactRecord]:
        """列出产物，按创建时间倒序。"""
        records = self._read_index()
        records.reverse()
        return records[: max(1, limit)]

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactRecord]:
        for record in self._read_index():
            if record.artifact_id == artifact_id:
                return record
        return None

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        """按 id 读取产物字节。"""
        record = self.get_artifact(artifact_id)
        if record is None:
            raise StorageError(f"产物不存在或已删除：{artifact_id}")

        base = self.bundles_dir if record.fit_mode == "bundle" else self.icons_dir
        path = self._safe_path(base, record.filename)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"读取产物失败：{record.filename}（{exc}）") from exc

    def pending_artifacts(self, limit: int = 50) -> List[ArtifactRecord]:
        """取出待上传产物（供上传队列消费）。"""
        records = [r for r in self._read_index() if r.upload_status == "pending"]
        return records[: max(1, limit)]

    # ---------------- 状态更新 ----------------

    def update_status(
        self,
        artifact_id: str,
        *,
        upload_status: str,
        upload_attempts: Optional[int] = None,
        last_error: str = "",
        object_key: str = "",
    ) -> Optional[ArtifactRecord]:
        """更新上传状态。"""
        records = self._read_index()
        for record in records:
            if record.artifact_id != artifact_id:
                continue

            record.upload_status = upload_status
            if upload_attempts is not None:
                record.upload_attempts = upload_attempts
            record.last_error = last_error[:500]  # 防止超长错误信息撑爆元数据
            if upload_status == "uploaded":
                record.uploaded_at = _utc_now()
                record.object_key = object_key
                record.last_error = ""
            self._write_index(records)
            return record
        return None

    def stats(self) -> Dict[str, Any]:
        """统计信息（供管理后台展示）。"""
        records = self._read_index()
        total_bytes = sum(r.size for r in records)
        by_status: Dict[str, int] = {}
        for record in records:
            by_status[record.upload_status] = by_status.get(record.upload_status, 0) + 1
        return {
            "root": str(self.root),
            "count": len(records),
            "total_bytes": total_bytes,
            "by_status": by_status,
        }