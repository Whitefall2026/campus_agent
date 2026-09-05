from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _parse_date(value: Any) -> date | None:
    if not value:
        return None

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _parse_datetime(
    date_value: Any,
    time_value: Any,
) -> datetime | None:
    d = _parse_date(date_value)

    if d is None or not time_value:
        return None

    try:
        text = str(time_value).strip()

        if len(text) == 5:
            text += ":00"

        t = datetime.strptime(
            text,
            "%H:%M:%S",
        ).time()

        return datetime.combine(d, t)

    except ValueError:
        return None


def _duration_minutes(todo: dict) -> int:
    """
    获取一个固定日程预计占用的时间。

    优先级：
    1. end_time - time
    2. duration_min
    3. 默认 60 分钟
    """

    start = _parse_datetime(
        todo.get("date"),
        todo.get("time"),
    )

    end = _parse_datetime(
        todo.get("date"),
        todo.get("end_time"),
    )

    if start and end and end > start:
        return max(
            1,
            int(
                (end - start).total_seconds() / 60
            ),
        )

    try:
        duration = int(
            todo.get("duration_min") or 0
        )

        if duration > 0:
            return duration

    except (TypeError, ValueError):
        pass

    return 60


def _is_done(todo: dict) -> bool:
    return str(
        todo.get("status") or "pending"
    ).lower() == "done"


def _has_fixed_time(todo: dict) -> bool:
    return bool(
        todo.get("date")
        and todo.get("time")
    )


def _is_todo(todo: dict) -> bool:
    kind = str(
        todo.get("kind") or ""
    ).lower()

    if kind == "todo":
        return True

    if kind == "schedule":
        return False

    # 兼容历史数据：
    # 有 deadline、没有固定开始时间 → 待办
    if (
        todo.get("deadline")
        and not todo.get("time")
    ):
        return True

    return False


def _priority_weight(todo: dict) -> float:
    priority = str(
        todo.get("priority") or "medium"
    ).lower()

    if priority in (
        "urgent",
        "critical",
        "high",
        "重要",
        "紧急",
    ):
        return 1.35

    if priority in (
        "low",
        "低",
    ):
        return 0.75

    return 1.0


def _deadline_pressure(
    todo: dict,
    today: date,
) -> float:
    deadline = _parse_date(
        todo.get("deadline")
    )

    if deadline is None:
        return 0.0

    days = (
        deadline - today
    ).days

    if days < 0:
        return 1.50

    if days == 0:
        return 1.50

    if days == 1:
        return 1.35

    if days <= 3:
        return 1.20

    if days <= 7:
        return 1.05

    if days <= 14:
        return 0.85

    return 0.65


def _daily_occupancy(
    todos: list[dict],
    today: date,
) -> dict[str, int]:
    """
    计算今天到未来 7 天的固定日程占用分钟数。
    """

    result: dict[str, int] = {}

    for offset in range(8):
        d = today + timedelta(days=offset)
        result[d.isoformat()] = 0

    for todo in todos:
        if _is_done(todo):
            continue

        if not _has_fixed_time(todo):
            continue

        d = _parse_date(todo.get("date"))

        if d is None:
            continue

        if d < today:
            continue

        if d > today + timedelta(days=7):
            continue

        minutes = _duration_minutes(todo)
        key = d.isoformat()

        result[key] = (
            result.get(key, 0) + minutes
        )

    return result


def _daily_event_count(
    todos: list[dict],
    today: date,
) -> dict[str, int]:
    """
    计算今天到未来 7 天每天的固定事项数量。
    """

    result: dict[str, int] = {}

    for offset in range(8):
        d = today + timedelta(days=offset)
        result[d.isoformat()] = 0

    for todo in todos:
        if _is_done(todo):
            continue

        if not _has_fixed_time(todo):
            continue

        d = _parse_date(todo.get("date"))

        if d is None:
            continue

        if d < today:
            continue

        if d > today + timedelta(days=7):
            continue

        key = d.isoformat()

        result[key] = (
            result.get(key, 0) + 1
        )

    return result


def _pending_todo_count(
    todos: list[dict],
) -> int:
    """
    当前未完成的待办数量。
    """

    return sum(
        1
        for todo in todos
        if not _is_done(todo)
        and _is_todo(todo)
    )


def _deadline_count(
    todos: list[dict],
    today: date,
    days: int = 7,
) -> int:
    """
    未来 N 天内有截止日期的未完成待办数量。
    """

    end = today + timedelta(days=days)

    count = 0

    for todo in todos:
        if _is_done(todo):
            continue

        deadline = _parse_date(
            todo.get("deadline")
        )

        if deadline is None:
            continue

        if today <= deadline <= end:
            count += 1

    return count


def _overdue_count(
    todos: list[dict],
    today: date,
) -> int:
    """
    已经过截止日期但尚未完成的事项数量。
    """

    count = 0

    for todo in todos:
        if _is_done(todo):
            continue

        deadline = _parse_date(
            todo.get("deadline")
        )

        if (
            deadline is not None
            and deadline < today
        ):
            count += 1

    return count


def _weighted_load(
    todos: list[dict],
    today: date,
) -> float:
    """
    计算未来 7 天的综合任务负荷。

    综合考虑：
    - 固定日程时长
    - 优先级
    - deadline
    """

    total = 0.0

    horizon_end = (
        today + timedelta(days=7)
    )

    for todo in todos:
        if _is_done(todo):
            continue

        weight = _priority_weight(todo)

        # 固定日程
        if _has_fixed_time(todo):
            d = _parse_date(
                todo.get("date")
            )

            if d is None:
                continue

            if today <= d <= horizon_end:
                hours = (
                    _duration_minutes(todo)
                    / 60.0
                )

                total += hours * weight

            continue

        # 非固定时间待办
        deadline = _parse_date(
            todo.get("deadline")
        )

        if deadline is not None:
            if deadline < today:
                total += 2.5 * weight

            elif deadline <= horizon_end:
                total += (
                    1.5
                    * weight
                    * _deadline_pressure(
                        todo,
                        today,
                    )
                )

            else:
                total += 0.75 * weight

        else:
            # 没有明确 deadline，
            # 仍然占用一定认知空间。
            total += 0.75 * weight

    return total


def _pressure_from_load(
    weighted_load: float,
    pending_count: int,
    deadline_count: int,
    overdue_count: int,
    max_daily_minutes: int,
    max_daily_events: int,
) -> float:
    """
    将客观负荷转换成 0~100 的压力分数。

    注意：
    这里完全由确定性规则计算，
    AI 不参与这个数字的计算。
    """

    load_score = min(
        100.0,
        weighted_load / 24.0 * 100.0,
    )

    todo_score = min(
        100.0,
        pending_count / 12.0 * 100.0,
    )

    deadline_score = min(
        100.0,
        deadline_count / 6.0 * 100.0,
    )

    overdue_score = min(
        100.0,
        overdue_count / 3.0 * 100.0,
    )

    occupancy_score = min(
        100.0,
        max_daily_minutes / 600.0 * 100.0,
    )

    event_score = min(
        100.0,
        max_daily_events / 7.0 * 100.0,
    )

    score = (
        load_score * 0.28
        + occupancy_score * 0.20
        + event_score * 0.12
        + todo_score * 0.15
        + deadline_score * 0.15
        + overdue_score * 0.10
    )

    return round(
        max(
            0.0,
            min(100.0, score),
        ),
        1,
    )


def calculate_pressure(
    todos: list[dict] | None = None,
    today: date | None = None,
) -> dict:
    """
    根据真实 todos 计算客观日程压力。

    返回：
    - pressure_score
    - future_density
    - max_daily_minutes
    - max_daily_events
    - weighted_load
    - pending_todo_count
    - deadline_count_7d
    - overdue_count
    - schedule_count_7d
    """

    if todos is None:
        todos = []

    if today is None:
        today = date.today()

    occupancy = _daily_occupancy(
        todos,
        today,
    )

    event_count = _daily_event_count(
        todos,
        today,
    )

    pending_count = _pending_todo_count(
        todos
    )

    deadline_count = _deadline_count(
        todos,
        today,
        days=7,
    )

    overdue_count = _overdue_count(
        todos,
        today,
    )

    weighted_load = _weighted_load(
        todos,
        today,
    )

    max_daily_minutes = (
        max(occupancy.values())
        if occupancy
        else 0
    )

    max_daily_events = (
        max(event_count.values())
        if event_count
        else 0
    )

    pressure_score = _pressure_from_load(
        weighted_load=weighted_load,
        pending_count=pending_count,
        deadline_count=deadline_count,
        overdue_count=overdue_count,
        max_daily_minutes=max_daily_minutes,
        max_daily_events=max_daily_events,
    )

    future_density = {}

    for key in occupancy:
        future_density[key] = {
            "minutes": occupancy[key],
            "events": event_count.get(
                key,
                0,
            ),
        }

    schedule_count_7d = sum(
        1
        for todo in todos
        if (
            not _is_done(todo)
            and _has_fixed_time(todo)
            and (
                (
                    d := _parse_date(
                        todo.get("date")
                    )
                )
                is not None
            )
            and today <= d <= today + timedelta(days=7)
        )
    )

    return {
        "pressure_score": pressure_score,
        "future_density": future_density,
        "max_daily_minutes": max_daily_minutes,
        "max_daily_events": max_daily_events,
        "weighted_load": round(
            weighted_load,
            2,
        ),
        "pending_todo_count": pending_count,
        "deadline_count_7d": deadline_count,
        "overdue_count": overdue_count,
        "schedule_count_7d": schedule_count_7d,
    }


def today_brief(todos: list[dict] | None = None,
                today: date | None = None,
                now: datetime | None = None) -> dict:
    """生成面向用户的「今天先做什么，还剩多少可自由安排时间」。

    这是一个确定性的摘要：不把尚未确认的待办偷偷塞进日程，而是先把
    必须完成的事说清楚，剩余空白仍然属于用户自己。
    """
    todos = todos or []
    today = today or date.today()
    now = now or datetime.now()
    pressure = calculate_pressure(todos, today)
    priorities = []
    for todo in todos:
        if _is_done(todo):
            continue
        deadline = _parse_date(todo.get("deadline"))
        fixed_today = _has_fixed_time(todo) and _parse_date(todo.get("date")) == today
        due_today = deadline == today
        overdue = deadline is not None and deadline < today
        high_priority = _priority_weight(todo) > 1.0 and _is_todo(todo)
        if not (fixed_today or due_today or overdue or high_priority):
            continue
        if overdue:
            reason = "已逾期，先决定今天是否处理"
        elif due_today:
            reason = "今天截止，优先留出时间"
        elif high_priority:
            reason = "高优先级，趁还有余量先推进一点"
        else:
            reason = "今天已有固定安排"
        priorities.append({
            "id": todo.get("id"), "title": str(todo.get("title") or "未命名事项"),
            "reason": reason, "deadline": todo.get("deadline"),
            "time": todo.get("time"), "kind": todo.get("kind"), "is_todo": _is_todo(todo),
            "rank": 0 if overdue else 1 if due_today else 2 if high_priority else 3,
        })
    priorities.sort(key=lambda x: (x["rank"], x.get("time") or "99:99", x["title"]))

    # 仅估算今天 09:00–22:00 内、从此刻开始仍未被固定日程占用的分钟数。
    day_start = datetime.combine(today, datetime.strptime("09:00", "%H:%M").time())
    day_end = datetime.combine(today, datetime.strptime("22:00", "%H:%M").time())
    cursor = max(day_start, now) if today == now.date() else day_start
    occupied = 0
    for todo in todos:
        if _is_done(todo) or not _has_fixed_time(todo) or _parse_date(todo.get("date")) != today:
            continue
        start = _parse_datetime(todo.get("date"), todo.get("time"))
        if start is None:
            continue
        end = start + timedelta(minutes=_duration_minutes(todo))
        overlap_start, overlap_end = max(start, cursor), min(end, day_end)
        if overlap_end > overlap_start:
            occupied += int((overlap_end - overlap_start).total_seconds() // 60)
    available = max(0, int((day_end - cursor).total_seconds() // 60) - occupied)
    starter = next((x for x in priorities if x.get("is_todo")), None)
    if starter:
        starter = {**starter, "action": "先给它 25 分钟；不要求一次做完。"}
    return {
        "today": today.isoformat(), "pressure": pressure,
        "free_minutes": available, "priority_items": priorities[:3],
        "starter": starter,
        "message": (
            "先完成最重要的一件，剩下的时间由你自己决定。"
            if priorities else "今天没有迫在眉睫的事项，留一点空白给自己。"
        ),
    }
