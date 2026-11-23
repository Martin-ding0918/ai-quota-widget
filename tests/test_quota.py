"""test_quota.py — 解析与剩余换算的单测（本机 Windows 跑，目标平台即 Windows）。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from widget_app.quota import (
    KimiQuota,
    countdown_ratio,
    format_countdown_dh,
    format_countdown_hm,
    parse_deepseek_quota,
    parse_kimi_quota,
    parse_opencode_quota,
    parse_reset_datetime,
    poll_step,
)

NOW = datetime(2026, 11, 20, 4, 0, 0, tzinfo=timezone.utc)
SIG = (70.0, 50.0, 50.0)


def make_service(fields, *, name="Kimi Code", service_type="custom", extra_result=None, svc_id=3):
    result = {"configured": True, "status": "ok", "data": {"fields": fields}, "cached": True}
    if extra_result:
        result.update(extra_result)
    return {"id": svc_id, "service_type": service_type, "name": name, "result": result}


def kimi_fields(**overrides):
    fields = [
        {"label": "5 小时剩余", "path": "usages.limit_5h.used_ratio",
         "type": "percentage_fraction_ring", "value": 0.17},
        {"label": "月总额度剩余", "path": "usages.limit_month_total.used_ratio",
         "type": "percentage_fraction", "value": 0.18},
        {"label": "月代码额度剩余", "path": "usages.limit_month_code.used_ratio",
         "type": "percentage_fraction", "value": 0.11},
        {"label": "5 小时重置", "path": "usages.limit_5h.reset_time",
         "type": "reset_date", "value": "2026-11-20T10:00:00+08:00"},
        {"label": "月额度重置", "path": "usages.limit_month_total.reset_time",
         "type": "reset_date", "value": "2026-12-01T00:00:00+08:00"},
    ]
    for idx, repl in overrides.items():
        fields[idx] = repl
    return fields


class TestNormalParse(unittest.TestCase):
    def test_normal(self):
        payload = [make_service(kimi_fields(), extra_result={"fetched_at": "2026-11-20T03:55:00+00:00"})]
        q = parse_kimi_quota(payload, now=NOW)
        self.assertIsInstance(q, KimiQuota)
        # value 是已用比例 → 剩余 = (1 - value) * 100
        self.assertAlmostEqual(q.five_hour_remaining, 83.0)
        self.assertAlmostEqual(q.month_total_remaining, 82.0)
        self.assertAlmostEqual(q.month_code_remaining, 89.0)
        self.assertFalse(q.stale)
        self.assertEqual(q.fetched_at, datetime(2026, 11, 20, 3, 55, tzinfo=timezone.utc))
        # 两个重置时间：2026-11-20T10:00+08:00 → 02:00 UTC；2026-12-01T00:00+08:00 → 2026-11-30 16:00 UTC
        self.assertEqual(q.reset_5h, datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(q.reset_month, datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc))

    def test_service_selected_by_name_among_customs(self):
        other = make_service(
            [{"label": "余额", "path": "data.balance", "type": "currency", "value": 12.5}],
            name="DeepSeek", service_type="custom",
        )
        kimi = make_service(kimi_fields(), name="Kimi Code")
        q = parse_kimi_quota([other, kimi], now=NOW)
        self.assertAlmostEqual(q.month_code_remaining, 89.0)

    def test_marker_by_kimi_fields_not_name(self):
        # 名字不含 kimi 但字段命中（如改名叫 "月之代码"）也能识别
        svc = make_service(kimi_fields(), name="自定义服务")
        q = parse_kimi_quota([svc], now=NOW)
        self.assertIsNotNone(q)

    def test_path_suffix_without_usages_prefix(self):
        fields = [
            {"label": "5h", "path": "data.limit_5h.used_ratio", "type": "percentage_fraction", "value": 0.5},
            {"label": "月总", "path": "data.limit_month_total.used_ratio", "type": "percentage_fraction", "value": 0.2},
            {"label": "月代码", "path": "data.limit_month_code.used_ratio", "type": "percentage_fraction", "value": 0.3},
            {"label": "5h重置", "path": "data.limit_5h.reset_time", "type": "reset_date",
             "value": "2026-11-20T06:00:00Z"},
            {"label": "月重置", "path": "data.limit_month_total.reset_time", "type": "reset_date",
             "value": "2026-12-01T00:00:00Z"},
        ]
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertAlmostEqual(q.five_hour_remaining, 50.0)
        self.assertAlmostEqual(q.month_total_remaining, 80.0)
        self.assertAlmostEqual(q.month_code_remaining, 70.0)
        self.assertEqual(q.reset_5h, datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc))

    def test_label_fallback_when_paths_scrambled(self):
        fields = [
            {"label": "5小时剩余", "path": "x.y.z", "type": "text", "value": 0.25},
            {"label": "月总额度剩余", "path": "a.b", "type": "text", "value": 0.4},
            {"label": "月代码额度剩余", "path": "c.d", "type": "text", "value": 0.6},
            {"label": "5小时重置", "path": "e.f", "type": "text", "value": "2026-11-20T11:00:00+08:00"},
            {"label": "月额度重置", "path": "g.h", "type": "text", "value": "2026-12-01T00:00:00+08:00"},
        ]
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertAlmostEqual(q.five_hour_remaining, 75.0)
        self.assertAlmostEqual(q.month_total_remaining, 60.0)
        self.assertAlmostEqual(q.month_code_remaining, 40.0)
        self.assertEqual(q.reset_5h, datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc))

    def test_missing_fields_become_none(self):
        fields = [
            {"label": "5 小时剩余", "path": "usages.limit_5h.used_ratio", "type": "percentage_fraction", "value": 0.2},
        ]
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertAlmostEqual(q.five_hour_remaining, 80.0)
        self.assertIsNone(q.month_total_remaining)
        self.assertIsNone(q.month_code_remaining)
        self.assertIsNone(q.reset_5h)
        self.assertIsNone(q.reset_month)


class TestValueEdgeCases(unittest.TestCase):
    def test_value_out_of_range_clamped(self):
        fields = kimi_fields()
        fields[0]["value"] = 1.5   # 已用 >100%
        fields[1]["value"] = -0.2  # 已用为负
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertAlmostEqual(q.five_hour_remaining, 0.0)
        self.assertAlmostEqual(q.month_total_remaining, 100.0)

    def test_value_invalid_becomes_none(self):
        fields = kimi_fields()
        fields[0]["value"] = "abc"
        fields[1]["value"] = True       # bool 不算数字
        fields[2]["value"] = float("nan")
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertIsNone(q.five_hour_remaining)
        self.assertIsNone(q.month_total_remaining)
        self.assertIsNone(q.month_code_remaining)

    def test_value_zero_and_one(self):
        fields = kimi_fields()
        fields[0]["value"] = 0
        fields[1]["value"] = 1
        q = parse_kimi_quota([make_service(fields)], now=NOW)
        self.assertAlmostEqual(q.five_hour_remaining, 100.0)
        self.assertAlmostEqual(q.month_total_remaining, 0.0)


class TestStaleAndErrors(unittest.TestCase):
    def test_stale_error_flag(self):
        svc = make_service(kimi_fields(), extra_result={"stale_error": "上游超时"})
        q = parse_kimi_quota([svc], now=NOW)
        self.assertTrue(q.stale)
        # 旧值仍然可用
        self.assertAlmostEqual(q.five_hour_remaining, 83.0)

    def test_status_error_marks_stale(self):
        svc = make_service(kimi_fields(), extra_result={"status": "error"})
        q = parse_kimi_quota([svc], now=NOW)
        self.assertTrue(q.stale)

    def test_error_without_fields_returns_none_but_keep_quiet(self):
        svc = {"id": 3, "service_type": "custom", "name": "Kimi Code",
               "result": {"configured": True, "status": "error", "error": "上游 500"}}
        q = parse_kimi_quota([svc], now=NOW)
        self.assertIsNone(q)

    def test_no_kimi_service_returns_none(self):
        svc = make_service(
            [{"label": "余额", "path": "data.balance", "type": "currency", "value": 9.9}],
            name="DeepSeek",
        )
        self.assertIsNone(parse_kimi_quota([svc], now=NOW))
        self.assertIsNone(parse_kimi_quota([], now=NOW))
        self.assertIsNone(parse_kimi_quota({"not": "a list"}, now=NOW))
        self.assertIsNone(parse_kimi_quota(None, now=NOW))


class TestResetDatetimeParsing(unittest.TestCase):
    def test_iso_with_z(self):
        self.assertEqual(parse_reset_datetime("2026-11-20T06:00:00Z"),
                         datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc))

    def test_iso_with_offset(self):
        self.assertEqual(parse_reset_datetime("2026-11-20T10:00:00+08:00"),
                         datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc))

    def test_epoch_seconds_and_milliseconds(self):
        expected = datetime.fromtimestamp(1763600000, tz=timezone.utc)
        self.assertEqual(parse_reset_datetime(1763600000), expected)
        self.assertEqual(parse_reset_datetime(1763600000000), expected)

    def test_naive_string_interpreted_as_local(self):
        parsed = parse_reset_datetime("2026-11-20 18:00:00")
        expected = datetime(2026, 11, 20, 18, 0, 0).astimezone(timezone.utc)
        self.assertEqual(parsed, expected)

    def test_garbage_returns_none(self):
        for bad in (None, True, "", "not a date", -5, 0, "2026-13-99T99:99:99Z", [1, 2], {"x": 1}):
            self.assertIsNone(parse_reset_datetime(bad), msg=f"应当拒绝 {bad!r}")


class TestCountdownRatio(unittest.TestCase):
    """收起线长度编码：(reset − now) / 5h，clamp 0–1。"""

    def test_missing_reset_returns_none(self):
        self.assertIsNone(countdown_ratio(None, NOW))

    def test_reset_in_past_clamped_to_zero(self):
        self.assertEqual(countdown_ratio(NOW - timedelta(hours=2), NOW), 0.0)

    def test_zero_remaining(self):
        self.assertEqual(countdown_ratio(NOW, NOW), 0.0)

    def test_full_window_at_exactly_five_hours(self):
        self.assertAlmostEqual(countdown_ratio(NOW + timedelta(hours=5), NOW), 1.0)

    def test_beyond_five_hours_clamped_to_one(self):
        self.assertEqual(countdown_ratio(NOW + timedelta(hours=6), NOW), 1.0)

    def test_linear_shrink_two_hours_thirteen(self):
        r = countdown_ratio(NOW + timedelta(hours=2, minutes=13), NOW)
        self.assertAlmostEqual(r, (2 * 3600 + 13 * 60) / (5 * 3600))

    def test_thirty_minutes_is_tenth(self):
        self.assertAlmostEqual(countdown_ratio(NOW + timedelta(minutes=30), NOW), 0.1)


class TestFormatCountdownHm(unittest.TestCase):
    """面板裸倒计时（小时·分）。"""

    def test_none(self):
        self.assertEqual(format_countdown_hm(None, NOW), "--")

    def test_past_is_zero(self):
        self.assertEqual(format_countdown_hm(NOW - timedelta(minutes=5), NOW), "0分")

    def test_under_hour(self):
        self.assertEqual(format_countdown_hm(NOW + timedelta(minutes=43), NOW), "43分")

    def test_hours_minutes(self):
        self.assertEqual(format_countdown_hm(NOW + timedelta(hours=2, minutes=13), NOW), "2小时13分")

    def test_under_minute(self):
        self.assertEqual(format_countdown_hm(NOW + timedelta(seconds=30), NOW), "0分")

    def test_exact_hour(self):
        self.assertEqual(format_countdown_hm(NOW + timedelta(hours=1), NOW), "1小时0分")


class TestFormatCountdownDh(unittest.TestCase):
    """月倒计时（天·小时，半小时四舍五入；不足 1 天退化）。"""

    def test_none(self):
        self.assertEqual(format_countdown_dh(None, NOW), "--")

    def test_past_is_zero(self):
        self.assertEqual(format_countdown_dh(NOW - timedelta(hours=2), NOW), "0分")

    def test_under_day_degrades_to_hm(self):
        self.assertEqual(format_countdown_dh(NOW + timedelta(hours=5, minutes=12), NOW), "5小时12分")

    def test_days_hours(self):
        self.assertEqual(format_countdown_dh(NOW + timedelta(days=11, hours=3), NOW), "11天3小时")

    def test_whole_days(self):
        self.assertEqual(format_countdown_dh(NOW + timedelta(days=11), NOW), "11天")

    def test_half_hour_rounding(self):
        # 3h15m 四舍五入到 3.5h
        self.assertEqual(
            format_countdown_dh(NOW + timedelta(days=11, hours=3, minutes=15), NOW),
            "11天3.5小时",
        )

    def test_rounds_up_to_next_day(self):
        # 23h50m → 24h → 1天
        self.assertEqual(format_countdown_dh(NOW + timedelta(hours=23, minutes=50), NOW), "1天")


class TestServiceId(unittest.TestCase):
    """service_id 从 /api/usage 条目的 id 解析，供活动态单服务轮询使用。"""

    def test_normal(self):
        q = parse_kimi_quota([make_service(kimi_fields())], now=NOW)
        self.assertEqual(q.service_id, 3)

    def test_missing(self):
        q = parse_kimi_quota([make_service(kimi_fields(), svc_id=None)], now=NOW)
        self.assertIsNone(q.service_id)

    def test_non_numeric(self):
        q = parse_kimi_quota([make_service(kimi_fields(), svc_id="abc")], now=NOW)
        self.assertIsNone(q.service_id)

    def test_bool_rejected(self):
        q = parse_kimi_quota([make_service(kimi_fields(), svc_id=True)], now=NOW)
        self.assertIsNone(q.service_id)

    def test_float_accepted(self):
        q = parse_kimi_quota([make_service(kimi_fields(), svc_id=7.0)], now=NOW)
        self.assertEqual(q.service_id, 7)


class TestPollStep(unittest.TestCase):
    """双速轮询档位判定：变化激活 / None⇄有值 / 3 次静默回落 / 首次基线。"""

    def test_change_activates(self):
        self.assertEqual(poll_step((50, 50, 50), (50.2, 50, 50), False, 0), (True, 0))

    def test_tiny_change_stays_idle(self):
        # |Δ|=0.05 ≤ epsilon=0.1 → 无变化
        self.assertEqual(poll_step((50, 50, 50), (50.05, 50, 50), False, 0), (False, 0))

    def test_none_to_value_activates(self):
        self.assertEqual(poll_step((None, 50, 50), (83, 50, 50), False, 0), (True, 0))

    def test_value_to_none_activates(self):
        self.assertEqual(poll_step((83, 50, 50), (None, 50, 50), True, 0), (True, 0))

    def test_none_to_none_not_changed(self):
        self.assertEqual(poll_step((None, 50, 50), (None, 50, 50), False, 0), (False, 0))

    def test_three_quiet_polls_deactivate(self):
        self.assertEqual(poll_step(SIG, SIG, True, 0), (True, 1))
        self.assertEqual(poll_step(SIG, SIG, True, 1), (True, 2))
        self.assertEqual(poll_step(SIG, SIG, True, 2), (False, 0))

    def test_idle_quiet_unchanged(self):
        self.assertEqual(poll_step(SIG, SIG, False, 2), (False, 2))

    def test_first_baseline_no_activation(self):
        self.assertEqual(poll_step(None, SIG, False, 0), (False, 0))

    def test_change_resets_streak(self):
        self.assertEqual(poll_step(SIG, (65, 50, 50), True, 2), (True, 0))

    def test_custom_epsilon_and_limit(self):
        self.assertEqual(poll_step((50, 50, 50), (52, 50, 50), False, 0, epsilon=3), (False, 0))
        self.assertEqual(poll_step(SIG, SIG, True, 1, quiet_limit=2), (False, 0))

    def test_signature_change_shape_mismatch_ignored(self):
        # 快照长度异常时按 zip 截断处理，不抛异常
        self.assertEqual(poll_step((50,), (50, 50, 50), False, 0), (False, 0))


class TestPollStepStringComponents(unittest.TestCase):
    """DeepSeek 快照（币种/余额/可用状态）走字符串比较，不适用 epsilon。"""

    def test_currency_change_activates(self):
        self.assertEqual(poll_step(("CNY", "25.92", "1"), ("USD", "25.92", "1"), False, 0), (True, 0))

    def test_balance_change_activates(self):
        self.assertEqual(poll_step(("CNY", "25.92", "1"), ("CNY", "24.50", "1"), False, 0), (True, 0))

    def test_availability_change_activates(self):
        self.assertEqual(poll_step(("CNY", "25.92", "1"), ("CNY", "25.92", "0"), False, 0), (True, 0))

    def test_identical_strings_stay_idle(self):
        self.assertEqual(poll_step(("CNY", "25.92", "1"), ("CNY", "25.92", "1"), False, 0), (False, 0))

    def test_none_to_string_activates(self):
        self.assertEqual(poll_step((None, None, None), ("CNY", "25.92", "1"), False, 0), (True, 0))


class TestOpenCodeQuota(unittest.TestCase):
    """OpenCode：percent_used 为 0–100 已用百分比（与 Kimi 的 0–1 相反）。"""

    def setUp(self):
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        self.service = {
            "id": 5,
            "service_type": "opencode",
            "name": "OpenCode",
            "result": {"configured": True, "status": "ok", "data": {
                "five_hour": {"percent_used": 25.0, "used": 3.21, "limit": 12.0,
                              "resets_at": "2026-11-22T13:55:47.000Z"},
                "week": {"percent_used": 10.69, "resets_at": "2026-11-23T00:00:00.000Z"},
                "month": {"percent_used": 5.34, "resets_at": "2026-12-22T08:31:52.000Z"},
            }},
        }

    def test_percent_used_converts_to_remaining(self):
        q = parse_opencode_quota(self.service, now=self.now)
        self.assertAlmostEqual(q.five_hour_remaining, 75.0)
        self.assertAlmostEqual(q.week_remaining, 89.31)
        self.assertAlmostEqual(q.month_remaining, 94.66)

    def test_zero_and_full(self):
        self.service["result"]["data"]["five_hour"]["percent_used"] = 0
        self.assertAlmostEqual(parse_opencode_quota(self.service, now=self.now).five_hour_remaining, 100.0)
        self.service["result"]["data"]["five_hour"]["percent_used"] = 100
        self.assertAlmostEqual(parse_opencode_quota(self.service, now=self.now).five_hour_remaining, 0.0)

    def test_out_of_range_clamped(self):
        self.service["result"]["data"]["five_hour"]["percent_used"] = 250
        self.assertAlmostEqual(parse_opencode_quota(self.service, now=self.now).five_hour_remaining, 0.0)
        self.service["result"]["data"]["five_hour"]["percent_used"] = -5
        self.assertAlmostEqual(parse_opencode_quota(self.service, now=self.now).five_hour_remaining, 100.0)

    def test_invalid_percent_becomes_none(self):
        self.service["result"]["data"]["five_hour"]["percent_used"] = "abc"
        self.assertIsNone(parse_opencode_quota(self.service, now=self.now).five_hour_remaining)

    def test_resets_at_parsed(self):
        q = parse_opencode_quota(self.service, now=self.now)
        self.assertEqual(q.reset_5h, datetime(2026, 11, 22, 13, 55, 47, tzinfo=timezone.utc))
        self.assertEqual(q.reset_week, datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc))

    def test_missing_tiers_become_none(self):
        self.service["result"]["data"] = {"five_hour": {"percent_used": 10}}
        q = parse_opencode_quota(self.service, now=self.now)
        self.assertAlmostEqual(q.five_hour_remaining, 90.0)
        self.assertIsNone(q.week_remaining)
        self.assertIsNone(q.reset_week)

    def test_service_id_and_stale(self):
        self.service["result"]["stale_error"] = "上游超时"
        q = parse_opencode_quota(self.service, now=self.now)
        self.assertEqual(q.service_id, 5)
        self.assertTrue(q.stale)

    def test_resets_at_none_means_no_countdown(self):
        # resets_at=None：外环只画轨道，不伪装成有效倒计时
        self.service["result"]["data"]["five_hour"].pop("resets_at")
        q = parse_opencode_quota(self.service, now=self.now)
        self.assertIsNone(q.reset_5h)


class TestDeepSeekQuota(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        self.service = {
            "id": 2,
            "service_type": "deepseek",
            "name": "DeepSeek",
            "result": {"configured": True, "status": "ok", "data": {
                "is_available": True,
                "balance_infos": [{"currency": "CNY", "total_balance": "25.92",
                                   "granted_balance": "0.00", "topped_up_balance": "25.92"}],
            }},
        }

    def test_balance_fields_as_decimal(self):
        q = parse_deepseek_quota(self.service, now=self.now)
        self.assertEqual(str(q.balance), "25.92")
        self.assertEqual(str(q.topped_up_balance), "25.92")
        self.assertEqual(str(q.granted_balance), "0.00")
        self.assertEqual(q.currency, "CNY")
        self.assertTrue(q.is_available)
        self.assertEqual(q.service_id, 2)

    def test_availability_false_and_string_truthy(self):
        self.service["result"]["data"]["is_available"] = False
        self.assertFalse(parse_deepseek_quota(self.service, now=self.now).is_available)
        self.service["result"]["data"]["is_available"] = "1"
        self.assertTrue(parse_deepseek_quota(self.service, now=self.now).is_available)
        self.service["result"]["data"]["is_available"] = "false"
        self.assertFalse(parse_deepseek_quota(self.service, now=self.now).is_available)

    def test_empty_balance_infos(self):
        self.service["result"]["data"]["balance_infos"] = []
        q = parse_deepseek_quota(self.service, now=self.now)
        self.assertIsNone(q.balance)
        self.assertEqual(q.currency, "")
        self.assertTrue(q.is_available)  # is_available 在 data 顶层，不受影响

    def test_invalid_amount_becomes_none(self):
        self.service["result"]["data"]["balance_infos"][0]["total_balance"] = "abc"
        self.assertIsNone(parse_deepseek_quota(self.service, now=self.now).balance)

    def test_missing_balance_infos(self):
        self.service["result"]["data"].pop("balance_infos")
        q = parse_deepseek_quota(self.service, now=self.now)
        self.assertIsNone(q.balance)
        self.assertEqual(q.currency, "")


if __name__ == "__main__":
    unittest.main()

