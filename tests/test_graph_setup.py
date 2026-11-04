import base64
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from src.inbox import Inbox
from test_inbox import update


GRAPH = "33333333-3333-4333-8333-333333333333"


class GraphSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.box = self.open()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.box.db.close())
        self.encrypted = True
        self.shared = True
        self.requests = []
        request = patch("src.inbox.request_json", side_effect=self.request)
        request.start()
        self.addCleanup(request.stop)
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *a, **kw: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)
        self.telegram = patch.object(self.box, "telegram", return_value=True).start()
        self.addCleanup(patch.stopall)

    def open(self):
        return Inbox(self.temp.name, "101,202", bot_token="test", api_url="http://sync", admin_id="101",
                     logseq_users='{"101":"owner@example.com","202":"member@example.com"}', allowlist_token="private")

    def request(self, url, payload=None, headers=None, method=None):
        self.requests.append((url, method))
        if "/training/graphs?" in url:
            return {"graphs": [{"id": GRAPH, "name": "Shared"}] if self.shared else []}
        self.assertIn("/training/graph-access?", url)
        self.assertIn("email=owner%40example.com", url)
        if method == "POST":
            return {"graph_id": GRAPH, "token": "graph-token", "expires_at": int(time.time() * 1000) + 60000}
        return {"graph_id": GRAPH, "encrypted": self.encrypted,
                "encrypted_private_key": "encrypted-private", "encrypted_aes_key": "encrypted-aes"}

    def select(self):
        self.box.remember([{"update_id": 1, "callback_query": {
            "id": "callback", "from": {"id": 101}, "data": f"graph:101:{GRAPH}",
            "message": {"message_id": 20, "chat": {"id": 101, "type": "private"}}}}])

    def test_button_prompts_then_unlocks_selects_and_persists_without_password(self):
        self.select()
        prompts = [c.args[1]["text"] for c in self.telegram.call_args_list if c.args[0] == "sendMessage"]
        self.assertIn("encryption password", prompts[0])
        self.assertIn("not end-to-end encrypted", prompts[0])
        key = base64.b64encode(b"k" * 32).decode()
        with patch("src.inbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"graph_id": GRAPH, "key": key}))) as run:
            self.box.remember([update(2, text="secret-password")])
            self.assertEqual(json.loads(run.call_args.kwargs["input"])["password"], "secret-password")
            self.assertNotIn("secret-password", str(run.call_args.args))
        self.assertEqual(self.box.selected_graph(101), GRAPH)
        self.assertEqual(self.box.graph_credentials({"graph_id": GRAPH, "submitted_by": 101}),
                         (GRAPH, "graph-token", {"graph_id": GRAPH, "key": key}))
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM graph_unlocks").fetchone()[0], 0)
        self.assertNotIn("secret-password", " ".join(self.box.db.iterdump()))
        self.assertTrue(any(c.args == ("deleteMessage", {"chat_id": 101, "message_id": 2}) for c in self.telegram.call_args_list))
        self.box.db.close()
        self.box = self.open()
        self.assertTrue(self.box.find_graph(GRAPH, 101)["enabled"])
        self.assertFalse(self.box.find_graph(GRAPH, 202)["enabled"])
        self.assertEqual((Path(self.temp.name) / "inbox.sqlite").stat().st_mode & 0o777, 0o600)

    def test_unencrypted_graph_connects_without_prompt_or_crypto(self):
        self.encrypted = False
        with patch("src.inbox.subprocess.run") as run:
            self.select()
        run.assert_not_called()
        self.assertEqual(self.box.selected_graph(101), GRAPH)
        self.assertTrue(self.box.find_graph(GRAPH, 101)["enabled"])
        self.assertIsNone(self.box.graph_credentials({"graph_id": GRAPH, "submitted_by": 101})[2]["key"])

    def test_wrong_password_is_deleted_and_never_saved_or_used_to_create_token(self):
        self.select()
        with patch("src.inbox.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "secret-password")):
            self.box.remember([update(2, text="secret-password")])
        self.assertFalse(any(method == "POST" for _, method in self.requests))
        self.assertNotIn("secret-password", " ".join(self.box.db.iterdump()))
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertIn("Check your Logseq encryption password", self.telegram.call_args.args[1]["text"])

    def test_pending_unlock_survives_restart_expires_and_never_consumes_other_users(self):
        self.select()
        self.box.db.commit()
        self.box.db.close()
        self.box = self.open()
        with patch.object(self.box, "telegram", return_value=True), patch("src.inbox.subprocess.run") as run:
            self.box.remember([update(2, sender=202, text="/help"), update(3, text="/help"), update(4, text="/note do not save this")])
            self.assertEqual(self.box.db.execute("SELECT count(*) FROM graph_unlocks").fetchone()[0], 1)
            self.box.db.execute("UPDATE graph_unlocks SET requested_at=0")
            self.box.remember([update(5, text="secret-password")])
        run.assert_not_called()
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM graph_unlocks").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_revoked_access_or_failed_deletion_never_saves_password(self):
        self.select()
        self.shared = False
        with patch("src.inbox.subprocess.run") as run:
            self.box.remember([update(2, text="secret-password")])
        run.assert_not_called()
        self.assertIn("no longer shared", self.telegram.call_args.args[1]["text"])
        self.telegram.side_effect = lambda method, payload: (_ for _ in ()).throw(OSError()) if method == "deleteMessage" else True
        self.box.remember([update(3, text="another-password")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertNotIn("password", " ".join(self.box.db.iterdump()))

    def test_cancel_and_expired_credentials_require_new_setup(self):
        self.select()
        self.box.remember([update(2, text="/cancel")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM graph_unlocks").fetchone()[0], 0)
        self.encrypted = False
        self.box.remember([update(3, text=f"/graph {GRAPH}")])
        self.box.db.execute("UPDATE graph_credentials SET expires_at=0")
        self.assertFalse(self.box.find_graph(GRAPH, 101)["enabled"])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)


class GraphSetupAuthorizationTests(unittest.TestCase):
    def test_worker_query_requires_verified_allowed_membership_even_for_owner(self):
        import re
        import sqlite3

        worker = (Path(__file__).resolve().parents[1] / "deploy/sync_allowlist.cljs").read_text()
        query_source = worker[worker.index('(str "SELECT u.id AS user_id'):].split('email graph-id', 1)[0]
        sql = ''.join(re.findall(r'"([^"]*)"', query_source))
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE users(id TEXT, email TEXT, email_verified INTEGER);
            CREATE TABLE graphs(graph_id TEXT, user_id TEXT, graph_ready_for_use INTEGER, graph_e2ee INTEGER);
            CREATE TABLE graph_members(graph_id TEXT, user_id TEXT);
            CREATE TABLE training_allowed_accounts(email TEXT);
            CREATE TABLE user_rsa_keys(user_id TEXT, encrypted_private_key TEXT);
            CREATE TABLE graph_aes_keys(graph_id TEXT, user_id TEXT, encrypted_aes_key TEXT);
            INSERT INTO users VALUES ('owner', 'owner@example.com', 1), ('member', 'member@example.com', 1),
                ('outsider', 'outsider@example.com', 1), ('unverified', 'unverified@example.com', 0);
            INSERT INTO graphs VALUES ('shared', 'member', 1, 1), ('private', 'outsider', 1, 1),
                ('unfinished', 'owner', 0, 0);
            INSERT INTO graph_members VALUES ('shared', 'owner'), ('shared', 'unverified');
            INSERT INTO training_allowed_accounts VALUES ('member@example.com'), ('unverified@example.com');
        ''')
        for email, graph, expected in [('owner@example.com', 'shared', True),
                                       ('member@example.com', 'shared', True),
                                       ('owner@example.com', 'private', False),
                                       ('outsider@example.com', 'private', False),
                                       ('unverified@example.com', 'shared', False),
                                       ('owner@example.com', 'unfinished', False),
                                       ('unknown@example.com', 'shared', False)]:
            with self.subTest(email=email, graph=graph):
                self.assertEqual(bool(db.execute(sql, (email, graph, 'owner@example.com')).fetchall()), expected)
        db.execute('DELETE FROM training_allowed_accounts')
        self.assertFalse(db.execute(sql, ('member@example.com', 'shared', 'owner@example.com')).fetchall())
