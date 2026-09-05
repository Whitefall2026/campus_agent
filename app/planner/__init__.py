# -*- coding: utf-8 -*-
"""app.planner ——「基于处境的推理与规划」后端引擎。

按功能拆分为以下模块（均可独立导入/测试）：
- fields:    任务规划字段归一化（energy_cost / deadline_type / deliverable 等默认与推断）
- energy:    用户精力曲线（逐小时精力系数，支持 EMA 更新）与时段能量计算
- store:     data/ 下规划数据的 JSON 持久化（profile、事件日志）
- planner:   贪心匹配调度器（容差 1.15）、阻塞兜底、软线自动延期、拖拽偏好记录
- decompose: LLM 任务拆解（提示词 / 解析清洗 / 子任务入池），AI 失败不拆
- risk:      历史完成率统计与简化蒙特卡洛风险评估
- shield:    负载评估与「智能挡箭牌」（外部插入任务的风险话术）
"""
from __future__ import annotations

VERSION = "0.1"
