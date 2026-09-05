# -*- coding: utf-8 -*-
"""日程(schedule)与待办(todo)的区分规则。

概念约定（与前端三页面对应）：
- schedule 日程：有明确开始时间/地点、需要出现在时间轴上的安排；
- todo 待办：只有截止时间、或者没有固定时间的任务（不一定有 deadline）。

规则模式/AI 结果都会经过这里的分类与字段归一化，
避免“9月30日前交材料”这种带日期的任务被误放进日程时间轴。
"""
from __future__ import annotations

import re

KIND_SCHEDULE = "schedule"
KIND_TODO = "todo"
VALID_KINDS = {KIND_SCHEDULE, KIND_TODO}

# 截止/任务信号：出现这些词时优先按“待办”处理
_TODO_MARK_RE = re.compile(
    r"(截止|ddl|deadline|报名截止|缴(?:费|钱|款)|交(?:作业|材料|报告|实验报告|"
    r"论文|申请表?|学费|电费|水费|党费|团费|入党申请|心得|总结|表格?|报名表|"
    r"名单|作品|文件|资料)|提交|上交|填写|填报|发(?:邮件|消息|给)|前(?:交|提交|完成|交齐|写完)|"
    r"之前(?:交|完成|交齐)|完成|写完|看完|复习|预习|准备|背|整理|打包|采购|买|"
    r"取(?:快递|包裹|东西)|办(?:证|手续)|修改|撰写|打印|复印|报名参加.*(?:截止)?)"
)

# 活动/日程信号：出现这些词时，带日期(±地点)的消息按“日程”处理
_EVENT_MARK_RE = re.compile(
    r"(参加|出席|到|去|前往|举行|举办|开展|开始|开会|上课|上课时间|比赛|例会|"
    r"讲座|活动|集合|报到|彩排|排练|演练|培训|开幕|仪式|典礼|考试|答辩|演出|"
    r"参观|开会|会议|论坛|见面会|宣讲会|招聘会|社团|纳新|招新|实践|调研|"
    r"选修|选课|体检|打(?:球|卡)|训练|操课|军训)"
)


def valid_kind(value) -> str | None:
    v = str(value or "").strip().lower()
    if v in ("schedule", "日程", "事件", "event"):
        return KIND_SCHEDULE
    if v in ("todo", "task", "待办", "任务", "事项"):
        return KIND_TODO
    return None


def derive_kind(fields: dict | None = None, raw: str | None = None,
                title: str | None = None) -> str:
    """根据解析字段 + 原文，推断一条内容属于日程还是待办。"""
    f = fields or {}
    text = " ".join(x for x in (raw, f.get("raw"), title or f.get("title")) if x)

    # 明确的截止字段 → 待办
    if f.get("deadline") or f.get("deadline_time"):
        return KIND_TODO
    # 原文带截止/提交信号 → 待办（即使规则引擎把日期放进了 date）
    if _TODO_MARK_RE.search(text):
        return KIND_TODO
    # 有开始时间（+地点）→ 大概率是日程
    if f.get("time"):
        return KIND_SCHEDULE
    # 有地点 + 日期，且像活动 → 日程
    if f.get("date") and f.get("location"):
        if _EVENT_MARK_RE.search(text) or not _TODO_MARK_RE.search(text):
            return KIND_SCHEDULE
        return KIND_TODO
    # 只有日期：看活动词还是任务词
    if f.get("date"):
        if _EVENT_MARK_RE.search(text):
            return KIND_SCHEDULE
        if _TODO_MARK_RE.search(text):
            return KIND_TODO
        return KIND_SCHEDULE if f.get("end_time") or f.get("duration_min") else KIND_TODO
    return KIND_TODO


def normalize_item(fields: dict | None, kind: str | None = None,
                   raw: str | None = None) -> dict:
    """入库前的字段归一化：
    - 确定 kind；
    - 待办：date/time（无地点、无开始语义）迁移为 deadline/deadline_time；
    - 日程：保留原字段。
    """
    f = dict(fields or {})
    kind = valid_kind(kind) or valid_kind(f.get("kind")) or derive_kind(f, raw)
    f["kind"] = kind
    text = " ".join(x for x in (raw, f.get("raw")) if x)

    if kind == KIND_TODO:
        if not f.get("deadline") and f.get("date"):
            f["deadline"] = f.get("date")
            f["date"] = None
        if f.get("deadline") and not f.get("deadline_time") and f.get("time"):
            f["deadline_time"] = f.get("time")
            f["time"] = None
        # kind=todo 不允许携带排期日期/时间：要排期请切 kind=schedule。
        # 否则会出现“有 date/time 但不上日程、也不参与规划”的夹生数据。
        f["date"] = None
        f["time"] = None
        f["end_time"] = None
        return f

    # schedule：必须保留一个日期供时间轴展示；没有日期但有 time 时给提示，由用户补日期
    if not f.get("date") and f.get("time") and not f.get("deadline"):
        pass
    return f


def is_todo(item: dict) -> bool:
    return valid_kind(item.get("kind")) == KIND_TODO


def is_schedule(item: dict) -> bool:
    return valid_kind(item.get("kind")) == KIND_SCHEDULE
