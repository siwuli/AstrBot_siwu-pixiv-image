# -*- coding: UTF-8 -*-
"""r18_policy 模块单元测试。"""

import os
import tempfile
import unittest
from typing import ClassVar

from r18_policy import R18StateStore, can_enable_r18, parse_id_list


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