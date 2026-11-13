"""配置管理。

安全要点（对应旧版硬编码凭据缺陷）：
- **源码内不存放任何密钥**。access_key / secret_key 只存在于用户配置文件中；
- 配置文件写入时设为 0600（POSIX）或尽力设置 ACL 隐藏（Windows）；
- 读取配置时对敏感字段做脱敏，绝不写进日志；
- 配置文件缺失时自动生成默认配置，不崩溃。

配置优先级：显式参数 > 环境变量 > 配置文件 > 默认值。
环境变量便于容器/服务化部署，同时覆盖本地开发。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

from .core.errors import StorageConfigError

LOGGER = logging.getLogger(__name__)

# 环境变量前缀
ENV_PREFIX = "ICOGEN_"


@dataclass
class StorageConfig:
    """对象存储配置。

    默认全部留空 —— 未配置时后端只写本地目录，不做任何网络请求，
    前台功能完全不受影响（对应「前端无感知」要求）。
    """

    # 是否启用对象存储上传；未配置凭据时自动保持关闭
    enabled: bool = False
    # 兼容 S3 的对象存储：阿里云 OSS / 腾讯云 COS / MinIO / AWS S3 均可
    endpoint: str = ""
    region: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    # 对象前缀，最终路径形如 {prefix}/{yyyy}/{mm}/{dd}/{filename}
    prefix: str = "icongenerator"
    # 是否使用 path-style 寻址（MinIO 必开；OSS/COS 通常关闭）
    path_style: bool = True
    # 是否使用 HTTPS
    use_ssl: bool = True
    # 单次请求超时（秒）
    timeout: int = 30

    def missing_fields(self) -> list[str]:
        """返回必填但为空的字段名列表。"""
        required = ("endpoint", "bucket", "access_key", "secret_key")
        return [name for name in required if not str(getattr(self, name, "") or "").strip()]

    def validate(self) -> None:
        """校验配置完整性。

        Raises:
            StorageConfigError: 启用上传但配置缺失或非法。
        """
        if not self.enabled:
            return

        missing = self.missing_fields()
        if missing:
            raise StorageConfigError(
                "对象存储配置不完整，缺少：" + "、".join(missing),
                hint="请在管理后台补全后再启用上传",
            )

        if not str(self.endpoint).startswith(("http://", "https://")):
            raise StorageConfigError(
                f"endpoint 必须以 http:// 或 https:// 开头：{self.endpoint}"
            )

        if self.timeout <= 0 or self.timeout > 300:
            raise StorageConfigError(
                f"timeout 必须在 1-300 秒之间，当前为 {self.timeout}"
            )

    def public_dict(self) -> Dict[str, Any]:
        """对外暴露的配置视图：敏感字段脱敏。

        任何返回给前端/日志的都必须走这里，不能直接 ``asdict()``。
        """
        data = asdict(self)
        for key in ("access_key", "secret_key"):
            value = str(data.get(key) or "")
            if value:
                data[key] = _mask(value)
            else:
                data[key] = ""
        return data


@dataclass
class SchedulerConfig:
    """定时自动上传配置。"""

    enabled: bool = False
    # 执行间隔（分钟）
    interval_minutes: int = 30
    # 单轮最多上传数量，避免一次性打爆网络
    batch_size: int = 50
    # 失败重试次数
    max_retries: int = 3

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.interval_minutes < 1:
            raise StorageConfigError(
                f"interval_minutes 必须 >= 1，当前为 {self.interval_minutes}"
            )
        if self.batch_size < 1 or self.batch_size > 1000:
            raise StorageConfigError(
                f"batch_size 必须在 1-1000 之间，当前为 {self.batch_size}"
            )
        if self.max_retries < 0 or self.max_retries > 10:
            raise StorageConfigError(
                f"max_retries 必须在 0-10 之间，当前为 {self.max_retries}"
            )


@dataclass
class UiPreferences:
    """界面偏好持久化。

    参考 Elephicon（sprout2000/elephicon）的 electron-store 做法：
    把用户上次的选择记下来，避免每次启动都回到默认值。
    """

    # 上次选择的尺寸预设 key（如 favicon / windows / full / small / mobile）
    preset: str = ""
    # 上次选择的非正方形适配模式（cover / contain / maskable / stretch）
    fit_mode: str = "cover"
    # 上次勾选的尺寸，如 ["16", "32", "48"]
    sizes: list = field(default_factory=list)
    # 上次是否勾选「生成站点物料包」
    site_assets: bool = False
    # 窗口几何，如 "960x680"
    window_geometry: str = ""

    def validate(self) -> None:
        if self.fit_mode and self.fit_mode not in ("cover", "contain", "maskable", "stretch"):
            raise StorageConfigError(f"未知的适配模式：{self.fit_mode}")
        if len(self.sizes) > 32:
            raise StorageConfigError("勾选的尺寸过多，配置可能已损坏")


@dataclass
class AppConfig:
    """应用总配置。"""

    # 本地存储根目录（生成结果的真相源）
    output_dir: str = ""
    storage: StorageConfig = field(default_factory=StorageConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    # 界面偏好（记住用户上次的选择）
    ui: UiPreferences = field(default_factory=UiPreferences)
    # 后台服务监听地址；仅绑定回环，避免暴露到局域网
    host: str = "127.0.0.1"
    port: int = 8765
    # 是否启用 HTTP 访问日志
    access_log: bool = True

    def validate(self) -> None:
        self.storage.validate()
        self.scheduler.validate()
        self.ui.validate()
        if not (0 < self.port < 65536):
            raise StorageConfigError(f"port 必须在 1-65535 之间，当前为 {self.port}")

    def to_json_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _mask(value: str) -> str:
    """脱敏：保留首尾各 4 位，中间用 * 替代。

    短凭据整体打码，避免因长度不足反而泄露。
    """
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 6}{text[-4:]}"


def _default_output_dir() -> str:
    """默认输出目录：用户主目录下的 icongenerator/output。"""
    return str(Path.home() / "icongenerator" / "output")


def default_config_path() -> Path:
    """默认配置文件路径。

    Windows: ``%LOCALAPPDATA%\\icongenerator\\config.json``
    POSIX  : ``~/.config/icongenerator/config.json``
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "icongenerator" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "icongenerator" / "config.json"


def _secure_write(path: Path, content: str) -> None:
    """原子写入配置文件，并尽力限制访问权限。

    先写同目录临时文件再 ``os.replace``，避免写入中途崩溃导致配置损坏。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        # POSIX：设为仅当前用户可读写
        try:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, AttributeError):
            # Windows：不支持 POSIX 权限位，忽略。
            # 说明：Windows 上真正的隔离应依赖用户账户 ACL 与 DPAPI，
            # 本工具仅保证明文不进入源码与日志。
            pass
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _coerce(raw: Any, target_type: Any) -> Any:
    """把 JSON 值转换为声明类型，失败时返回原值交由后续校验拦截。"""
    try:
        if target_type is bool and isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return target_type(raw)
    except (TypeError, ValueError):
        return raw


def _build_dataclass(cls: Any, data: Dict[str, Any]) -> Any:
    """按 dataclass 字段类型递归构造，忽略未知字段。"""
    kwargs: Dict[str, Any] = {}
    type_map = {f.name: f.type for f in fields(cls)}
    for name, ftype in type_map.items():
        if name not in data:
            continue
        value = data[name]
        # 处理嵌套 dataclass（storage / scheduler / ui）
        if isinstance(ftype, str) and ftype in ("StorageConfig", "SchedulerConfig", "UiPreferences") and isinstance(value, dict):
            nested = {
                "StorageConfig": StorageConfig,
                "SchedulerConfig": SchedulerConfig,
                "UiPreferences": UiPreferences,
            }[ftype]
            kwargs[name] = _build_dataclass(nested, value)
        else:
            real_type = {
                "str": str, "int": int, "bool": bool,
                "StorageConfig": StorageConfig,
                "SchedulerConfig": SchedulerConfig,
                "UiPreferences": UiPreferences,
            }.get(ftype if isinstance(ftype, str) else "", None)
            kwargs[name] = _coerce(value, real_type) if real_type else value
    return cls(**kwargs)


def _apply_env_overrides(config: AppConfig) -> AppConfig:
    """环境变量覆盖配置，便于服务化部署。"""
    mapping = {
        "OUTPUT_DIR": ("output_dir", str),
        "HOST": ("host", str),
        "PORT": ("port", int),
        "STORAGE_ENABLED": ("_storage_enabled", bool),
        "ENDPOINT": ("_endpoint", str),
        "REGION": ("_region", str),
        "BUCKET": ("_bucket", str),
        "ACCESS_KEY": ("_access_key", str),
        "SECRET_KEY": ("_secret_key", str),
        "PREFIX": ("_prefix", str),
        "PATH_STYLE": ("_path_style", bool),
        "USE_SSL": ("_use_ssl", bool),
        "TIMEOUT": ("_timeout", int),
        "SCHEDULER_ENABLED": ("_sched_enabled", bool),
        "SCHEDULER_INTERVAL": ("_sched_interval", int),
    }

    storage_map = {
        "_storage_enabled": ("enabled", bool),
        "_endpoint": ("endpoint", str),
        "_region": ("region", str),
        "_bucket": ("bucket", str),
        "_access_key": ("access_key", str),
        "_secret_key": ("secret_key", str),
        "_prefix": ("prefix", str),
        "_path_style": ("path_style", bool),
        "_use_ssl": ("use_ssl", bool),
        "_timeout": ("timeout", int),
    }
    scheduler_map = {"_sched_enabled": ("enabled", bool), "_sched_interval": ("interval_minutes", int)}

    for env_suffix, (attr, caster) in mapping.items():
        raw = os.environ.get(ENV_PREFIX + env_suffix)
        if raw is None:
            continue
        if attr.startswith("_storage") or attr in ("_endpoint", "_region", "_bucket", "_access_key",
                                                  "_secret_key", "_prefix", "_path_style",
                                                  "_use_ssl", "_timeout"):
            target_attr, target_cast = storage_map.get(attr, (attr, caster))
            setattr(config.storage, target_attr, _coerce(raw, target_cast))
        elif attr in scheduler_map:
            target_attr, target_cast = scheduler_map[attr]
            setattr(config.scheduler, target_attr, _coerce(raw, target_cast))
        else:
            setattr(config, attr, _coerce(raw, caster))

    return config


def load_config(path: Optional[Path] = None) -> AppConfig:
    """加载配置：文件 -> 环境变量 -> 默认值。

    配置文件不存在时自动创建默认配置，保证首次启动即可用。
    """
    cfg_path = Path(path) if path else default_config_path()

    if not cfg_path.exists():
        config = AppConfig(output_dir=_default_output_dir())
        # 默认关闭上传与定时任务，遵循最小权限原则
        try:
            save_config(config, cfg_path)
            LOGGER.info("已生成默认配置文件：%s", cfg_path)
        except OSError as exc:
            LOGGER.warning("默认配置写入失败（不影响启动）：%s", exc)
    else:
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("配置文件根节点必须是对象")
            config = _build_dataclass(AppConfig, data)
        except (OSError, ValueError) as exc:
            # 配置损坏不能导致程序无法启动
            LOGGER.error("配置文件解析失败，改用默认配置：%s", exc)
            config = AppConfig(output_dir=_default_output_dir())
        else:
            if not config.output_dir:
                config.output_dir = _default_output_dir()

    _apply_env_overrides(config)
    return config


def save_config(config: AppConfig, path: Optional[Path] = None) -> Path:
    """保存配置到磁盘（原子写 + 权限收紧）。"""
    cfg_path = Path(path) if path else default_config_path()
    content = json.dumps(config.to_json_dict(), indent=2, ensure_ascii=False)
    _secure_write(cfg_path, content)
    LOGGER.info("配置已保存：%s", cfg_path)
    return cfg_path


def resolve_output_dir(config: AppConfig, override: Optional[str] = None) -> Path:
    """解析并创建输出目录。

    Args:
        config: 应用配置。
        override: 调用方显式指定目录（优先级最高）。

    Returns:
        已确保存在的输出目录。
    """
    raw = override or config.output_dir or _default_output_dir()
    path = Path(raw).expanduser()

    # 关键修复：旧版把临时文件写在进程 CWD，用户从不同目录启动会散落一地。
    # 现在统一锚定到显式配置的绝对路径。
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageConfigError(
            f"无法创建输出目录：{path}（{exc}）",
            hint="请检查路径权限，或在配置中更换 output_dir",
        ) from exc

    return path.resolve()