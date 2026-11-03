"""SM-2 改良版间隔重复调度算法。

每次评级后，根据卡片当前状态计算新的：
state / ease / interval_days / reps / lapses / due
"""

from datetime import date, timedelta

from . import config


def _good_interval(reps: int, prev_interval: int, ease: float) -> int:
    """Good 评级的标准间隔序列。"""
    if reps <= len(config.GOOD_INTERVALS):
        return config.GOOD_INTERVALS[reps - 1]
    return max(1, round(prev_interval * ease))


def _state_for_interval(interval: int) -> str:
    if interval >= config.MASTER_INTERVAL_THRESHOLD:
        return config.STATE_MASTERED
    return config.STATE_REVIEW


def schedule(card, rating: int, today: date) -> dict:
    """根据评级计算卡片新的调度字段。

    参数:
        card: 含 state/ease/interval_days/reps/lapses 的映射（sqlite3.Row 或 dict）
        rating: 1~4
        today: 基准日期
    返回:
        新字段 dict
    """
    if type(rating) is not int or rating not in (1, 2, 3, 4):
        raise ValueError("评分须为 1–4 的整数")
    state = card["state"]
    ease = float(card["ease"])
    interval = int(card["interval_days"])
    reps = int(card["reps"])
    lapses = int(card["lapses"])

    if rating == config.RATING_AGAIN:
        # 完全不会：复习中遗忘记一次 lapse；间隔清零，当天重做
        if state in (config.STATE_REVIEW, config.STATE_MASTERED):
            lapses += 1
        reps = 0
        ease = max(config.MIN_EASE, ease + config.AGAIN_EASE_DELTA)
        return {
            "state": config.STATE_LEARNING,
            "ease": ease,
            "interval_days": 0,
            "reps": reps,
            "lapses": lapses,
            "due": today.isoformat(),
        }

    if rating == config.RATING_HARD:
        ease = max(config.MIN_EASE, ease + config.HARD_EASE_DELTA)
        if state in (config.STATE_NEW, config.STATE_LEARNING):
            # 新词/重做中模糊：仍不通过，当天继续
            return {
                "state": config.STATE_LEARNING,
                "ease": ease,
                "interval_days": 0,
                "reps": reps,
                "lapses": lapses,
                "due": today.isoformat(),
            }
        # 复习中模糊：算通过，间隔缩短
        reps += 1
        interval = max(1, round(interval * config.HARD_INTERVAL_FACTOR))
        return {
            "state": _state_for_interval(interval),
            "ease": ease,
            "interval_days": interval,
            "reps": reps,
            "lapses": lapses,
            "due": (today + timedelta(days=interval)).isoformat(),
        }

    if rating == config.RATING_GOOD:
        if state in (config.STATE_NEW, config.STATE_LEARNING):
            reps, interval = 1, 1
        else:
            reps += 1
            interval = _good_interval(reps, interval, ease)
        return {
            "state": _state_for_interval(interval),
            "ease": ease,
            "interval_days": interval,
            "reps": reps,
            "lapses": lapses,
            "due": (today + timedelta(days=interval)).isoformat(),
        }

    # RATING_EASY
    ease = ease + config.EASY_EASE_DELTA
    if state in (config.STATE_NEW, config.STATE_LEARNING):
        reps, interval = 1, config.FIRST_EASY_INTERVAL
    else:
        reps += 1
        interval = max(1, round(interval * ease * config.EASY_INTERVAL_FACTOR))
    return {
        "state": _state_for_interval(interval),
        "ease": ease,
        "interval_days": interval,
        "reps": reps,
        "lapses": lapses,
        "due": (today + timedelta(days=interval)).isoformat(),
    }
