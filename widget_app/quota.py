"""Kimi Code 额度解析：把 OpenUsage /api/usage 的服务数组换算成剩余百分比。

纯逻辑、无 I/O。字段匹配优先按 path 后缀（与 Kimi 上游 API 结构绑定，稳定），
标签匹配做兜底。OpenUsage 字段配置见 openusage-deployment/README-STATUS.md
「Kimi Code 接入」：三个百分比字段的 value 是 0–1 的**已用**比例，
剩余% = (1 - value) × 100；两个重置字段 type 为 reset_date。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# path 后缀（有无 usages. 前缀都能匹配）
FIVE_H_USED = "limit_5h.used_ratio"
MONTH_TOTAL_USED = "limit_month_total.used_ratio"
MONTH_CODE_USED = "limit_month_code.used_ratio"
FIVE_H_RESET = "limit_5h.reset_time"
MONTH_RESET = "limit_month_total.reset_time"

# 标签兜底：归一化（去空格、转小写）后做包含判断；候选从具体到宽泛
_LABEL_FALLBACKS = {
    FIVE_H_USED: ("5小时剩余", "5小时", "5h"),
    MONTH_TOTAL_USED: ("月总额度剩余", "月总额度", "月总"),
    MONTH_CODE_USED: ("月代码额度剩余", "月代码额度", "月代码"),
    FIVE_H_RESET: ("5小时重置",),
    MONTH_RESET: ("月额度重置", "月重置"),
}


@dataclass
class KimiQuota:
    """三个档位的剩余百分比（0–100，None=字段缺失/非法）与重置时间（aware UTC）。

    service_id 是 OpenUsage 里该服务的 id（来自 /api/usage 条目的 "id"），
    供活动态单服务轮询使用；解析失败为 None。
    """

    five_hour_remaining: Optional[float]
    month_total_remaining: Optional[float]
    month_code_remaining: Optional[float]
    reset_5h: Optional[datetime]
    reset_month: Optional[datetime]
    fetched_at: datetime
    stale: bool = False
    service_id: Optional[int] = None


@dataclass
class OpenCodeQuota:
    """OpenCode 三档（5 小时/每周/每月）剩余百分比与各自重置时间。

    上游 percent_used 是 0–100 的已用百分比（与 Kimi 的 0–1 小数不同），
    remaining = 100 − clamp(percent_used)。
    """

    five_hour_remaining: Optional[float]
    week_remaining: Optional[float]
    month_remaining: Optional[float]
    reset_5h: Optional[datetime]
    reset_week: Optional[datetime]
    reset_month: Optional[datetime]
    fetched_at: datetime
    stale: bool = False
    service_id: Optional[int] = None


@dataclass
class DeepSeekQuota:
    """DeepSeek 余额快照。金额用 Decimal（上游是字符串金额，避免浮点误差）；
    currency 为币种代码（CNY/USD/…），显示符号由调用方映射。"""

    balance: Optional[Decimal]
    topped_up_balance: Optional[Decimal]
    granted_balance: Optional[Decimal]
    currency: str
    is_available: Optional[bool]
    fetched_at: datetime
    stale: bool = False
    service_id: Optional[int] = None


# Kimi 的 5 小时限额窗口长度；收起态迷你卡的"满环"基准
FIVE_H_WINDOW = timedelta(hours=5)


def countdown_ratio(reset: Optional[datetime], now: datetime) -> Optional[float]:
    """距 5 小时重置的剩余时间比例（迷你卡圆环弧长编码）。

    (reset − now) / 5h 并 clamp 到 0–1：满环=整整 5 小时，随时间线性缩短到 0，
    服务端换上下一个重置点后自动回满。reset 缺失返回 None（调用方自行兜底）。
    """
    if reset is None:
        return None
    delta = (reset - now).total_seconds()
    return max(0.0, min(1.0, delta / FIVE_H_WINDOW.total_seconds()))


def format_countdown_hm(target: Optional[datetime], now: datetime) -> str:
    """裸倒计时（小时·分）：2小时13分 / 43分；已过期 0分；缺失 --。"""
    if target is None:
        return "--"
    secs = (target - now).total_seconds()
    if secs <= 0:
        return "0分"
    total_minutes = int(secs // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def format_countdown_dh(target: Optional[datetime], now: datetime) -> str:
    """裸倒计时（天·小时，小时按半小时四舍五入）：11天3小时 / 11天；
    不足 1 天退化为小时分格式；已过期 0分；缺失 --。"""
    if target is None:
        return "--"
    secs = (target - now).total_seconds()
    if secs <= 0:
        return "0分"
    rounded = int(secs / 1800 + 0.5) * 1800  # 半小时四舍五入（floor(x+0.5)，避免银行家舍入）
    days = rounded // 86400
    if days == 0:
        return format_countdown_hm(target, now)
    hours = (rounded % 86400) / 3600
    if hours == 0:
        return f"{days}天"
    return f"{days}天{hours:g}小时"


def parse_reset_datetime(value: Any) -> Optional[datetime]:
    """宽松解析上游 reset 时间：epoch 秒/毫秒、ISO 字符串（含 Z / 时区偏移）。

    返回 aware UTC datetime；解析失败返回 None。
    无时区信息的字符串按本地时间解释（与前端 new Date(val) 行为一致）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            return None
        try:
            seconds = v / 1000.0 if v >= 1e12 else v
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _normalize_label(text: Any) -> str:
    return str(text or "").strip().lower().replace(" ", "").replace("　", "")


def _find_field(fields: list, path_suffix: str, label_keys: tuple) -> Optional[dict]:
    """按 path 后缀精确匹配，失败再按标签兜底（候选键从具体到宽泛）。"""
    for f in fields:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip().lower()
        if path.endswith(path_suffix):
            return f
    for key in label_keys:
        norm = _normalize_label(key)
        if not norm:
            continue
        for f in fields:
            if isinstance(f, dict) and norm in _normalize_label(f.get("label")):
                return f
    return None


def _remaining_pct(value: Any) -> Optional[float]:
    """0–1 的已用比例 → 0–100 的剩余百分比；超界值钳制到 [0, 100]。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    used = max(0.0, min(1.0, v))
    return (1.0 - used) * 100.0


def _has_kimi_marker(fields: list) -> bool:
    """服务里出现任一已知 path/标签即视为 Kimi Code 的候选。"""
    for f in fields:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip().lower()
        if path.endswith(FIVE_H_USED) or path.endswith(MONTH_TOTAL_USED) or path.endswith(MONTH_CODE_USED):
            return True
        label = _normalize_label(f.get("label"))
        for key in ("5小时剩余", "月总额度剩余", "月代码额度剩余"):
            if key and key in label:
                return True
    return False


def _safe_int(value: Any) -> Optional[int]:
    """宽松转 int；bool/非数值/转换失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def poll_step(prev_sig, cur_sig, active: bool, quiet_streak: int,
              epsilon: float = 0.1, quiet_limit: int = 3) -> "tuple[bool, int]":
    """自适应双速轮询的档位判定（纯函数，脱离 Qt 可测）。

    输入上一次/当前额度快照与当前档位状态，返回 (是否应处于活动态, 新静默计数)：
    - 任一档 |Δ|>epsilon 或 None⇄有值 → (True, 0)（激活并清零静默计数）；
    - 无变化且活动态：静默计数 +1，达到 quiet_limit → (False, 0)（回落空闲）；
    - 无变化且空闲态：原样返回；
    - prev_sig 为 None（首次基线）：只当基线用，不触发档位变化。
    """
    if prev_sig is None:
        return (active, quiet_streak)
    changed = False
    for prev, cur in zip(prev_sig, cur_sig):
        if (prev is None) != (cur is None):
            changed = True
        elif prev is None and cur is None:
            continue
        elif (
            isinstance(prev, (int, float))
            and not isinstance(prev, bool)
            and isinstance(cur, (int, float))
            and not isinstance(cur, bool)
        ):
            if abs(cur - prev) > epsilon:
                changed = True
        elif str(prev) != str(cur):
            changed = True
    if changed:
        return (True, 0)
    if active:
        streak = quiet_streak + 1
        if streak >= quiet_limit:
            return (False, 0)
        return (True, streak)
    return (False, quiet_streak)


def _parse_kimi_from_service(svc: dict, now: datetime) -> KimiQuota:
    result = svc.get("result")
    if not isinstance(result, dict):
        result = {}
    data = result.get("data")
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, list):
        fields = []

    f5h = _find_field(fields, FIVE_H_USED, _LABEL_FALLBACKS[FIVE_H_USED])
    fmt = _find_field(fields, MONTH_TOTAL_USED, _LABEL_FALLBACKS[MONTH_TOTAL_USED])
    fmc = _find_field(fields, MONTH_CODE_USED, _LABEL_FALLBACKS[MONTH_CODE_USED])
    fr5 = _find_field(fields, FIVE_H_RESET, _LABEL_FALLBACKS[FIVE_H_RESET])
    frm = _find_field(fields, MONTH_RESET, _LABEL_FALLBACKS[MONTH_RESET])

    fetched_at = now
    raw_fetched = result.get("fetched_at")
    if isinstance(raw_fetched, str):
        parsed = parse_reset_datetime(raw_fetched)
        if parsed is not None:
            fetched_at = parsed

    stale = bool(result.get("stale_error")) or str(result.get("status") or "").strip().lower() != "ok"

    return KimiQuota(
        five_hour_remaining=_remaining_pct(f5h.get("value")) if f5h else None,
        month_total_remaining=_remaining_pct(fmt.get("value")) if fmt else None,
        month_code_remaining=_remaining_pct(fmc.get("value")) if fmc else None,
        reset_5h=parse_reset_datetime(fr5.get("value")) if fr5 else None,
        reset_month=parse_reset_datetime(frm.get("value")) if frm else None,
        fetched_at=fetched_at,
        stale=stale,
        service_id=_safe_int(svc.get("id")),
    )


def parse_kimi_quota(payload: Any, now: Optional[datetime] = None) -> Optional[KimiQuota]:
    """从 /api/usage 的服务数组解析 Kimi Code；找不到对应服务返回 None。

    result.stale_error 非空或 status != "ok" 时标记 stale=True（数据是缓存旧值）。
    """
    if not isinstance(payload, list):
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.astimezone()

    candidates = []
    for svc in payload:
        if not isinstance(svc, dict):
            continue
        if str(svc.get("service_type") or "").strip().lower() not in ("custom", ""):
            continue
        result = svc.get("result")
        if not isinstance(result, dict):
            continue
        data = result.get("data")
        fields = data.get("fields") if isinstance(data, dict) else None
        if not isinstance(fields, list) or not fields:
            continue
        if _has_kimi_marker(fields):
            candidates.append(svc)

    if not candidates:
        return None

    # 多个 custom 服务命中时，优先名字里带 kimi 的
    chosen = candidates[0]
    for cand in candidates:
        if "kimi" in str(cand.get("name") or "").strip().lower():
            chosen = cand
            break
    return _parse_kimi_from_service(chosen, now)


def _opencode_remaining(percent_used: Any) -> Optional[float]:
    """OpenCode 的 percent_used 是 0–100 的**已用**百分比（与 Kimi 的 0–1
    小数不同）：remaining = 100 − clamp(percent_used)。"""
    if percent_used is None or isinstance(percent_used, bool):
        return None
    try:
        v = float(percent_used)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return 100.0 - max(0.0, min(100.0, v))


def _decimal(value: Any) -> Optional[Decimal]:
    """宽松转 Decimal（上游金额是字符串）；bool/非法/非有限值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def parse_opencode_quota(service: dict, now: Optional[datetime] = None) -> OpenCodeQuota:
    """解析 OpenCode 服务（result.data.five_hour/week/month，每档含
    percent_used / used / limit / resets_at）。"""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.astimezone()
    result = service.get("result") if isinstance(service, dict) else None
    if not isinstance(result, dict):
        result = {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}

    def tier(name: str):
        block = data.get(name) if isinstance(data.get(name), dict) else {}
        return (
            _opencode_remaining(block.get("percent_used")),
            parse_reset_datetime(block.get("resets_at")),
        )

    five_hour = tier("five_hour")
    week = tier("week")
    month = tier("month")
    stale = bool(result.get("stale_error")) or str(result.get("status") or "").strip().lower() != "ok"

    return OpenCodeQuota(
        five_hour_remaining=five_hour[0],
        week_remaining=week[0],
        month_remaining=month[0],
        reset_5h=five_hour[1],
        reset_week=week[1],
        reset_month=month[1],
        fetched_at=now,
        stale=stale,
        service_id=_safe_int(service.get("id")) if isinstance(service, dict) else None,
    )


def parse_deepseek_quota(service: dict, now: Optional[datetime] = None) -> DeepSeekQuota:
    """解析 DeepSeek 服务（result.data.is_available + balance_infos[]，取首条）。"""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.astimezone()
    result = service.get("result") if isinstance(service, dict) else None
    if not isinstance(result, dict):
        result = {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    infos = data.get("balance_infos")
    first = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}

    is_available = data.get("is_available")
    if is_available is not None and not isinstance(is_available, bool):
        is_available = str(is_available).strip().lower() in ("1", "true")

    return DeepSeekQuota(
        balance=_decimal(first.get("total_balance")),
        topped_up_balance=_decimal(first.get("topped_up_balance")),
        granted_balance=_decimal(first.get("granted_balance")),
        currency=str(first.get("currency") or ""),
        is_available=is_available,
        fetched_at=now,
        stale=bool(result.get("stale_error")) or str(result.get("status") or "").strip().lower() != "ok",
        service_id=_safe_int(service.get("id")) if isinstance(service, dict) else None,
    )
