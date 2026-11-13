"""统一异常体系。

设计原则：调用方只需捕获 ``IcogenError`` 即可拿到所有可预期的业务错误，
其余异常视为程序缺陷，不应在正常流程中被吞掉。
"""


class IcogenError(Exception):
    """所有可预期业务错误的基类。"""

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.message = message
        # hint 面向终端用户，说明「怎么解决」，而非复述错误
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}（{self.hint}）"
        return self.message


class UnsupportedImageError(IcogenError):
    """输入文件不是受支持的图片格式。"""


class ImageDecodeError(IcogenError):
    """文件扩展名像图片，但内容无法解码（损坏文件 / 伪装扩展名）。"""


class ImageTooLargeError(IcogenError):
    """图片像素总量超出安全上限，拒绝解码以规避解压炸弹。"""


class NoSizeSelectedError(IcogenError):
    """未选择任何 ICO 尺寸。"""


class StorageError(IcogenError):
    """本地存储读写失败。"""


class StorageConfigError(IcogenError):
    """对象存储配置缺失或非法。"""


class UploadError(IcogenError):
    """上传对象存储失败。"""