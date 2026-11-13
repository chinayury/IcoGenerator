"""IcoGenerator —— 多尺寸 ICO 图标生成器。

设计原则：**核心逻辑与界面彻底分离**。

    core/     纯逻辑：解码、规范化、ICO 编码。零 GUI、零网络、零磁盘。
    config    配置与凭据管理（凭据不在源码中）
    storage   本地存储（生成结果的真相源）
    uploader  S3 兼容对象存储上传（标准库实现 SigV4）
    queue     上传队列与定时调度
    server    本地 HTTP 后台服务 + 管理控制台
    desktop   Tkinter 桌面版
    web       网页版前端

桌面版与网页版共用同一套 core，保证两端生成结果完全一致。
"""

__version__ = "0.1.1"
__all__ = ["__version__"]