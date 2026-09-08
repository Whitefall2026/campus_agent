# RUC Agent（Demo）

一个零依赖的校园生活日程助手 demo：把对话、微信里的安排自动整理成日程与待办，
用“画像 + 长期记忆 + 精力”做越来越懂你的排期与风险预判，不让任何一件重要的事被吞没。

## 功能

- **对话式录入**：全屏对话页（DeepSeek 风格）直接说安排，AI 边聊边提取；
  识别结果与微信消息统一以「待采纳」卡片弹进对话流，确认后才写入日程 / 待办
- **五页面布局**：对话 → 计划 → 日程 → 待办 → 我的（青色客户端风 UI）
- **日程 / 待办分流**：带固定时间或地点的进入日程；只有截止时间或纯任务的
  进入待办；每条识别结果都可选择“采纳到日程 / 采纳到待办 / 忽略”
- **跨日自动顺延**：今天之前仍未完成的普通日程会自动转入待办池，等待重新规划；
  导入课表生成的课程不会顺延
- **冲突提醒**：同一时间重叠、计划时间晚于截止时间，均会高亮提示
- **日程时间轴**：按天查看、拖拽调整时段、空白时段直接添加
- **待办池兜底**：识别不出时间的事项也绝不丢失，留在待办页等待规划
- **AI 选择性计划页（计划）**：AI 结合画像、处境、长期记忆、精力曲线与当前时间，
  只挑“目前值得规划”的待办排期（远的截止不提前刷屏），并给出完成概率推演；
  低概率任务显示风险与拆分/降标建议；采纳后写入对应日期
- **重新规划与跳过**：当天首次进入自动规划一次；跳过只隐藏本轮建议，
  点「重新规划」会重新纳入；规划时段绝不会落在当前时间之前
- **画像与长期记忆闭环**：对话 / 微信 / 计划反馈沉淀为证据 → 动态画像
  （精力 / 负载 / 压力 / 处境，事件驱动刷新）与长期记忆（偏好 / 性格）→
  注入对话、排期与挡箭牌；清空会话不会清空对话证据
- **微信自动提取**：读取并监听微信 4.x 聊天消息（wechatauto-replica），把含
  时间/地点/截止等要素的消息自动识别成日程；支持文件传输助手转发、指定会话或
  全部会话
- **智能挡箭牌**：粘贴外部任务（团建/聚餐通知）→ 自动评估当前负载，
  生成冷静、不制造焦虑的“婉拒/待定/接单”话术，可一键复制或仍添加
- **精力反馈校准**：完成待办后点「轻松/正常/吃力」，逐小时精力曲线按
  指数移动平均自动微调，排期与风险推演会越用越贴合你的真实状态
- **AI 可选、规则兜底**：AI 调用失败或未配置时，提取、排期、概率与话术
  都有本地规则兜底，Demo 全程可用
- **本地隐私**：日程 / 待办 / API Key / 画像 / 记忆 / 证据全部只存本机 `data/`

## 运行

无需安装任何依赖（仅用 Python 标准库）：

```
python server.py
```

浏览器打开 http://127.0.0.1:8000，顶部切换「对话 / 计划 / 日程 / 待办 / 我的」
五个页面。对话页全屏铺开；计划页首次进入会自动规划，AI 给出值得现在做的
排期建议与完成概率；日程页管理时间轴。

- 换端口：`python server.py 9000`
- 数据保存在 `data/todos.json`，删除该文件即可重置

### Windows 安装包（1.0.1）

安装版已经包含 Python 运行环境，目标电脑无需另行安装 Python。安装后从开始菜单
启动“RUC Agent 校园管家”，程序会驻留在系统托盘并打开默认浏览器。

- 安装包：`dist\installer\RUC-Agent-Setup-1.0.1.exe`
- 完整性校验：`dist\installer\SHA256SUMS-1.0.1.txt`
- 用户数据：`%LOCALAPPDATA%\RUC Agent\data`

升级或卸载程序不会主动删除用户数据。版本变化见 [CHANGELOG.md](CHANGELOG.md)。
当前本地构建未配置代码签名证书，首次运行时 Windows 可能显示 SmartScreen 提示；
请先核对随包 SHA-256，再选择“更多信息 → 仍要运行”。正式公开分发前建议配置代码签名。

### 微信自动提取（可选）

需要：

- Windows 上已安装并**登录**微信 4.1.12+ 桌面客户端；
- 先安装本仓库内置的微信库（仅 Windows，Python 3.9+）：

  ```
  pip install -e wechatauto-replica-main
  ```

1. 启动 `python server.py`（首次连接会自动读取数据库密钥，约几秒）；
2. 打开 http://127.0.0.1:8000，进入「我的」页，「微信自动提取」卡片会显示
   “等待微信登录后自动连接”，登录后自动变为“已连接”；
3. 默认监听**文件传输助手**：把别人发的、或自己粘贴的日程消息转发/发送到
   文件传输助手，日程会自动出现在时间轴里（带「📲 微信」标签）；
4. 也可在输入框改成其他会话名（昵称或备注均可，多个用逗号分隔）后点「保存」，
   或勾选“监听所有会话”；「立即扫描」会回读最近消息再提取一次。

实时监听默认每 `1.5` 秒轮询一次新消息（`data/wechat_config.json` 的 `interval`）；
把某个会话加入监听（或保存配置重连）时会先回读它最近 `backfill`（默认 30）条
消息，所以“加入监听之前就已经收到”的日程也会被补进，无需手动扫描。
“立即扫描”的作用也是回读每个监听会话最近 30 条（按条数，不按时间），
重复内容会按消息序号去重。

### AI 日程分析（可选，需要 API Key）

规则引擎只能抓“明显带时间/地点”的说法，复杂通知（群公告、多行转发、
口语化日程）会漏。启用 AI 后，微信消息改为：

1. **规则预筛**：消息需 ≥ `10` 字，且含明显时间词（今天/明天/周几/几点/
   月日/截止等）或日程线索（“结束后/之后/集合/开展”等时序词，以及
   “到/去/在 + 明确地点”如“到教一1101”）才会上传，闲聊直接过滤，
   省钱也保护隐私；
2. **AI 分析**：把该条消息连同会话最近几条上下文发给大模型，输出结构化日程；
3. **待采纳**：结果以「待采纳 · N」卡片直接出现在对话页的消息流里，
   点「采纳到日程 / 采纳到待办」才会写入；可随时忽略。

「我的」页的「AI 日程 / 待办分析」卡片里可选择服务商（OpenAI / DeepSeek / Kimi /
自定义 OpenAI 兼容接口）、填写模型名并粘贴 API Key，点「测试连接」可先验证。
AI 调用失败时会自动退回规则引擎的结果供你采纳，不会漏事。

配置保存在 `data/ai_config.json`（含 API Key，仅存本机）；
待采纳队列在 `data/ai_pending.json`。未启用 AI 时保持原有
规则自动入库行为不变。

配置保存在 `data/wechat_config.json`（`watch` 监听会话、`watch_all` 全部会话、
`backfill` 回读条数、`ignore_self` 是否忽略自己发的消息等）；处理记录与状态分别
在 `data/wechat_activity.json`、`data/wechat_state.json`。消息需含日期/时间/地点/
截止/时长等要素才会自动入库，闲聊消息只会记录在活动日志里，不会污染日程。

## 试试这些输入

| 输入 | 效果 |
| --- | --- |
| 周四晚上7点参加社团例会，在二教401 | 周四 19:00，自动识别地点、类别 |
| 数学作业下周一上午9点前提交 | 识别为「作业」+ 截止时间 |
| 明天上午9点去图书馆写论文，重要 | 高优先级 |
| 周六下午3点到体育馆参加篮球比赛 | 活动 + 地点 |
| 八点半去图书馆 / 晚上八点半参加社团例会 | 中文数字时间也能识别（八点半 → 8:30） |
| 九月三日参加考试 / 三十一号交水电费 | 中文数字日期（月/日/号） |
| 帮我买牛奶 | 无时间 → 放入待办池，并给出建议时段 |

## 架构

```
server.py             入口（启动 HTTP 服务；端口、微信自动启动在此处理）
app/core/             领域核心：extractor（自然语言→结构化）、scheduler（冲突/时间轴）、
                      kinds（类别）、storage（本地 JSON 持久化）
app/ai/gateway.py     AI Gateway（预筛 → 大模型提取 → 待采纳确认）
app/ai/profile.py     动态画像（状态/处境；事件驱动刷新 + 证据评估）
app/ai/memory.py      长期记忆（偏好/性格沉淀，AI 判断 + 规则兜底）
app/ai/estimator.py   用证据做 AI 状态评估（energy/load/pressure）
app/ai/context.py     统一用户背景构建（对话/规划/挡箭牌共用）
app/planner/          「基于处境的推理与规划」引擎：
                      fields（耗能/硬软线字段归一化）、energy（24h 精力曲线 + EMA）、
                      planner（空闲块 + 贪心排期 + 软线顺延 + 拖拽偏好）、
                      decompose（LLM 里程碑拆解，可选用）、
                      risk（AI 概率推演 + 规则蒙特卡洛兜底）、
                      shield（负载评估 + 挡箭牌话术）
app/wechat/bridge.py  微信读取/监听桥接（WeChatDB + Listener → 自动提取日程）
app/web/handlers.py   HTTP 路由 + 静态页面服务
app/paths.py          仓库内关键路径（data/、static/ 统一定位）
app/version.py        应用版本号
desktop.py            Windows 桌面/托盘启动器
static/               前端页面（原生 HTML / CSS / JS，无框架）
tests/                单元测试 + 真实 HTTP 端到端集成测试
packaging/            PyInstaller、Inno Setup 与校验文件构建脚本
wechatauto-replica-main/  第三方库 wechatauto-replica（微信能力，Apache-2.0）
```

运行数据（均在 `data/` 内，不会提交）：
- `todos.json` / `courses.json`：日程、待办与课程表
- `user_profile.json` / `user_state_history.json`：动态画像（状态 + 处境 + 历史快照）
- `user_memory.json`：长期记忆（偏好 / 性格，由证据定期沉淀）
- `user_evidence.json`：对话与行为证据（**清空会话不会清除**，供画像与记忆持续分析）
- `planner_profile.json` / `planner_events.json`：精力曲线与排期反馈事件
- `ai_config.json` / `ai_pending.json` / `wechat_*.json`：AI 配置与微信运行状态

## 后续可以怎么升级

- 接入 LLM（如 OpenAI API）替换规则解析，支持更复杂的口语表达
- 到期提醒：桌面通知 / 微信 / 邮件
- 导出 `.ics` 日历，对接课程表一键导入
- 多用户与云同步

## 开发与协作

1. 克隆仓库后可直接运行（核心功能仅用 Python 标准库，无需安装依赖）：

   ```
   python server.py
   ```

2. 需要「微信自动提取」的成员，在 Windows 上再执行一次
   `pip install -e wechatauto-replica-main`。
   `wechatauto-replica-main/` 是第三方库 `wechatauto-replica`
   （Apache-2.0，来源 https://github.com/fanyuantaier/wechatauto-replica ）
   的随仓库副本，仅作微信能力支撑，改动前请先看它的 LICENSE。
3. 所有本地运行数据（含 API Key、微信状态、个人日程）都写入 `data/`，
   该目录已被 `.gitignore` 忽略，**严禁提交**；换机器后重新配置即可。
4. 改动后端后建议先自检（详见 CONTRIBUTING.md）：
   `python -m unittest discover -s tests`（单元 + 端到端 API 集成测试）；
   改过前端后再跑 `node --check static/app.js`；也可单独看某天的能量排程
   演示：`python -m app.planner.demo`。
5. 分支、提交信息与本地验证的约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 构建 Windows 安装包

```powershell
python -m pip install -r packaging\requirements-build.txt
winget install --id JRSoftware.InnoSetup -e
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

构建会使用隔离数据目录运行测试，不会修改本机 `data/`；随后生成目录版 EXE、
安装包和 SHA-256 校验文件。详细说明见 [packaging/README.md](packaging/README.md)。

## License

本项目以 Apache License 2.0 开源（见根目录 `LICENSE`）。
`wechatauto-replica-main/` 为 Apache-2.0 的第三方库，保留其自带 LICENSE。
