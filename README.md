# IcoGenerator

多尺寸 ICO 图标生成器。把常见图片（JPG / PNG / GIF / BMP / WebP / TIFF / ICO / SVG）转换为包含多个分辨率的 ICO 文件，适用于网站 favicon 与应用程序图标。

提供四套入口，共用同一套核心引擎：

| 形态 | 启动方式 | 适用场景 |
|---|---|---|
| 桌面版 GUI | `icogen desktop` | 单机日常使用，支持拖拽 |
| 本地网页版 | `icogen serve` 后访问 `/app` | 浏览器操作、批量处理 |
| 管理控制台 | `icogen serve` 后访问 `/admin` | 管理员配置对象存储、查看产物 |
| 命令行 | `icogen convert` | 服务器、脚本、CI 流水线 |

## 核心特性

- **一次生成多尺寸 ICO**，16/24/32/48/64/128/256 可任意组合
- **非正方形图片不变形**：提供居中裁切（cover）、留边（contain）、PWA 安全区（maskable）、拉伸（stretch）四种策略；安全区比例与留边颜色均可调
- **分级质量提示**：源图过小、无透明通道、逼近解码上限、体积超预算，按级别给出可操作说明
- **一站式站点物料包**：一次产出 ICO、apple-touch-icon、android-chrome、maskable 变体、mstile、manifest、browserconfig、HTML 片段与自说明清单
- **本地优先**：所有结果先落本地目录，不配置对象存储也能完整使用
- **对象存储可选**：支持 S3 兼容存储（阿里云 OSS / 腾讯云 COS / MinIO / AWS S3），由管理员手动或定时上传，前台用户无感知
- **依赖极少**：运行时只需 Pillow 一个第三方库；S3 签名用标准库实现。SVG 输入为可选增强（装 cairosvg 才启用，未装则明确提示）

## 快速开始

```bash
pip install -r requirements.txt      # 安装依赖
python -m icogenerator desktop       # 启动桌面版（等价于 python -m icogenerator.cli desktop）
```

或者安装为命令行工具：

```bash
pip install -e .
icogen desktop     # 桌面版
icogen serve       # 网页版 + 管理控制台
icogen info        # 查看当前配置
icogen doctor      # 运行前环境自检
```

命令行预演（不落盘，只报告体积与警告）：

```bash
icogen convert logo.png --dry-run -s 16,32,48,64,128,256
icogen convert logo.png --fit maskable --safe-zone 0.6 --background "#ffffff"
```

详细说明见 [部署教程](docs/02-环境部署教程.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [01-项目介绍与架构](docs/01-项目介绍与架构.md) | 项目概述、模块结构、分层原则、技术选型 |
| [02-环境部署教程](docs/02-环境部署教程.md) | 安装、配置、启动、对象存储接入 |
| [03-功能使用说明](docs/03-功能使用说明.md) | 桌面版 / 网页版 / 管理台 / CLI 用法 |
| [04-安全设计说明](docs/04-安全设计说明.md) | 安全架构、鉴权、CSRF、路径防护、凭据管理 |
| [05-开发日志](docs/05-开发日志.md) | 缺陷修复、功能新增、架构改进、版本迭代 |
| [06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md) | 同类开源项目调研、筛选过程与借鉴取舍 |

## 版本

版本号统一维护在以下三个位置，升级版本时需同时修改：

- `pyproject.toml` → `version`
- `src/icogenerator/__init__.py` → `__version__`
- `src/icogenerator/server.py` → `VERSION`

## CI 与发布产物

仓库内置 GitHub Actions：

- `.github/workflows/ci.yml`：push / PR 自动构建 wheel + sdist，运行 CLI、ICO 生成、Web/API 与 Docker 冒烟测试。
- `.github/workflows/release.yml`：推送 `v*` 标签或手动触发时构建并发布：

| 产物 | 内容 |
|---|---|
| Python 包 | `icogenerator-<version>-py3-none-any.whl`、sdist；安装后提供 `icogen` / `icogen-desktop` |
| Windows 包 | `IcoGenerator-<version>-Windows-x64.zip`：桌面版 EXE + `icogen.exe`（CLI / serve） |
| Linux 包 | `IcoGenerator-<version>-Linux-x86_64.tar.gz`：`icogen`（CLI / serve） |
| Docker 镜像 | `ghcr.io/chinayury/icogenerator:<version>`、`:latest` |

> 桌面版与管理台/网页版共享同一服务端包；网页版和管理台是 `icogen serve` 的两个路由，
> 不单独拆包。Windows 可执行文件未签名，首次运行可能触发 SmartScreen 提示。

## 许可

本项目包含可对外提供服务的 Web 形态，按项目许可策略采用
**GNU Affero 通用公共许可证 v3.0 或更高版本（AGPL-3.0-or-later）**。

完整条款见 [LICENSE](LICENSE)。通过网络使用本项目的服务时，同样适用该许可证的源码提供义务。
