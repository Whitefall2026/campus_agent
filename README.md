# RUC Agent｜校园日程智能助手

> 把对话和微信里的安排整理成日程与待办，再结合个人精力、任务压力和长期偏好给出可执行的排期建议。

RUC Agent 是一个本地运行的校园生活助手。你可以像聊天一样输入安排，也可以让它从微信消息中发现事项；识别结果经过确认后进入日程或待办，后续再由规划引擎检查冲突、评估风险并安排合适时段。

当前版本：**2.1.1**。新增 Excel 待办导入，修正精力预算、负载计算与多项排期逻辑，详见 [CHANGELOG.md](CHANGELOG.md)。

核心后端仅使用 Python 标准库，前端使用原生 HTML、CSS 和 JavaScript。即使不配置大模型，日程提取、待办管理、冲突检查和规则排期也可以使用；配置 AI 后可获得更复杂的语义理解、任务拆解、画像分析和自然语言建议。

![RUC Agent Excel 待办导入示例（虚构数据）](docs/images/chat-preview.png)

## 核心体验

- **对话式录入**：直接输入“周四晚上 7 点在二教 401 开社团例会”，自动提取时间、地点和类别。
- **先确认，再入库**：对话和 AI 识别结果以“待采纳”卡片展示，可选择加入日程、加入待办或忽略。
- **日程与待办分流**：固定时间事项进入日程；只有截止时间或暂时无法排期的任务进入待办池。
- **跨日自动顺延**：今天之前仍未完成的普通日程会自动转入待办池等待重新规划；导入课表生成的课程不会顺延。
- **长期目标**：记录项目名称和截止时间，查看实时倒数、临期与逾期状态，并支持排序和本地持久化。
- **选择性规划**：结合截止时间、空闲时段、24 小时精力曲线和当前负载，只推荐现在值得规划的任务。
- **风险与冲突提示**：识别时间重叠、排期晚于截止时间、能量超载和低完成概率，并给出拆分或降标建议。
- **反馈闭环**：任务完成后可标记“轻松 / 正常 / 吃力”，系统用反馈校准后续时段的精力估计。
- **微信自动提取**：可监听文件传输助手、指定普通会话或全部普通会话，从聊天文本中识别日程与待办。
- **课程表导入**：支持导入教务平台导出的个人课表 `.xlsx`，课程会按教学周显示在日程中。
- **Excel 待办导入**：上传 `.xlsx` 任务表，自动识别任务、截止、优先级与时长，预览勾选后导入，跳过已完成和重复事项。
- **智能挡箭牌**：根据当前负载生成“婉拒 / 待定 / 接单”话术，帮助处理团建、聚餐等外部安排。
- **本地优先**：日程、画像、长期记忆和 API Key 默认只保存在本机 `data/` 目录。

## 页面概览

| 页面 | 用途 |
| --- | --- |
| 对话 | 输入自然语言安排，查看 AI 回复与待采纳结果 |
| 计划 | 查看精力曲线、排期建议、完成概率和挡箭牌 |
| 日程 | 按天查看时间轴，添加或拖动事项，检查冲突 |
| 待办 | 管理未排期任务、截止时间、优先级和完成反馈，上传 Excel 提取待办 |
| 长期目标 | 管理长期项目截止时间、倒数状态和到期统计 |
| 我的 | 导入课程表，配置 AI、微信监听、画像和本地数据 |

## 快速开始

### 环境要求

- Python 3.9 或更高版本
- Windows、macOS 或 Linux 均可运行核心功能
- 微信自动提取仅支持 Windows，并要求微信桌面版 4.1.12+ 已登录

### 启动项目

核心功能无需安装第三方依赖：

```bash
python server.py
```

然后访问 <http://127.0.0.1:8000>。按 `Ctrl+C` 停止服务。

指定端口有两种方式：

```bash
python server.py 9000
```

```powershell
$env:PORT = "9000"
python server.py
```

首次体验可以在页面中依次尝试：

1. 在“对话”页输入一条自然语言安排并确认识别结果。
2. 在“待办”页添加一个带截止时间的任务。
3. 打开“计划”页查看排期建议和完成概率。
4. 采纳建议后，到“日程”页查看或拖动时间块。
5. 完成任务并反馈体感，让精力曲线逐步校准。

## 输入示例

| 输入 | 预期结果 |
| --- | --- |
| 周四晚上 7 点参加社团例会，在二教 401 | 识别日期、19:00、地点和活动类别 |
| 数学作业下周一上午 9 点前提交 | 识别为带硬截止时间的待办 |
| 明天上午 9 点去图书馆写论文，重要 | 创建高优先级事项 |
| 周六下午 3 点到体育馆参加篮球比赛 | 识别活动时间和地点 |
| 晚上八点半参加社团例会 | 支持中文数字时间，识别为 20:30 |
| 九月三日参加考试 | 支持中文数字日期 |
| 帮我买牛奶 | 无明确时间，保留在待办池等待规划 |

## Excel 提取待办

进入“待办 → 从 Excel 提取待办”，选择 `.xlsx` 文件，核对预览后勾选并点击“导入选中待办”。取消预览不会新增事项。

建议首行采用以下表头（只有任务名称为必填列）：

| 任务名称 | 截止时间 | 优先级 | 预计时长（分钟） | 地点 | 交付物 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| 提交示例报告 | 2030-10-09 18:00 | 高 | 90 | 图书馆 | 报告初稿 | 未完成 |
| 整理示例笔记 | 2030-10-12 | 中 | 30 | | 整理后的笔记 | 未完成 |

- 支持中文表头和 `Task / Deadline / Priority / Duration / Location / Deliverable / Status` 等英文表头，读取所有可见工作表。
- 截止支持 Excel 日期、`YYYY-MM-DD`、`YYYY-MM-DD HH:MM`，也可使用“明天”“下周五”等自然语言；相对日期以导入当日为基准，缺年份的月日以当年解析。
- 时长支持数字分钟、`1.5小时` 或 `01:30`。优先级支持高/中/低与 high/medium/low。缺省字段沿用任务模型默认值，预览展示最终结果。
- 无表头时按每行一条自然语言待办提取，并提示人工核对。任意复杂版式、合并单元格与图片中的任务不保证识别。
- 已完成/已取消的行会跳过；相同标题、截止日期和截止时刻的事项会去重，已规划或已完成的同一事项也不会再次导入。非法日期、公式无缓存等问题显示行级提示。
- 支持 `.xlsx`，旧 `.xls` 请先另存为 `.xlsx`。单文件上限 20MB，解压后上限 64MB，最多 10000 行、100000 个单元格、500 条候选；超限请拆分表格。
- 读取器不执行公式、宏或外部链接，只读取公式缓存；请先在 Excel 中重算并保存。原文件仅在内存解析，不保存文件名、工作表名或原始整行。

提取无需 AI；确认导入后，任务与手动添加的待办一样参与画像和规划。如果用户启用了 AI，后续画像/规划仍遵循下文的 AI 数据发送约定。

## 精力与负载计算

1 个能量点表示系数 1.0 下的 30 分钟精力。默认逐小时曲线用于冷启动，07:00–23:00 之外不安排任务。全天和空闲时段使用相同单位积分；与日程/课程部分重叠的半小时格也视为占用，已完成日程仍保留其实际时间占位。

每次有效完成反馈以学习率 0.15、轻松/吃力增量 ±0.10 小幅校准，并按任务覆盖各小时的时长分摊权重；正常反馈不改变曲线。系数限制在有效范围内，零系数和睡眠时段不增加，同一任务的体感反馈只记录一次。所有规划入口读取同一条持久化个人曲线。

负载综合当天已有日程/课程与方案能量、今天至后天需处理的待办耗能和三天空闲容量。远期截止任务不再直接触发近期高负载；近期任务数 ≥6、当天占用或三天需求比 ≥95%、存在硬线阻塞时判为高，任务数 ≥3 或比例 ≥70% 时判为中。近期疲劳只看七天内反馈，外部压力包含五天内及已逾期的硬线截止。

风险概率是基于反馈权重和负载修正的规则模拟估计，并非真实失败率的统计。完成事件与后续体感反馈合并为一次观察；没有可用历史时采用默认基线。

## 可选：启用 AI

未配置 AI 时，项目使用本地规则完成基础提取、排期、概率评估和话术生成。启用 AI 后，可增强复杂通知解析、对话理解、画像更新、任务拆解和建议生成。

在“我的 → AI 日程 / 待办分析”中：

1. 选择 OpenAI、DeepSeek、Kimi 或自定义 OpenAI 兼容服务。
2. 填写模型名和 API Key。
3. 点击“测试连接”。
4. 测试成功后启用并保存。

微信消息在发送给 AI 前会先经过本地预筛：过短且没有时间、地点或安排线索的闲聊不会上传。AI 调用失败时会回退到规则识别，并保留可供确认的结果。

AI 配置保存在 `data/ai_config.json`，其中包含 API Key。请勿提交、分享或截图公开该文件。调用 AI 时，相关消息内容会发送给你选择的服务商，其数据处理规则以对应服务商条款为准。

## 可选：微信自动提取

### 前置条件

1. 使用 Windows，并安装、登录微信桌面版 4.1.12+。
2. 安装仓库内置的微信适配库：

   ```bash
   python -m pip install -e wechatauto-replica-main
   ```

3. 启动 RUC Agent，进入“我的 → 微信自动提取”。

### 使用方式

- 默认监听“文件传输助手”，可把包含日程的消息转发到这里。
- 可填写其他昵称或备注名；多个会话用逗号分隔。
- 可选择监听全部会话，也可点击“立即扫描”回读最近消息。
- 新增监听会话时会回读最近 `backfill` 条消息，默认 30 条，并按消息序号去重。
- 实时监听默认每 1.5 秒轮询一次；选择监听全部会话时，运行期间会继续发现新的普通会话。

未启用 AI 时，规则能够明确识别的普通微信日程会按原有规则自动入库；启用 AI 后，候选项会先进入“对话”页等待采纳。

微信配置位于 `data/wechat_config.json`，活动与处理状态位于 `data/wechat_activity.json` 和 `data/wechat_state.json`。

## Windows 安装包

安装版会打包 Python 运行环境，目标电脑无需单独安装 Python。安装时默认勾选“随 Windows 启动”；登录 Windows 后应用会在系统托盘中后台运行，不会自动打开浏览器。重新运行安装包可取消该选项，卸载时也会自动清理启动项。个人数据保存在 `%LOCALAPPDATA%\RUC Agent\data`，与程序文件分离，正常升级或卸载不会覆盖这些数据。

在项目根目录构建安装包：

```powershell
python -m pip install -r packaging\requirements-build.txt
winget install --id JRSoftware.InnoSetup -e
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

产物位于 `dist\installer\RUC-Agent-Setup-2.1.1.exe`，对应校验文件为 `dist\installer\SHA256SUMS-2.1.1.txt`。构建脚本会先运行自动化测试，再对冻结后的程序执行静态页面与核心 API 冒烟验证。

当前安装包未配置 Authenticode 代码签名，首次运行时 Windows 可能显示 SmartScreen 提示。请先核对 Release 附带的 SHA-256 校验文件，再决定是否运行；正式公开分发前建议配置代码签名。

## macOS 安装包

Release 提供两种原生 DMG，Apple Silicon（M1/M2/M3 等）选择 `RUC-Agent-2.1.1-macos-arm64.dmg`，Intel Mac 选择 `RUC-Agent-2.1.1-macos-x86_64.dmg`。打开 DMG 后将 **RUC Agent.app** 拖到 **Applications**，再从应用程序目录启动；应用会打开浏览器并留在菜单栏，菜单提供打开应用、打开数据目录和退出。

macOS 个人数据保存在 `~/Library/Application Support/RUC Agent/data`，升级或移除 `.app` 不删除这些数据。核心对话、日程、长期目标、精力规划和 Excel 待办可用；微信读取仍仅支持 Windows。macOS 不自动设置登录启动，如需自启动可在系统设置“通用 → 登录项”添加 RUC Agent。

DMG 在 GitHub macOS runner 原生构建，包含 Python 运行时，无需另装 Python。当前构建没有 Apple Developer 签名/公证；核对对应 SHA-256 后，如系统拦截可信下载，可在“系统设置 → 隐私与安全性”按系统提示允许打开。原生构建、冻结冒烟与架构检查由 [macOS 发布工作流](.github/workflows/release-macos.yml) 执行。

在对应架构的 Mac 上自行构建：

```bash
python3 -m pip install -r packaging/requirements-macos.txt
bash packaging/build_macos.sh
```

构建需要 Python 3.12+、Node.js 与 macOS 自带的 `hdiutil`、`ditto` 和 `lipo`，产物与校验文件位于 `dist/macos/`。请使用原生架构 Python；脚本会拒绝架构不一致的构建。

## 数据与隐私

Windows/Linux 源码运行时，数据默认写入仓库的 `data/`；macOS 使用上述 Application Support 目录。也可以通过 `RUC_AGENT_DATA_DIR` 指向其他目录。主要文件包括：

| 文件 | 内容 |
| --- | --- |
| `todos.json` / `courses.json` | 日程、待办和课程表 |
| `goals.json` | 长期目标 |
| `chat_thread.json` | 本地对话记录 |
| `ai_pending.json` / `ai_config.json` | 待采纳队列和 AI 配置 |
| `user_profile.json` / `user_state_history.json` | 动态画像与历史快照 |
| `user_memory.json` / `user_evidence.json` | 长期记忆与行为证据 |
| `planner_profile.json` / `planner_events.json` | 精力曲线与规划反馈 |
| `wechat_config.json` / `wechat_activity.json` / `wechat_state.json` | 微信配置、活动和处理状态 |

长期目标保存在 `data/goals.json`，与日程、待办一致，重启应用或更换浏览器都不会丢失。浏览器 `localStorage` 仅作为离线缓存与旧版本数据的迁移来源。

`data/`、个人 `materials/`、本机 `.env` 与构建目录已被 `.gitignore` 忽略。运行数据可能包含 API Key、微信状态和个人日程，请勿提交到 Git、粘贴到 Issue，或作为调试附件公开。公开截图仅使用虚构数据，安装包只包含程序与必要依赖。

如需彻底重置源码版数据，请先停止服务并备份整个 `data/` 目录，再删除其中内容；这会同时清除日程、课程、配置、画像、记忆和处理状态，无法在应用内恢复。页面中的局部清理按钮适合只重置某一类数据。

## 项目结构

```text
server.py                    HTTP 服务入口
desktop.py                   Windows / macOS 安装版桌面启动器
app/
├─ ai/                       AI 网关、上下文、画像、记忆与状态评估
├─ core/                     文本提取、类别、冲突检测与 JSON 存储
├─ planner/                  精力曲线、排期、风险、拆解与挡箭牌
├─ web/handlers.py           页面与 API 路由
├─ wechat/bridge.py          微信读取、监听与回填
└─ paths.py                  静态资源和用户数据路径
app/version.py               应用版本号
static/                      原生前端页面、样式和交互脚本
assets/brand/                校徽等品牌源文件（用于生成 Windows 图标）
tests/                       单元测试与真实 HTTP API 集成测试
packaging/                   PyInstaller 与 Inno Setup 构建脚本
docs/                        设计与技术文档
docs/images/                 README 使用的界面截图
wechatauto-replica-main/     微信能力依赖的随仓库副本
```

核心数据流：

```text
对话 / 微信 / 课程表
        ↓
规则预筛 → AI 可选增强 → 规则兜底
        ↓
待采纳确认 / 明确规则入库
        ↓
日程与待办 → 画像、记忆、精力与风险评估
        ↓
规划建议 → 用户采纳与完成反馈 → 下一轮校准
```

## 开发与验证

运行全部自动化测试：

```bash
python -m unittest discover -s tests
```

自动化覆盖核心算法、真实 HTTP API 和 Excel 导入；测试数据与本机个人数据隔离。还可以执行以下检查：

```bash
python -m compileall app tests server.py desktop.py
node --check static/app.js
python -m app.planner.demo
```

涉及浏览器交互、微信登录或真实 AI 服务的功能需要手动验证，详细步骤见 [ACCEPTANCE.md](ACCEPTANCE.md)。分支、提交和协作约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 已知边界

- 微信读取依赖 Windows、兼容版本的微信桌面端及本机登录状态。
- API Key 由用户自行提供；不同服务商的模型能力、费用和可用性可能不同。
- 拖拽排期基于原生 HTML5，桌面浏览器体验最佳，移动端可使用手动规划入口。
- 当前是本地单用户应用，尚未提供云同步、跨设备账户或系统级到期通知。

## Roadmap

- 桌面通知、邮件或微信到期提醒
- 导出 `.ics` 并与系统日历同步
- 更多学校课程表格式与一键导入
- 多端同步与可选的加密备份

### 构建 Windows 安装包

```powershell
python -m pip install -r packaging\requirements-build.txt
winget install --id JRSoftware.InnoSetup -e
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

构建会使用隔离数据目录运行测试，不会修改本机 `data/`；随后生成目录版 EXE、
安装包和 SHA-256 校验文件。详细说明见 [packaging/README.md](packaging/README.md)。

## License

本项目采用 [Apache License 2.0](LICENSE)。`wechatauto-replica-main/` 是第三方项目 `wechatauto-replica` 的随仓库副本，同样保留其 Apache-2.0 许可证；修改该目录前请先阅读其中的 `LICENSE`。
