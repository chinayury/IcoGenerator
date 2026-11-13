# 打包说明

四个入口共用同一个 `icogenerator` 包，CI 产物对应关系：

| 入口 | 产物 |
|---|---|
| 桌面版 GUI | `IcoGenerator-Desktop.exe`（PyInstaller，Windows） |
| 网页版 `/app` | `icogen`（`icogen serve`）/ Docker 镜像 |
| 管理台 `/admin` | 同上 |
| CLI | `icogen`（`icogen convert`）/ Python wheel |

## 本地构建

```bash
# Python 包（wheel + sdist）
python -m pip install build
python -m build

# Windows 桌面版与 CLI（在 Windows 上执行）
python -m pip install ".[dnd]" ttkthemes pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed \
  --name IcoGenerator-Desktop \
  --collect-data icogenerator \
  --collect-all tkinterdnd2 \
  --distpath release/windows \
  --workpath build/pyinstaller-desktop \
  packaging/pyinstaller_desktop.py

pyinstaller --noconfirm --clean --onefile --console \
  --name icogen \
  --collect-data icogenerator \
  --distpath release/windows \
  --workpath build/pyinstaller-cli \
  packaging/pyinstaller_cli.py
```

`--collect-data icogenerator` 用于把 `web/`、`assets/` 等包数据打进可执行文件；
`--collect-all tkinterdnd2` 用于带上拖拽所需的 Tcl/Tk 资源。未安装 `tkinterdnd2` 时，
桌面版仍可运行，但拖拽会降级为点击选择文件。
