# 更新日志（CHANGELOG）

> 记录规则：只追加、不修改历史条目。每条包含时间、改动类型、受影响范围与变更详情。
> 缺陷与功能明细见 [docs/05-开发日志](docs/05-开发日志.md)；
> 版本口径见 [docs/05-开发日志](docs/05-开发日志.md)。

---

## [v0.2.5] — 2026-11-20 CI 稳定性加固

连续多个版本出现 macOS CI runner 排队超时被系统取消（ubuntu / windows / docker /
wheel 各 job 均稳定通过），针对该结构性问题加固 CI 配置。

### 改进

- **CI 并发取消策略**：`concurrency: ci-<ref>` + `cancel-in-progress`——
  同一分支的旧 CI run 在新 run 启动时自动取消，从结构上避免重复排队
- **全部 CI/发布 job 增加 `timeout-minutes`**：单个 job 挂起时按时限失败
  （可 rerun），不再无限占用 runner 或长时间排队后整批被取消

### 兼容性

- 仅 CI/发布基础设施变更，不影响任何运行时行为与产物

---

## [v0.2.4] — 2026-11-19 清理预览与产物统计

维护循环复检（Issues 0 条、深筛项目无变化）。两项运营可见性小改进。

### 新增

- **管理台清理预览**（三段式：统计 → 确认 → 执行）：点击「清理历史产物」
  先以 dry-run 调用统计「将保留 / 将删除 / 可释放空间」，确认后再真实执行
  （在 v0.2.3 dry-run 能力上延伸，确认信息更充分）
- **`icogen info` 增加产物统计**：显示本地输出目录的文件数量与占用空间，
  并提示 `icogen clean` 清理入口

### 兼容性

- 纯 UI 与输出信息增强，无接口与行为变更

---

## [v0.2.3] — 2026-11-18 产物清理能力（存储卫生）

维护循环复检（Issues 0 条、深筛项目无更新）。巡检发现本地输出目录随使用
无界增长（元数据有 5000 条上限，但产物文件永不清），借鉴
[duma520/Image_To_Icon_Converter](https://github.com/duma520/Image_To_Icon_Converter)
的「清除历史记录」能力补齐清理入口。

### 新增

- **`LocalStore.prune(keep_last=N, dry_run)`**：保留最近 N 条，删除更旧的
  产物文件与元数据记录；安全约束——只删除仓库登记且目录内的文件
  （路径防穿越校验，越界登记仅清元数据）、`.icogen` 元数据目录永不触碰、
  dry-run 只统计不删除；返回释放字节与明细
- **管理台「清理历史产物」按钮**（`POST /api/artifacts/prune`，需管理员令牌）：
  输入保留数量 → 两次确认（浏览器 confirm）→ 结果反馈释放空间
- **CLI `icogen clean`**：`--keep N`（默认 100）+ `--dry-run`；
  真实删除需显式 `--yes`（不带时等同 dry-run，防误删）

### 兼容性

- 纯新增能力，不改变任何既有产物与行为；不清理则与历史行为完全一致

---

## [v0.2.2] — 2026-11-18 桌面版物料档位与批量联动

维护循环复检（Issues 0 条、深筛项目无更新）。完成 v0.2.1 UI parity 的桌面收尾：
物料档位能力透出到桌面版，并让批量转换尊重「同时生成站点物料包」勾选。

### 新增

- **桌面版「物料包档位」下拉**（完整 / 精简，借鉴 vite-pwa minimal2023Preset，
  与网页版 v0.2.1 对齐）：单图「保存站点物料包」与批量转换共用该选择，
  随 `UiPreferences.site_asset_set` 持久化（非法值回退 full）
- **批量转换联动站点物料包**：勾选「同时生成站点物料包」后，批量模式为
  每个文件额外产出物料包 ZIP（此前批量模式忽略该勾选，仅输出 ICO）

### 兼容性

- 档位默认 `full`，不勾选站点物料包时批量行为不变
- 配置文件新增 `ui.site_asset_set` 字段，旧配置缺省解析为 full

---

## [v0.2.1] — 2026-11-17 四入口能力透出（UI 对齐 API）

维护循环复检（Issues 0 条、深筛项目无更新）。此前 bitmap_format 与
站点物料包档位仅 CLI / API 可用，本轮把两项能力透出到网页版与桌面版，
补齐「四入口同一能力」的一致性；同时关闭一项暂缓决策。

### 新增

- **网页版控件**（能力透出）：「ICO 帧格式」下拉（bmp / png）与
  「物料包档位」下拉（完整 / 精简），分别接入既有
  `/api/generate`、`/api/bundle` 的 `bitmap_format` 与 `/api/site-assets`
  的 `asset_set` 字段
- **桌面版控件**（能力透出）：「② 定制输出规格」新增「帧格式」下拉，
  单图、批量共用；选择随界面偏好（UiPreferences.bitmap_format）持久化，
  恢复时对非法值回退 bmp

### 决策记录

- **CLI 重构复用 batch 引擎——决定不采纳**（关闭 v0.2.0 暂缓项）：
  CLI 的逐文件输出语义（质量提示、体积评估、单文件副本）与批处理引擎
  耦合度低，重构收益仅为去重，回归风险与验证成本不成比例；维持现状

### 兼容性

- 全部控件默认值（bmp / full）与历史行为逐字节一致
- 配置文件新增 `ui.bitmap_format` 字段，旧配置缺省解析为 bmp

---

## [v0.2.0] — 2026-11-16 桌面版批量转换与非正方形尺寸修复

维护循环复检（Issues 0 条、深筛项目无更新）后，落地桌面版批量转换能力，
并修复批量引擎自检中发现的一个自 v0.1.0 起潜伏的缺陷。详见
[docs/06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md) 附录。

### 新增

- **桌面版批量转换**（借鉴
  [duma520/Image_To_Icon_Converter](https://github.com/duma520/Image_To_Icon_Converter)
  的批量模式与 ConversionThread 后台线程模式）：
  「批量转换文件夹…」按钮——选择文件夹（含子目录，自动跳过无关文件），
  按「② 定制输出规格」当前设置逐个转换；后台线程执行不卡界面，
  进度实时显示到状态栏，结束弹窗汇总成功/失败与失败原因
- **批量转换引擎 `icogenerator/batch.py`**（服务编排层，桌面与后续 CLI 重构共用）：
  失败隔离（单文件失败不影响其余）、进度回调、dry-run 支持、
  参数前置校验（bitmap_format / asset_set / sizes / png_sizes）

### 修复

- **非正方形 ICO 目标尺寸 100% 失败的问题**（v0.1.0 起潜伏，本轮批量引擎自检发现）：
  实测 Pillow 13.0.0 的 ICO 编码器会把非正方形请求**静默降级**为正方形帧
  （如 16x32 → 16x16）且不报错，导致 `--sizes 16x32` 等输入必然失败于完整性自检，
  且错误信息误导。现于生成前明确拒绝并给出引导
  （「ICO 帧必须为正方形」+ 正方形尺寸简写提示）；
  实测确认无任何可用行为被改变——非正方形尺寸此前从未成功过
- `convert_batch` 参数错误（bitmap_format / asset_set / sizes / png_sizes）
  改为入口快速失败，与单文件路径行为一致

### 兼容性

- 桌面版原有单图流程与界面零改动（新增按钮与后台逻辑）
- 非正方形尺寸从「必然失败的误导报错」变为「生成前清晰拒绝」；
  正方形尺寸行为不变
- 批量引擎位于服务编排层（`icogenerator/batch.py`），core 纯逻辑层保持零 storage 依赖

---

## [v0.1.4] — 2026-11-15 单文件输出语义与物料包精简档

维护循环复检（Issues 0 条、深筛项目无更新）后，采纳已深读项目中尚未细化的
输出语义与物料档位要点。详见
[docs/06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md) 附录。

### 新增

- **单文件输出语义**（借鉴
  [duma520/Image_To_Icon_Converter](https://github.com/duma520/Image_To_Icon_Converter)
  第 7 节「单文件指定完整路径」的输出位置语义）：
  `icogen convert logo.png -o D:\apps\myapp\app.ico` 在单文件输入时把产物
  **额外精确复制**到指定路径；本地 store 仍是真相源，记录不变
- **站点物料包精简档**（借鉴 vite-pwa minimal2023Preset 的「现代最小物料集」）：
  CLI `--site-assets-set minimal` 与 API `POST /api/site-assets` 的 `asset_set`
  字段——minimal 档只保留 favicon.ico、独立 16/32、apple-touch、
  android-192/512、manifest.json、site-icon.html 与 MANIFEST.txt，
  去掉 mstile / browserconfig / maskable；HTML 片段与既有 manifest 合并
  逻辑同步不引用缺失文件
- **兼容性速览矩阵**（借鉴 favycon README 的兼容矩阵文档实践）：
  docs/03 新增各产物在现代浏览器 / Win10-11 / XP 的兼容对照表

### 兼容性

- `--site-assets-set` / `asset_set` 默认 `full`，完整档产物与历史逐字节一致
- minimal 为显式 opt-in；`include_maskable` 在 minimal 档强制视为 False
- `-o` 以 .ico 结尾且多文件输入时不再把该路径误建为目录（改为按配置目录输出）；
  单文件 + .ico 路径为新增语义，不影响原有目录用法

---

## [v0.1.3] — 2026-11-14 ICO PNG 帧格式选项

维护循环复检（Issues 0 条、深筛项目无更新）后，采纳上轮深读中记录未采纳的
ICO 帧编码优化点。详见
[docs/06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md) 附录。

### 新增

- **ICO 帧编码格式选项 `bitmap_format`**（借鉴
  [duma520/Image_To_Icon_Converter](https://github.com/duma520/Image_To_Icon_Converter)
  技术章节：现代 Windows 10/11 支持 PNG 压缩帧，仅 Windows XP 等旧系统不识别；
  Pillow ≥8.1 原生支持）：
  - CLI：`icogen convert --bitmap-format png`
  - API：`POST /api/generate` 与 `POST /api/bundle` 新增 `bitmap_format` 字段
    （非法值返回 400）
  - 同等画质下体积显著减小，超体积软预算的质量提示会给出该建议

### 改进

- `ico_over_budget` 质量提示补充 PNG 帧压缩建议（含旧系统兼容性说明）

### 兼容性

- 默认仍为 `bmp` 帧，与历史产出逐字节一致；`png` 为显式 opt-in
- 站点物料包内 favicon.ico 保持 BMP 帧（兼容优先），不受该选项影响
- Windows XP 等旧系统不识别 PNG 帧——追求最大兼容时继续使用默认 bmp

---

## [v0.1.2] — 2026-11-14 同类项目复检借鉴优化

按维护循环对 6 个深筛项目复检（无显著更新）、Issues 巡检（0 条开放）、新候选检索，
落地 2 项借鉴点与 1 项顺带改进。详见
[docs/06-同类项目调研与借鉴要点](docs/06-同类项目调研与借鉴要点.md) 第五节。

### 新增

- **目录递归扫描**（借鉴 duma520/Image_To_Icon_Converter，pixa 佐证同类普遍能力）：
  `icogen convert --recursive` 递归处理子目录中的图片
- **独立 PNG 尺寸集输出**（借鉴 electron-icon-maker 的 png 尺寸集与
  pwa-asset-generator 的 `--type png`）：`icogen convert --png-sizes 16,32,256`，
  尺寸范围 1–1024，与 ICO 共用同一套适配参数与渲染引擎

### 改进

- **目录输入按支持格式过滤**：批量扫描只处理 SUPPORTED_INPUT_FORMATS 扩展名的文件，
  递归扫描项目目录时不再把无关文件逐个报错（行为更贴近同类工具惯例）
- **core 渲染 API 公开化**：站点物料包内部的 PNG 渲染函数提升为公开 `png_bytes()`，
  CLI 与物料包共用同一渲染，避免逻辑复制

### 兼容性

- `--recursive` / `--png-sizes` 均为显式开启，默认行为不变
- 目录扫描的扩展名过滤会使「目录中含无关文件」的场景从逐个报错变为跳过；
  退出码语义不变（全部失败才返回 1）
- 同名 stem 的递归输出沿用既有冲突哈希机制，不会互相覆盖

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
