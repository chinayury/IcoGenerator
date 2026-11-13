"""上传队列与定时调度。

对应需求：「先存本地 → 管理员配置对象存储后可手动上传 / 定时自动上传，
前端无感知」。

设计要点：
- 前台生成流程只写本地，从不等待网络，因此上传失败不影响用户使用；
- 手动上传与定时上传共用同一队列，避免并发重复上传同一文件；
- 指数退避重试，避免网络抖动时反复打爆接口；
- 上传线程与 HTTP 服务共享状态时使用锁，保证元数据写入不交错。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from .config import AppConfig, StorageConfig
from .core.errors import StorageConfigError, UploadError
from .storage import LocalStore
from .uploader import S3Uploader

LOGGER = logging.getLogger(__name__)


class UploadQueue:
    """线程安全的上传队列。"""

    def __init__(self, store: LocalStore, config: AppConfig):
        self.store = store
        self.config = config
        self._lock = threading.Lock()
        self._inflight = set()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_run: float = 0.0
        self._last_error: str = ""
        self._stats = {"uploaded": 0, "failed": 0, "skipped": 0}

    # ---------------- 状态 ----------------

    def status(self) -> Dict[str, object]:
        """队列状态快照（供管理后台展示）。"""
        with self._lock:
            return {
                "running": self._worker is not None and self._worker.is_alive(),
                "inflight": sorted(self._inflight),
                "last_run_epoch": self._last_run,
                "last_error": self._last_error,
                "stats": dict(self._stats),
            }

    def _uploader_or_none(self) -> Optional[S3Uploader]:
        """仅在启用且配置完整时构造上传器。

        未配置对象存储时返回 None —— 前台完全不受影响。
        """
        storage: StorageConfig = self.config.storage
        if not storage.enabled:
            return None
        if storage.missing_fields():
            LOGGER.warning("对象存储已启用但配置不完整，本轮跳过上传")
            return None
        try:
            return S3Uploader(storage)
        except StorageConfigError as exc:
            LOGGER.error("上传器初始化失败：%s", exc)
            return None

    # ---------------- 上传执行 ----------------

    def upload_one(self, artifact_id: str) -> Dict[str, object]:
        """上传单个产物。

        Returns:
            结果字典：``{"ok": bool, "message": str, "object_key": str}``。
        """
        record = self.store.get_artifact(artifact_id)
        if record is None:
            return {"ok": False, "message": f"产物不存在：{artifact_id}", "object_key": ""}

        if record.upload_status == "uploaded":
            return {"ok": True, "message": "已是最新，无需重复上传", "object_key": record.object_key}

        uploader = self._uploader_or_none()
        if uploader is None:
            self.store.update_status(artifact_id, upload_status="pending", last_error="")
            return {"ok": False, "message": "对象存储未启用或配置不完整", "object_key": ""}

        try:
            data = self.store.read_artifact_bytes(artifact_id)
            object_key = uploader.upload_bytes(data, record.filename)
        except (UploadError, Exception) as exc:  # noqa: BLE001 - 上层统一呈现
            attempts = record.upload_attempts + 1
            max_retries = self.config.scheduler.max_retries
            # 未超过重试上限则退回 pending，等待下一轮重试
            next_status = "failed" if attempts > max_retries else "pending"
            self.store.update_status(
                artifact_id,
                upload_status=next_status,
                upload_attempts=attempts,
                last_error=str(exc),
            )
            with self._lock:
                self._stats["failed"] += 1
                self._last_error = str(exc)
            LOGGER.warning("上传失败（%s/第 %d 次）：%s", record.filename, attempts, exc)
            return {"ok": False, "message": str(exc), "object_key": ""}

        self.store.update_status(
            artifact_id,
            upload_status="uploaded",
            upload_attempts=record.upload_attempts + 1,
            object_key=object_key,
        )
        with self._lock:
            self._stats["uploaded"] += 1
            self._last_error = ""
        LOGGER.info("上传成功：%s -> %s", record.filename, object_key)
        return {"ok": True, "message": "上传成功", "object_key": object_key}

    def upload_pending(self, limit: Optional[int] = None) -> Dict[str, object]:
        """批量上传待处理产物。

        Args:
            limit: 本轮最多上传数量，默认取配置的 batch_size。
        """
        if not self.config.storage.enabled:
            return {"ok": False, "message": "对象存储未启用", "uploaded": 0, "failed": 0}

        batch = limit or self.config.scheduler.batch_size
        pending = self.store.pending_artifacts(limit=batch)
        if not pending:
            return {"ok": True, "message": "没有待上传的产物", "uploaded": 0, "failed": 0}

        uploaded = 0
        failed = 0
        for record in pending:
            with self._lock:
                if record.artifact_id in self._inflight:
                    continue
                self._inflight.add(record.artifact_id)
            try:
                result = self.upload_one(record.artifact_id)
                if result["ok"]:
                    uploaded += 1
                else:
                    failed += 1
            finally:
                with self._lock:
                    self._inflight.discard(record.artifact_id)

        with self._lock:
            self._last_run = time.time()

        return {
            "ok": failed == 0,
            "message": f"上传完成：成功 {uploaded}，失败 {failed}",
            "uploaded": uploaded,
            "failed": failed,
        }

    # ---------------- 后台调度 ----------------

    def _loop(self) -> None:
        """后台线程主循环。

        使用 ``Event.wait`` 而非 ``sleep``，保证 stop() 能立即唤醒退出。
        """
        interval_seconds = max(1, self.config.scheduler.interval_minutes) * 60
        LOGGER.info("定时上传已启动，间隔 %d 分钟", self.config.scheduler.interval_minutes)

        while not self._stop.is_set():
            # 启动后先等一个间隔，避免与手动上传抢占
            if self._stop.wait(interval_seconds):
                break
            try:
                result = self.upload_pending()
                LOGGER.info("定时上传轮次：%s", result["message"])
            except Exception as exc:  # noqa: BLE001 - 线程必须存活
                LOGGER.exception("定时上传轮次异常：%s", exc)

        LOGGER.info("定时上传线程已退出")

    def start(self) -> bool:
        """启动定时上传线程。

        Returns:
            是否成功启动（已运行或未启用时返回 False）。
        """
        if not self.config.scheduler.enabled:
            LOGGER.info("定时上传未启用")
            return False
        if self._worker is not None and self._worker.is_alive():
            return False

        self._stop.clear()
        self._worker = threading.Thread(
            target=self._loop, name="icogen-uploader", daemon=True
        )
        self._worker.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """停止定时上传线程。"""
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        self._worker = None

    def restart(self) -> bool:
        """按最新配置重启调度（管理后台改配置后调用）。"""
        self.stop()
        return self.start()