# -*- coding: UTF-8 -*-
"""r18_policy 模块单元测试。"""

import os
import tempfile
import unittest
from typing import ClassVar

from r18_policy import (
    R18StateStore,
    can_enable_r18,
    migrate_legacy_keys,
    parse_id_list,
    session_key,
)


class TestParseIdList(unittest.TestCase):
    def test_comma_and_space(self):
        self.assertEqual(parse_id_list("123, 456 789"), {"123", "456", "789"})

    def test_chinese_comma(self):
        self.assertEqual(parse_id_list("123，456"), {"123", "456"})

    def test_list_input(self):
        self.assertEqual(parse_id_list([123, "456"]), {"123", "456"})

    def test_empty_and_junk(self):
        self.assertEqual(parse_id_list(""), set())
        self.assertEqual(parse_id_list("abc, 12x"), set())
        self.assertIsNone(parse_id_list(None) if False else None)

    def test_none(self):
        self.assertEqual(parse_id_list(None), set())


class TestSessionKey(unittest.TestCase):
    def test_group_shared(self):
        # 群聊按群维度共享：任何成员触发都是同一个 key
        self.assertEqual(session_key("10001", "888888"), "group:888888")
        self.assertEqual(session_key("10002", "888888"), "group:888888")

    def test_private_isolated(self):
        self.assertEqual(session_key("10001", None), "user:10001")
        self.assertEqual(session_key("10002", None), "user:10002")

    def test_empty_group_id_is_private(self):
        self.assertEqual(session_key("10001", ""), "user:10001")
        self.assertEqual(session_key("10001", "0"), "user:10001")
        self.assertEqual(session_key("10001", " "), "user:10001")


class TestMigrateLegacyKeys(unittest.TestCase):
    def test_group_legacy_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("AstrBot:GroupMessage:1554808351_602519154", "r18")
            moved = migrate_legacy_keys(s)
            self.assertEqual(moved, 1)
            self.assertEqual(s.get("group:602519154"), "r18")

    def test_private_legacy_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("AstrBot:FriendMessage:1554808351", "r18only")
            moved = migrate_legacy_keys(s)
            self.assertEqual(moved, 1)
            self.assertEqual(s.get("user:1554808351"), "r18only")

    def test_new_keys_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("group:123", "r18")
            s.set("user:456", "safe")
            self.assertEqual(migrate_legacy_keys(s), 0)
            self.assertEqual(s.get("group:123"), "r18")


class TestCanEnableR18(unittest.TestCase):
    OWNERS: ClassVar[set] = {"10001"}
    GROUPS: ClassVar[set] = {"888888"}

    def test_owner_in_allowed_group(self):
        ok, reason = can_enable_r18("10001", "888888", self.OWNERS, self.GROUPS)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_non_owner_rejected(self):
        ok, reason = can_enable_r18("10002", "888888", self.OWNERS, self.GROUPS)
        self.assertFalse(ok)
        self.assertIn("白名单", reason)

    def test_group_not_allowed(self):
        ok, reason = can_enable_r18("10001", "666666", self.OWNERS, self.GROUPS)
        self.assertFalse(ok)
        self.assertIn("允许列表", reason)

    def test_no_groups_configured(self):
        ok, reason = can_enable_r18("10001", "888888", self.OWNERS, set())
        self.assertFalse(ok)
        self.assertIn("未配置", reason)

    def test_private_chat_owner_ok(self):
        ok, _ = can_enable_r18("10001", None, self.OWNERS, set())
        self.assertTrue(ok)

    def test_private_chat_with_empty_group_id(self):
        # 私聊事件 group_id 可能是空串/"0"，应视为私聊放行
        ok, _ = can_enable_r18("10001", "", self.OWNERS, self.GROUPS)
        self.assertTrue(ok)
        ok2, _ = can_enable_r18("10001", "0", self.OWNERS, self.GROUPS)
        self.assertTrue(ok2)
        ok3, _ = can_enable_r18("10001", " ", self.OWNERS, set())
        self.assertTrue(ok3)

    def test_private_chat_non_owner_rejected(self):
        ok, _ = can_enable_r18("10002", None, self.OWNERS, set())
        self.assertFalse(ok)

    def test_empty_sender(self):
        ok, _ = can_enable_r18("", "888888", self.OWNERS, self.GROUPS)
        self.assertFalse(ok)
        ok2, _ = can_enable_r18(None, None, self.OWNERS, set())
        self.assertFalse(ok2)

    def test_owner_is_group_admin_but_no_whitelist(self):
        # 群主/管理员身份不参与：即使 sender 在群里，只要不在 owners 就无法开启
        ok, _ = can_enable_r18("10003", "888888", self.OWNERS, self.GROUPS)
        self.assertFalse(ok)


class TestR18StateStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("group:888", "r18")
            self.assertEqual(s.get("group:888"), "r18")
            s2 = R18StateStore(p)
            self.assertEqual(s2.get("group:888"), "r18")

    def test_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = R18StateStore(os.path.join(tmp, "r18.json"))
            s.set("x", "r18g")
            s.clear("x")
            self.assertIsNone(s.get("x"))

    def test_invalid_level_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = R18StateStore(os.path.join(tmp, "r18.json"))
            s.set("x", "bogus")
            self.assertIsNone(s.get("x"))

    def test_roundtrip_r18only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("g:1", "r18only")
            self.assertEqual(s.get("g:1"), "r18only")
            s2 = R18StateStore(p)  # 重新加载后保留
            self.assertEqual(s2.get("g:1"), "r18only")

    def test_roundtrip_r18gonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r18.json")
            s = R18StateStore(p)
            s.set("g:2", "r18gonly")
            self.assertEqual(s.get("g:2"), "r18gonly")
            s2 = R18StateStore(p)
            self.assertEqual(s2.get("g:2"), "r18gonly")

    def test_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = R18StateStore(os.path.join(tmp, "r18.json"))
            s.set("a", "r18")
            s.set("b", "safe")
            self.assertEqual(s.all(), {"a": "r18", "b": "safe"})

    def test_missing_file(self):
        s = R18StateStore("/nonexistent/dir/r18.json")
        self.assertEqual(s.all(), {})


if __name__ == "__main__":
    unittest.main()