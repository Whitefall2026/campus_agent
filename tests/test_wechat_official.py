from __future__ import annotations

import json
import tempfile
import unittest

from app.ai import gateway as ai_gateway
from app.wechat.bridge import (
    CARD_TYPE,
    WeChatBridge,
    official_push_items,
    official_todo_fields,
)


CARD_XML = """<msg><appmsg><appname>人大就业</appname><mmreader><category>
<item><title><![CDATA[秋招报名通知]]></title>
<digest><![CDATA[请于9月10日前提交报名表。]]></digest>
<url><![CDATA[https://example.com/apply]]></url></item>
<item><title><![CDATA[校园风景回顾]]></title>
<digest><![CDATA[一起欣赏秋日校园。]]></digest>
<url><![CDATA[https://example.com/view]]></url></item>
</category></mmreader></appmsg></msg>"""


class TestOfficialAccountPush(unittest.TestCase):
    def test_extracts_multi_article_card(self):
        items = official_push_items({"type": CARD_TYPE, "content": CARD_XML})
        self.assertEqual([x["title"] for x in items], ["秋招报名通知", "校园风景回顾"])
        self.assertEqual(items[0]["url"], "https://example.com/apply")

    def test_classifies_only_actionable_article_as_todo(self):
        items = official_push_items({"type": CARD_TYPE, "content": CARD_XML})
        task = official_todo_fields(items[0], 1788969600)
        news = official_todo_fields(items[1], 1788969600)
        self.assertIsNotNone(task)
        self.assertEqual(task["kind"], "todo")
        self.assertEqual(task["title"], "秋招报名通知")
        self.assertIsNone(news)

    def test_push_enters_pending_once_and_never_todos(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = WeChatBridge(tmp)
            msg = {
                "type": CARD_TYPE,
                "content": CARD_XML,
                "sort_seq": 101,
                "local_id": 9,
                "create_time": 1788969600,
            }
            target = {"username": "gh_jobs", "display": "人大就业", "official": True}
            self.assertEqual(bridge._process_official_push(None, target, msg), "added")
            self.assertEqual(bridge._process_official_push(None, target, msg), "duplicate")
            pending = ai_gateway.get_pending(tmp)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["source"], "wechat_official")
            self.assertEqual(pending[0]["fields"]["kind"], "todo")
            self.assertEqual(pending[0]["official_url"], "https://example.com/apply")
            try:
                with open(bridge.todo_file, "r", encoding="utf-8") as f:
                    todos = json.load(f)
            except FileNotFoundError:
                todos = []
            self.assertEqual(todos, [])

    def test_resolve_targets_auto_includes_official_accounts(self):
        class DB:
            @staticmethod
            def get_nickname(username):
                return {"gh_jobs": "人大就业", "friend": "朋友"}.get(username, username)

            @staticmethod
            def search_contact(name):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = WeChatBridge(tmp)
            bridge._config["watch"] = []
            sessions = [{"username": "friend"}, {"username": "gh_jobs"}]
            targets = bridge._resolve_targets(DB(), sessions, False)
            self.assertEqual([x["username"] for x in targets], ["gh_jobs"])
            self.assertTrue(targets[0]["official"])


if __name__ == "__main__":
    unittest.main()
