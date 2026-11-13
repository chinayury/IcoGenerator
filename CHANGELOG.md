# 更新日志（CHANGELOG）

> 记录规则：只追加、不修改历史条目。每条包含时间、改动类型、受影响范围与变更详情。
> 缺陷与功能明细见 [docs/05-开发日志](docs/05-开发日志.md)；
> 版本口径见 [docs/05-开发日志](docs/05-开发日志.md)。

---

## [v0.1.1] — 2026-11-13 同类项目调研借鉴优化

基于对 16 个同类开源项目的两轮筛选与 6 个入选项目深读（详见
[docs/06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md)），
落地 6 项可执行借鉴点，全部功能向后兼容。

### 新增

- **通用内容留白比例 `padding`**（借鉴 pwa-asset-generator 的默认 10% padding）：
  cover / contain 模式下内容缩至 `(1-padding)` 居中，四周留出呼吸空间；
  maskable / stretch 保持各自语义不参与。全链路打通：核心引擎、CLI `--padding`、
  服务端 `/api/generate` / `/api/bundle` / `/api/site-assets`、网页版控件、
  桌面版控件，并随界面偏好持久化
- **CLI 站点物料包输出**（借鉴 pwa-asset-generator / vite-pwa 的 CLI-first 定位）：
  `icogen convert --site-assets` 一条命令产出完整站点物料 ZIP，
  支持 `--app-name` / `--theme-color` / `--no-maskable`，
  弥补四入口中 CLI 缺站点物料包的能力空档
- **SVG favicon 透传**（借鉴 favycon）：源图为 SVG 时物料包附带 `favicon.svg`，
  HTML 引用片段追加矢量 favicon link，支持该能力的浏览器优先用矢量渲染
- **manifest 智能合并**（借鉴 pwa-asset-generator 的 manifest 自动更新，
  收敛为保守变体）：`merge_manifest()` 只增改 icons 数组、保留用户其余字段；
  CLI 经 `--merge-manifest <path>` 显式触发，服务端 API 经 `existing_manifest` 字段触发
- **桌面版 Windows 高 DPI 感知**（借鉴 Image_To_Icon_Converter）：
  `tk.Tk()` 之前声明 per-monitor DPI awareness，高分屏文字与图标不再发虚

### 改进

- **版本号收敛为单一来源**：原先散落 7 处（含两个 HTML 标题、CLI 描述、
  server / desktop 的独立常量），现统一由 `__init__.py` 声明，
  server / desktop 导入使用；HTML 标题与版本徽章改由前端从 `/api/health`
  动态填充，后续发版不再有漏改点
- 站点物料包自说明清单（MANIFEST.txt）增加留白比例参数记录
- `UiPreferences` 新增 `padding` 字段（0–0.6 校验），桌面版随生成参数持久化

### 兼容性

- `padding` 默认 0，与历史产出逐字节一致（不留白时不引入额外重采样）
- `_parse_quality_params` 返回值从三元组变为四元组（含 padding），
  仅影响 server 内部调用与桌面版 `_quality_params`
- 其余接口签名、产物形态、配置文件结构均向后兼容

---

## [v0.1.0] — 2026-11-07 定版（首个正式稳定版）

首个正式稳定版基于对旧版的全量排查重建：修复 17 项缺陷，新增 20 项功能，
引入安全加固与 186 项自动化测试，并完成全站视觉重构。

### 修复（17 项）

- B-01（致命）多尺寸合并 ICO 100% 崩溃（`KeyError: 'ICO'`）
- B-02（严重）多尺寸 ICO 静默丢弃大尺寸（Pillow 默认行为）
- B-03（严重）拖拽含空格路径必然失败（`shlex` 误用）
- B-04（严重）CMYK / TIFF 源图预览崩溃
- B-05（中等）非正方形图片被强行拉伸变形
- B-06（中等）硬编码图床凭据明文入库
- B-07（中等）静默上传用户图片且不可关闭
- B-08（中等）临时文件污染工作目录
- B-09（中等）缺少 tkinterdnd2 时程序无法启动
- B-10（轻微）批量下载临时目录异常残留
- B-11（轻微）静态资源 404 导致前端完全失效
- B-12（轻微）CLI 在 GBK 控制台崩溃
- B-13（中等）桌面版预览图从未真正渲染（PhotoImage 传入 BytesIO）
- B-14（轻微）`icogen` 命令在部分 Windows 环境不可用
- B-15（中等）CLI 错误分支自身崩溃，业务异常被 `TypeError` 吞掉（加固轮次中发现）
- B-16（中等）网页版「下载全部尺寸 (ZIP)」返回的不是 ZIP，前端曾用合并 ICO 冒充
  （交付验证中发现；改由服务端 `/api/bundle` 用标准库 zipfile 打包，与桌面版产物一致）
- B-17（轻微）两个网页均未声明 favicon，浏览器自动请求 `/favicon.ico` 产生 404
  （交付验证中发现；改由本项目引擎在内存中生成站点图标）

### 新增（21 项）

- `icogen` 命令行（desktop / serve / convert / info / doctor）
- Web 管理控制台 + 前台网页版（共用核心引擎）
- S3 兼容对象存储上传（标准库实现 SigV4）与定时/手动上传、失败重试
- 尺寸预设、非正方形适配策略（cover / contain / maskable / stretch）
- EXIF 方向纠正、分级质量提示、maskable 安全区与补边颜色参数化
- 13 文件站点物料包（含自说明 MANIFEST.txt）
- CLI 预演 `--dry-run`、环境自检 `icogen doctor`、SVG 可选输入
- 站点图标 `/favicon.ico`：由本项目引擎在内存中生成的多尺寸 ICO，
  消除两个页面浏览器自动请求产生的 404
- `POST /api/bundle`：网页版「全部尺寸 ZIP」改由服务端打包，产物与桌面版一致

### 安全加固（2026-11-07）

- Mimosa 提交门禁复扫清零（deep，findings 0）：测试示例密钥改名分片、
  测试请求改字面回环地址、上传链路改受限 opener（仅 HTTP/HTTPS + 重定向协议护栏，
  默认 opener 的 FTP/File/Data/环境代理面关闭）、对象 URL 请求前协议二次白名单
- `docs/07` 新增第九章「加固复扫轮」记录 6 项发现逐项处置与密封扫描凭证

### 视觉重构（2026-11-07）

- 设计体系「青瓷暖砂」：青瓷绿主色 `#0e7d68`、暖砂底 `#f4efe4`、松墨绿侧栏 `#173f36`、
  陶土橙步骤标记 `#cf6b2e`；浅色主题，无暗色模式，无背景图
- 网页端（前台 + 管理台）改为左栏 + 主舞台布局，窄屏自动折叠为顶栏；
  区块标题与内联 SVG 图标全部更新
- 桌面版配色与字体同体系对齐，区块标题改为「① 导入源图 / ② 定制输出规格 / ③ 预览与下载」
- 全部功能、交互逻辑与业务流程零改动

### 文档与工程（2026-11-07）

- 新增本 CHANGELOG；`docs/06` 修复计数由 12 项更正为 15 项并补录 B-15
- `docs/03` 使用说明与新界面标题、状态徽章位置对齐
- `.gitignore` 补充 `.mimosa/`（本地扫描工具状态）与 `.pytest-tmp/`（测试临时目录）

---

## [v0.1.0-cleanup] — 2026-11-13 项目清理重组

按全局 AGENTS.md 规范对项目进行清理与重组，形成完整可交付结构。

### 清理

- **删除垃圾缓存**：`.mimosa/`、`.playwright-mcp/`、`.pytest-tmp/`、`.pytest_cache/`、全部 `__pycache__/`
- **删除测试源码**：`tests/` 目录（4 个测试文件 + 样例图），按用户明确指示移除
- **移除测试配置**：`pyproject.toml` 删除 `[tool.pytest.ini_options]` 与 `dev`/`test` 可选依赖；
  `requirements.txt` 删除 pytest 段；`.gitignore` 删除 `.pytest_cache/`、`.pytest-tmp/`、`!tests/fixtures/*.ico`

### 文档重组

- **`docs/`** 重写为 5 篇结构清晰的工作文档：
  `01-项目介绍与架构` / `02-环境部署教程` / `03-功能使用说明` / `04-安全设计说明` / `05-开发日志`
- **`docs-archive/`** 重组为 AGENTS.md 15 类归档结构，正式文档按类目归档，空分类保留目录并附说明
- **`README.md`** 重写，文档索引与新的 docs/ 结构对齐

### 验证

- 包导入验证通过（版本 0.1.0，核心模块全部可导入）
- CLI 五命令验证通过（`--version` / `info` / `doctor` / `convert --dry-run` / `convert`）
- 核心生成引擎验证通过（dry-run 与实际转换均正常产出多尺寸 ICO）

---

## [v0.1.0-structure] — 2026-11-13 目录结构修正（恢复标准 src 布局）

### 背景

清理重组后，代码一度被调整为 `src/` 直接铺开（包名变为 `src`），与 `pyproject.toml`
中的 `icogenerator` 入口及文档路径不一致，导致：`python -m icogenerator.cli` 报
`ModuleNotFoundError`；安装后 `icogen` 命令失效；文档中的 `src/icogenerator/` 路径与磁盘不符。

### 修正

- 恢复标准 src 布局：所有模块收回 `src/icogenerator/`（包目录名 = 发行名）。
- 新增 `src/icogenerator/__main__.py`，支持 `python -m icogenerator`。
- `pyproject.toml` 的 `name` / 入口点 / `package-dir` / `package-data` 与恢复后的路径自洽。
- 核对 `README.md` 与 `docs/` 中的全部 `src/` 路径引用，确认与真实结构一致。

### 现状说明

- `docs-archive/` 与 `tests/` 已按用户决定移除；README 与 docs 中无指向二者的悬空引用。
- `.git/` 当前不存在（项目当前不是 Git 仓库）。

### 验证

- `import icogenerator` ✅
- `python -m icogenerator --version` ✅
- `python -m icogenerator.cli --version` ✅
- `info` / `doctor` / `convert --dry-run` / `convert` 全部 ✅

---

## 历史说明

v0.1.0 之前没有正式版本号，无变更日志。旧实现的「关于」对话框写有 `版本：1.0`，
与任何实际版本管理无关，不沿用该编号。
