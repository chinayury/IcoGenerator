"""支持 ``python -m icogenerator`` 直接运行命令行入口。

等价于 ``icogen`` 命令；便于未安装脚本或 PATH 未配置的环境使用。
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
