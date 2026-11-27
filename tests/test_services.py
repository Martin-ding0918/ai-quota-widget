import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from widget_app import config
from widget_app.api_client import ApiError
from widget_app.quota import poll_step
from widget_app.services import default_ids, fetch_services, merge_services, parse_services, selected_ids


def row(sid, kind, name, data=None, **result):
    return {"id": sid, "name": name, "service_type": kind,
            "result": {"data": data or {}, "status": "ok", **result}}


class TestServices(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row(12, "opencode", "OpenCode", {"five_hour": {"percent_used": 12}}),
            row(26, "opencode", "OpenCode", {"five_hour": {"percent_used": 68}}),
            row(31, "deepseek", "DeepSeek",
                {"balance_infos": [{"currency": "CNY", "total_balance": "25.92"}]}),
            row(45, "custom", "额度账户", {"fields": [
                {"type": "balance", "label": "余额", "value": "42", "symbol": "$"},
                {"type": "progress_bar_remaining", "label": "本月", "value": 20, "total": 50},
                {"type": "text", "label": "无语义", "value": "127"},
                {"type": "reset_date", "label": "重置", "value": "2026-12-01T00:00:00Z"},
            ]}),
        ]

    def test_duplicates_and_custom_data_keep_independent_cards(self):
        services = parse_services(self.rows)
        self.assertEqual(list(services), [12, 26, 31, 45])
        self.assertEqual(services[12].name, services[26].name)
        self.assertEqual(services[12].hero.remaining, 88)
        self.assertEqual(services[26].hero.remaining, 32)
        self.assertEqual(services[31].hero.value, "¥25.92")
        self.assertEqual(services[45].hero.value, "$42")
        self.assertEqual(services[45].details[0].remaining, 40)
        self.assertEqual(services[45].details[1].reset.day, 1)

    def test_kimi_recognized_by_field_and_keeps_display_name(self):
        service = parse_services([row(21, "custom", "我的账号", {"fields": [
            {"type": "percentage_fraction_ring", "path": "usages.limit_5h.used_ratio",
             "label": "5 小时额度", "value": 0.25},
            {"type": "reset_date", "path": "usages.limit_5h.reset_time", "label": "重置",
             "value": "2026-12-01T00:00:00Z"},
        ]})])[21]
        self.assertEqual((service.name, service.kind, service.hero.remaining), ("我的账号", "kimi", 75))
        self.assertEqual(service.hero.window_seconds, 18000)

    def test_gemini_labels_are_normalized_case_insensitively(self):
        def hero_label(raw):
            return parse_services([row(7, "gemini", "Gemini", {"limits": [
                {"label": raw, "percent_used": 20},
            ]})])[7].hero.label

        self.assertEqual(hero_label("Session"), "会话额度")
        self.assertEqual(hero_label("Weekly"), "每周")
        self.assertEqual(hero_label("Session "), "会话额度")
        self.assertEqual(hero_label("Custom"), "Custom")
        self.assertEqual(hero_label("   "), "额度")

    def test_month_and_week_detail_rows_carry_resets(self):
        opencode = parse_services([row(5, "opencode", "OpenCode", {
            "week": {"percent_used": 11, "resets_at": "2026-11-28T00:00:00Z"},
            "month": {"percent_used": 5, "resets_at": "2026-12-01T00:00:00Z"},
        })])[5]
        self.assertEqual((opencode.details[0].reset.day, opencode.details[1].reset.day), (28, 1))
        kimi = parse_services([row(21, "custom", "Kimi", {"fields": [
            {"type": "percentage_fraction", "path": "usages.limit_month_total.used_ratio", "label": "月总", "value": 0.5},
            {"type": "percentage_fraction", "path": "usages.limit_month_code.used_ratio", "label": "月代码", "value": 0.3},
            {"type": "reset_date", "path": "usages.limit_month.reset_time", "label": "月重置",
             "value": "2026-12-01T00:00:00Z"},
        ]})])[21]
        self.assertEqual((kimi.details[0].reset.day, kimi.details[1].reset.day), (1, 1))

    def test_defaults_selection_rename_and_temporary_absence(self):
        services = parse_services(self.rows)
        self.assertEqual(default_ids(services, legacy=True), [12, 26, 31])
        self.assertEqual(selected_ids(services, {}), ([12, 26, 31, 45], [12, 26]))
        prefs = {"panel_ids": [45, 26, 12], "mini_ids": [26, 45]}
        self.assertEqual(selected_ids(services, prefs), ([45, 26, 12], [26, 45]))
        services = parse_services([self.rows[0], {**self.rows[3], "name": "改名"}])
        self.assertEqual(selected_ids(services, prefs), ([45, 12], [45]))
        self.assertEqual(services[45].name, "改名")
        self.assertEqual(prefs["panel_ids"], [45, 26, 12])
        self.assertEqual(selected_ids(services, {"panel_ids": []}), ([], []))

    def test_idle_cached_and_manual_only_for_selected_ids(self):
        client = Mock()
        client.get_usage.return_value = self.rows
        client.get_service_usage.side_effect = [ApiError("回源失败"), self.rows[3]]
        self.assertEqual(len(fetch_services(client, [])), 4)
        client.get_service_usage.assert_not_called()
        snapshots = fetch_services(client, [12, 999, 45])
        self.assertEqual([c.args[0] for c in client.get_service_usage.call_args_list], [12, 45])
        self.assertTrue(all(c.kwargs == {"force": True} for c in client.get_service_usage.call_args_list))
        self.assertEqual(snapshots[12].error, "回源失败")
        self.assertEqual(snapshots[12].hero.remaining, 88)
        self.assertFalse(snapshots[45].stale)
        self.assertTrue(all(not c.args and not c.kwargs for c in client.get_usage.call_args_list))

    def test_provider_failure_keeps_last_good_sample_only_for_matching_id(self):
        original = parse_services(self.rows)
        failing = parse_services([row(12, "opencode", "改名", error="失败", status="error")])
        merged = merge_services(failing, original)
        self.assertEqual(list(merged), [12])
        self.assertEqual(merged[12].hero.remaining, 88)
        self.assertEqual(merged[12].name, "改名")
        self.assertTrue(merged[12].stale)

    def test_generic_provider_does_not_guess_unrecognized_usage(self):
        unknown = row(1, "unknown", "Unknown", {"remaining": 90})
        claude = row(2, "claude", "Claude", {"five_hour": {"utilization": 53}})
        services = parse_services([unknown, claude])
        self.assertIsNone(services[1].hero)
        self.assertEqual(services[2].hero.remaining, 47)

    def test_malformed_list_does_not_look_like_account_with_no_services(self):
        with self.assertRaises(ApiError):
            parse_services([{"result": {"status": "ok"}}])
        self.assertEqual(parse_services([]), {})

    def test_malformed_provider_fields_do_not_blank_other_cards(self):
        services = parse_services([row(99, "custom", "坏字段", {"fields": 7}), self.rows[0]])
        self.assertIsNone(services[99].hero)
        self.assertEqual(services[12].hero.remaining, 88)

    def test_adding_an_optional_metric_activates_just_that_service(self):
        base = row(45, "custom", "额度账户", {"fields": [
            {"type": "balance", "label": "余额", "value": "42", "symbol": "$"},
        ]})
        old = parse_services([base, self.rows[0]])
        new = parse_services([self.rows[3], self.rows[0]])
        self.assertEqual(poll_step(old[45].signature, new[45].signature, False, 0), (True, 0))
        self.assertEqual(poll_step(old[12].signature, new[12].signature, False, 0), (False, 0))

    def test_antigravity_picks_tightest_5h_hero_other_5h_and_tightest_weekly(self):
        ag = parse_services([row(70, "antigravity", "Antigravity", {
            "plan": "Google AI Pro",
            "limits": [
                {"id": "gemini-5h", "group": "gemini", "group_name": "Gemini Models", "window": "5h",
                 "percent_used": 0.0, "percent_remaining": 100.0, "resets_at": None},
                {"id": "gemini-weekly", "group": "gemini", "group_name": "Gemini Models", "window": "weekly",
                 "percent_used": 15.0, "percent_remaining": 85.0, "resets_at": "2026-12-02T00:00:00Z"},
                {"id": "3p-5h", "group": "claude_gpt", "group_name": "Claude and GPT models", "window": "5h",
                 "percent_used": 60.0, "percent_remaining": 40.0, "resets_at": "2026-11-26T20:00:00Z"},
                {"id": "3p-weekly", "group": "claude_gpt", "group_name": "Claude and GPT models", "window": "weekly",
                 "percent_used": 50.5, "percent_remaining": 49.5, "resets_at": "2026-12-01T13:10:00Z"},
                {"id": "gemini-argon-wtu-weekly", "group": "argon", "group_name": "Gemini 4 Argon", "window": "weekly",
                 "percent_used": 100.0, "percent_remaining": 0.0, "resets_at": "2026-12-01T18:33:00Z"},
            ],
        })])[70]
        self.assertEqual(ag.hero.label, "Claude 5h")
        self.assertEqual(ag.hero.remaining, 40.0)
        self.assertEqual(ag.hero.window_seconds, 18000)
        self.assertEqual(ag.hero.reset.hour, 20)
        self.assertEqual([d.label for d in ag.details], ["Gemini 5h", "Argon 周"])
        self.assertEqual([d.remaining for d in ag.details], [100.0, 0.0])
        self.assertIsNone(ag.details[0].reset)
        self.assertEqual(ag.details[1].reset.day, 1)

    def test_antigravity_groups_fallback_and_untouched_tiebreak(self):
        ag = parse_services([row(71, "antigravity", "AG", {
            "groups": [
                {"id": "gemini", "name": "Gemini Models", "buckets": [
                    {"id": "gemini-5h", "window": "5h", "percent_used": 0.0, "resets_at": None},
                    {"id": "gemini-weekly", "window": "weekly", "percent_used": 0.0, "resets_at": None},
                ]},
                {"id": "claude_gpt", "name": "Claude and GPT models", "buckets": [
                    {"id": "3p-5h", "window": "5h", "percent_used": 0.0, "resets_at": None},
                    {"id": "3p-weekly", "window": "weekly", "percent_used": 0.0, "resets_at": None},
                ]},
            ],
        })])[71]
        self.assertEqual(ag.hero.label, "Gemini 5h")
        self.assertEqual(ag.hero.remaining, 100.0)
        self.assertIsNone(ag.hero.reset)
        self.assertEqual([d.label for d in ag.details], ["Claude 5h", "Gemini 周"])

    def test_antigravity_weekly_only_and_empty_payload(self):
        weekly_only = parse_services([row(72, "antigravity", "AG Weekly", {
            "limits": [
                {"id": "gemini-argon-wtu-weekly", "group": "argon", "window": "weekly", "percent_used": 80.0},
                {"id": "gemini-weekly", "group": "gemini", "window": "weekly", "percent_used": 20.0},
            ],
        })])[72]
        self.assertEqual(weekly_only.hero.label, "Argon 周")
        self.assertEqual(weekly_only.hero.remaining, 20.0)
        self.assertEqual(weekly_only.hero.window_seconds, 604800)
        self.assertEqual(len(weekly_only.details), 1)
        self.assertEqual(weekly_only.details[0].label, "Gemini 周")

        empty = parse_services([row(73, "antigravity", "AG Empty", {"limits": []})])[73]
        self.assertIsNone(empty.hero)
        self.assertEqual(empty.details, ())


class TestPreferences(unittest.TestCase):
    def test_upgrade_marker_survives_save_until_first_successful_migration(self):
        with tempfile.TemporaryDirectory() as name, patch.object(config, "CONFIG_DIR", Path(name)), \
                patch.object(config, "CONFIG_PATH", Path(name) / "config.json"):
            config.CONFIG_PATH.write_text('{"server_url":"https://usage.example","username":"Alice"}')
            cfg = config.load_config()
            self.assertEqual(cfg["legacy_service_scope"], "https://usage.example|alice")
            config.save_config(cfg)
            self.assertEqual(config.load_config()["legacy_service_scope"], cfg["legacy_service_scope"])

    def test_scoped_order_and_explicit_empty_persist_without_credentials(self):
        with tempfile.TemporaryDirectory() as name, patch.object(config, "CONFIG_DIR", Path(name)), \
                patch.object(config, "CONFIG_PATH", Path(name) / "config.json"):
            cfg = config.load_config()
            cfg["service_preferences"] = {
                "https://usage.example|alice": {"panel_ids": [31, 12, 31, False], "mini_ids": [12]},
                "https://usage.example|bob": {"panel_ids": [], "mini_ids": []},
            }
            cfg["token"] = "private"
            config.save_config(cfg)
            actual = config.load_config()
            self.assertEqual(actual["service_preferences"]["https://usage.example|alice"]["panel_ids"], [31, 12])
            self.assertEqual(actual["service_preferences"]["https://usage.example|bob"]["panel_ids"], [])
            self.assertNotIn("private", config.CONFIG_PATH.read_text())

