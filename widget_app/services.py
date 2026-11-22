"""Normalize OpenUsage services for the widget, preserving each service ID."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .api_client import ApiError, OpenUsageError
from .quota import (
    _decimal, _has_kimi_marker, _opencode_remaining, _parse_kimi_from_service,
    _safe_int, parse_deepseek_quota, parse_opencode_quota, parse_reset_datetime,
)

CURRENCY = {"CNY": "¥", "USD": "$", "EUR": "€"}
LEGACY_ORDER = ("kimi", "opencode", "deepseek")


@dataclass(frozen=True)
class Metric:
    label: str
    value: str = ""
    remaining: float | None = None
    reset: datetime | None = None
    window_seconds: int | None = None


@dataclass(frozen=True)
class Service:
    id: int
    name: str
    kind: str
    hero: Metric | None = None
    details: tuple[Metric, ...] = ()
    stale: bool = False
    error: str = ""
    unavailable: bool = False

    @property
    def signature(self):
        slots = (self.hero, *self.details, None, None)[:3]
        metrics = tuple(
            part for metric in slots
            for part in ((metric.label, metric.remaining, metric.value) if metric else ("", None, ""))
        )
        return (str(self.unavailable), *metrics)


def _object(value):
    return value if isinstance(value, dict) else {}


def _array(value):
    return value if isinstance(value, list) else []


def _amount(value, symbol=""):
    number = _decimal(value)
    return f"{symbol}{number}" if number is not None else ""


def _meter(label, used, reset=None, window=None):
    remaining = _opencode_remaining(used)
    if remaining is not None:
        return Metric(label, remaining=remaining, reset=parse_reset_datetime(reset), window_seconds=window)
    if reset is not None and (at := parse_reset_datetime(reset)) is not None:
        return Metric(label, reset=at)
    return None


def _custom_metrics(data):
    for field in _array(data.get("fields")):
        if not isinstance(field, dict):
            continue
        kind = field.get("type")
        label = str(field.get("label") or "").strip()
        if not label:
            continue
        value = field.get("value")
        if kind in ("balance", "currency"):
            if amount := _amount(value, str(field.get("symbol") or "")):
                yield Metric(label, value=amount)
        elif kind in ("percentage", "percentage_fraction", "percentage_fraction_ring",
                      "progress", "progress_bar", "progress_remaining", "progress_bar_remaining"):
            number = _decimal(value)
            if number is None:
                continue
            if kind.startswith("progress"):
                total = _decimal(field.get("total"))
                if total is None or total <= 0:
                    continue
                ratio = max(0, min(1, number / total))
                number = (1 - ratio if kind.endswith("_remaining") else ratio) * 100
            elif kind.startswith("percentage_fraction"):
                number *= 100
            if metric := _meter(label, number):
                yield metric
        elif kind == "reset_date" and (at := parse_reset_datetime(value)) is not None:
            yield Metric(label, reset=at)


def _provider_metrics(kind, data):
    if kind == "custom":
        return list(_custom_metrics(data))
    if kind in ("claude", "chatgpt"):
        return [m for key, label, duration in (
            ("five_hour", "5 小时", 18000), ("seven_day", "7 天", 604800)
        ) if (m := _meter(label, _object(data.get(key)).get("utilization"),
                          _object(data.get(key)).get("resets_at"), duration))]
    if kind == "gemini":
        return [m for raw in _array(data.get("limits"))
                if (row := _object(raw))
                and (m := _meter({"session": "会话额度", "weekly": "每周"}.get(row.get("label"), str(row.get("label") or "额度")),
                                row.get("percent_used"), row.get("resets_at")))]
    if kind == "copilot":
        metrics = []
        for key, label in (("premium_requests", "高级请求"), ("chat", "聊天"), ("completions", "补全")):
            row = _object(data.get(key))
            value = _decimal(row.get("percent_remaining"))
            if value is not None and not row.get("unlimited"):
                metrics.append(Metric(label, remaining=float(max(0, min(100, value)))))
        return metrics
    if kind == "mistral":
        return [m for key, label in (("vibe", "Vibe 用量"), ("api", "Studio 用量"))
                if (row := _object(data.get(key))) and (m := _meter(label, row.get("percent_used"), row.get("resets_at")))]
    if kind == "ollama_cloud":
        metric = _meter("会话额度", data.get("percent_used"), data.get("resets_at"))
        return [metric] if metric else []
    if kind == "openai":
        currency = str(data.get("currency") or "USD").upper()
        symbol = CURRENCY.get(currency, f"{currency} ")
        return [Metric(label, value=amount) for label, value in (
            ("余额", _object(data.get("credit_balance")).get("total_available")),
            ("本月消费", data.get("spend_this_month")),
        ) if (amount := _amount(value, symbol))]
    return []


def parse_services(payload, now=None):
    if not isinstance(payload, list):
        raise ApiError("用量接口未返回服务列表")
    now = now or datetime.now(timezone.utc)
    services = {}
    for raw in payload:
        row = _object(raw)
        raw_id = row.get("id")
        service_id = _safe_int(raw_id) if type(raw_id) in (int, str) else None
        if service_id is None or service_id <= 0:
            continue
        result = _object(row.get("result"))
        data = _object(result.get("data"))
        kind = str(row.get("service_type") or "custom").lower()
        name = str(row.get("name") or kind).strip() or kind
        fields = data.get("fields")
        if kind == "custom" and isinstance(fields, list) and _has_kimi_marker(fields):
            kind = "kimi"
        error = str(result.get("error") or result.get("stale_error") or "")
        stale = bool(result.get("stale_error")) or result.get("status") != "ok"
        hero, details, unavailable = None, (), False
        if data:
            if kind == "kimi":
                q = _parse_kimi_from_service(row, now)
                hero = Metric("5 小时", remaining=q.five_hour_remaining, reset=q.reset_5h, window_seconds=18000)
                details = (Metric("月总额", remaining=q.month_total_remaining),
                           Metric("月代码", remaining=q.month_code_remaining, reset=q.reset_month))
            elif kind == "opencode":
                q = parse_opencode_quota(row, now)
                hero = Metric("5 小时", remaining=q.five_hour_remaining, reset=q.reset_5h, window_seconds=18000)
                details = (Metric("每周", remaining=q.week_remaining),
                           Metric("每月", remaining=q.month_remaining, reset=q.reset_month))
                if q.five_hour_remaining is None and q.week_remaining is None and q.month_remaining is None:
                    hero = Metric("Zen 余额", value=_amount(data.get("balance"), "$"))
                    details = (Metric("本月消费", value=_amount(data.get("cost_month"), "$")),)
            elif kind == "deepseek":
                q = parse_deepseek_quota(row, now)
                symbol = CURRENCY.get(q.currency, f"{q.currency} " if q.currency else "")
                hero = Metric("余额", value=_amount(q.balance, symbol))
                details = (Metric("充值余额", value=_amount(q.topped_up_balance, symbol)),
                           Metric("赠送余额", value=_amount(q.granted_balance, symbol)))
                unavailable = q.is_available is False
            else:
                metrics = _provider_metrics(kind, data)
                if metrics:
                    hero, *details = metrics[:3]
        services[service_id] = Service(service_id, name, kind, hero, tuple(details), stale, error, unavailable)
    if payload and not services:
        raise ApiError("用量接口未返回可识别的服务 ID")
    return services


def merge_services(fresh, previous):
    """A provider error can omit the payload while the last good sample is still useful."""
    out = {}
    for service_id, service in fresh.items():
        old = previous.get(service_id)
        if service.hero is None and service.stale and old is not None:
            service = replace(service, kind=old.kind, hero=old.hero, details=old.details, unavailable=old.unavailable)
        out[service_id] = service
    return out


def fetch_services(client, targets):
    """An inventory read uses the server cache. Force only selected, still-present IDs."""
    services = parse_services(client.get_usage())
    for service_id in targets:
        if service_id not in services:
            continue
        try:
            update = parse_services([client.get_service_usage(service_id, force=True)])
            if service_id not in update:
                raise ApiError("刷新返回的服务与请求不一致")
            services.update(merge_services({service_id: update[service_id]}, services))
        except OpenUsageError as exc:
            services[service_id] = replace(services[service_id], stale=True, error=str(exc))
    return services


def default_ids(services, legacy=False):
    if not legacy:
        return list(services)
    return [s.id for kind in LEGACY_ORDER for s in services.values() if s.kind == kind]


def selected_ids(services, preferences, legacy=False):
    panel = [i for i in preferences.get("panel_ids", default_ids(services, legacy)) if i in services]
    mini = preferences.get("mini_ids", panel[:2])
    return panel, [i for i in mini if i in panel][:2]

