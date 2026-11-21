"""test_config.py — 凭据作用域绑定（origin+用户名）的单测。

重点：更换服务器/用户名后不得复用旧凭据；token 按作用域分桶；旧版单 token 字段
同源迁移；旧版仅按用户名保存的 keyring 密码不自动迁移（保存新密码时清理）。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from widget_app import config


class FakeKeyring:
    """内存 keyring 替身：记录 set/get/delete 调用；fail_read_keys 里的键
    get_password 永远返回 None（模拟写后读校验失败）。"""

    def __init__(self):
        self.store = {}
        self.deleted = []
        self.fail_read_keys = set()

    def get_password(self, service, key):
        if key in self.fail_read_keys:
            return None
        return self.store.get(key)

    def set_password(self, service, key, value):
        self.store[key] = value

    def delete_password(self, service, key):
        self.deleted.append(key)
        if key in self.store:
            del self.store[key]


class TestNormalizeOrigin(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(config.normalize_origin("http://10.0.0.100:4188"), "http://10.0.0.100:4188")

    def test_case_and_trailing_slash_and_path(self):
        self.assertEqual(
            config.normalize_origin("HTTP://Example.COM:4188/openusage/"),
            "http://example.com:4188",
        )

    def test_default_port_omitted(self):
        self.assertEqual(config.normalize_origin("https://example.com:443"), "https://example.com")
        self.assertEqual(config.normalize_origin("http://example.com:80/"), "http://example.com")

    def test_non_default_port_kept(self):
        self.assertEqual(config.normalize_origin("https://example.com:8443"), "https://example.com:8443")

    def test_bare_host_with_port(self):
        self.assertEqual(config.normalize_origin("10.0.0.100:4188"), "http://10.0.0.100:4188")

    def test_empty_or_garbage(self):
        self.assertEqual(config.normalize_origin(""), "")
        self.assertEqual(config.normalize_origin(None), "")
        self.assertEqual(config.normalize_origin("   "), "")

    def test_different_hosts_do_not_collide(self):
        self.assertNotEqual(
            config.normalize_origin("http://10.0.0.100:4188"),
            config.normalize_origin("https://openusage.example.com"),
        )


class TestCredentialScope(unittest.TestCase):
    def test_scope_binds_origin_and_username(self):
        self.assertEqual(
            config.credential_scope("http://10.0.0.100:4188", "admin"),
            "http://10.0.0.100:4188|admin",
        )

    def test_same_username_different_servers_differ(self):
        a = config.credential_scope("http://server-a:4188", "admin")
        b = config.credential_scope("http://server-b:4188", "admin")
        self.assertNotEqual(a, b)

    def test_username_case_normalized(self):
        self.assertEqual(
            config.credential_scope("http://s", "Admin"),
            config.credential_scope("http://s", " admin "),
        )


class TestTokenKeyringMigration(unittest.TestCase):
    """会话 token 迁入凭据管理器：先写入、验证可读，才从 config.json 清除；
    验证失败回滚并保留旧值。跨服务器/用户名作用域隔离不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kqw-config-test-")
        self._old_dir = config.CONFIG_DIR
        config.CONFIG_DIR = Path(self.tmp.name)
        self._old_path = config.CONFIG_PATH
        config.CONFIG_PATH = config.CONFIG_DIR / "config.json"
        self.fake = FakeKeyring()
        self._keyring_patch = patch.object(config, "keyring", self.fake, create=True)
        self._keyring_patch.start()
        self.addCleanup(self._keyring_patch.stop)

    def tearDown(self):
        config.CONFIG_DIR = self._old_dir
        config.CONFIG_PATH = self._old_path
        self.tmp.cleanup()

    def _write(self, payload):
        config.CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _read(self):
        return json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))

    def test_legacy_token_field_migrates_into_keyring(self):
        self._write({
            "server_url": "http://10.0.0.100:4188",
            "username": "admin",
            "token": "tok-old",
        })
        cfg = config.load_config()
        self.assertTrue(config.migrate_tokens_to_keyring(cfg))
        # 凭据管理器里按作用域可读
        self.assertEqual(
            self.fake.store["http://10.0.0.100:4188|admin"], "tok-old"
        )
        # 迁移成功后 save：config.json 不再含 token 字段
        config.save_config(cfg)
        self.assertNotIn("token", self._read())

    def test_v102_buckets_migrate_into_keyring(self):
        self._write({
            "server_url": "http://10.0.0.100:4188",
            "username": "admin",
            "tokens": {"http://10.0.0.100:4188|admin": "tok-v102"},
        })
        cfg = config.load_config()
        self.assertTrue(config.migrate_tokens_to_keyring(cfg))
        self.assertEqual(
            self.fake.store["http://10.0.0.100:4188|admin"], "tok-v102"
        )
        config.save_config(cfg)
        self.assertNotIn("tokens", self._read())

    def test_verify_failure_rolls_back_and_keeps_legacy(self):
        self._write({
            "server_url": "http://10.0.0.100:4188",
            "username": "admin",
            "token": "tok-old",
        })
        cfg = config.load_config()

        # 让写后读校验失败：作用域键进入 fail_read_keys（get_password 返回 None）
        self.fake.fail_read_keys.add("http://10.0.0.100:4188|admin")
        self.assertFalse(config.migrate_tokens_to_keyring(cfg))
        # config.json 里的旧值仍在（cfg 内存与磁盘均未清除）
        self.assertEqual(cfg["token"], "tok-old")
        self.assertEqual(self._read().get("token"), "tok-old")

    def test_same_scope_overwrite_and_clear(self):
        cfg = {"token": ""}
        config.set_stored_token(cfg, "http://a", "u", "tok")
        self.assertEqual(self.fake.store["http://a|u"], "tok")
        self.assertEqual(config.get_stored_token(cfg, "http://a", "u"), "tok")
        config.set_stored_token(cfg, "http://a", "u", "")  # 清空会话
        self.assertNotIn("http://a|u", self.fake.store)
        self.assertEqual(config.get_stored_token(cfg, "http://a", "u"), "")

    def test_scope_isolation_for_tokens(self):
        cfg = {"token": ""}
        config.set_stored_token(cfg, "http://a", "u", "tok-a")
        config.set_stored_token(cfg, "http://b", "u", "tok-b")
        self.assertEqual(config.get_stored_token(cfg, "http://a", "u"), "tok-a")
        self.assertEqual(config.get_stored_token(cfg, "http://b", "u"), "tok-b")

    def test_legacy_fallback_only_for_same_scope(self):
        # config.json 里遗留的 token 只对它所属的 server/username 生效
        self._write({"server_url": "http://10.0.0.100:4188", "username": "admin", "token": "tok-legacy"})
        cfg = config.load_config()
        self.assertEqual(config.get_stored_token(cfg, "http://10.0.0.100:4188", "admin"), "tok-legacy")
        self.assertEqual(config.get_stored_token(cfg, "http://other:4188", "admin"), "")


class TestPasswordScoping(unittest.TestCase):
    """keyring 密码按作用域绑定；旧版仅按用户名的条目不自动迁移。"""

    def setUp(self):
        self.fake = FakeKeyring()
        patcher = patch.object(config, "keyring", self.fake, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_password_bound_to_scope(self):
        config.set_password("http://a:4188", "admin", "pw-a")
        config.set_password("http://b:4188", "admin", "pw-b")
        self.assertEqual(config.get_password("http://a:4188", "admin"), "pw-a")
        self.assertEqual(config.get_password("http://b:4188", "admin"), "pw-b")
        # keyring 内是作用域键，不是裸用户名
        self.assertNotIn("admin", self.fake.store)
        self.assertIn("http://a:4188|admin", self.fake.store)

    def test_new_password_cleans_legacy_username_entry(self):
        self.fake.store["admin"] = "legacy-pw"  # 旧版残留
        config.set_password("http://a:4188", "admin", "new-pw")
        self.assertNotIn("admin", self.fake.store)          # 旧条目被清理
        self.assertEqual(config.get_password("http://a:4188", "admin"), "new-pw")

    def test_legacy_password_not_returned_for_new_scope(self):
        # 旧版条目存在（按用户名），新作用域下读取不到 → 不会自动迁移
        self.fake.store["admin"] = "legacy-pw"
        self.assertIsNone(config.get_password("http://a:4188", "admin"))

    def test_switch_server_requires_reentry(self):
        config.set_password("http://a:4188", "admin", "pw-a")
        # 切到 server-b：无已存密码 → None（要求重新输入），且读不到 a 的密码
        self.assertIsNone(config.get_password("http://b:4188", "admin"))
        self.assertEqual(config.get_password("http://a:4188", "admin"), "pw-a")  # 切回可用


if __name__ == "__main__":
    unittest.main()
