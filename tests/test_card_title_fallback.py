"""卡片标题：模型 headline 超长时改用「有证据支持」方向的中文名，不出半句/英文括号。"""
import unittest

from netops_ai.feishu import card


class TitleTextTests(unittest.TestCase):
    CHECK = {"local_action": {"status": "有证据支持"}, "remote_or_upstream": {"status": "已排除"}}

    def test_短标题原样用(self):
        a = {"hypothesis_checklist": self.CHECK}
        self.assertEqual(card._title_text(a, "A1", "人为 shutdown 导致接口中断", "rc"), "人为 shutdown 导致接口中断")

    def test_超长标题改用有证据支持的方向名(self):
        a = {"hypothesis_checklist": self.CHECK}
        long = "本端接口 Ethernet0/1 被管理性关闭 (Administratively Down) 导致链路中断"
        out = card._title_text(a, "A1", long, "rc")
        self.assertNotIn("Administratively", out)
        self.assertLessEqual(len(out), 24)
        self.assertTrue(out)

    def test_英文旧枚举也认(self):
        a = {"hypothesis_checklist": {"local_action": {"status": "supported"}}}
        self.assertTrue(card._title_text(a, "A1", "x" * 40, "rc"))

    def test_没有支持方向时退回原逻辑(self):
        a = {"hypothesis_checklist": {"local_action": {"status": "暂时无法判断"}}}
        self.assertEqual(card._title_text(a, "A1", "短标题", "rc"), "短标题")
        self.assertEqual(card._title_text(a, "A1", "", "根因整句"), "根因整句")


if __name__ == "__main__":
    unittest.main()
