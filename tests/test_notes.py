import base64
from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.inbox import Inbox
from test_inbox import update


class NoteTests(unittest.TestCase):
    def setUp(self):
        activity = patch("src.inbox.telegram_activity", side_effect=lambda *args, **kwargs: nullcontext())
        activity.start()
        self.addCleanup(activity.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.graph_id = "22222222-2222-4222-8222-222222222222"
        self.catalog = root / "graphs.json"
        self.catalog.write_text(json.dumps([{"id": self.graph_id, "name": "Other graph", "token": "token", "key": None}]))
        self.box = Inbox(root, "101,202", graphs_file=self.catalog, api_url="http://sync")
        self.addCleanup(self.box.db.close)

    def record(self, identifier):
        return json.loads(self.box.db.execute("SELECT record FROM submissions WHERE id=?", (identifier,)).fetchone()[0])

    def test_explicit_graph_and_selected_shorthand_preserve_multiline_text(self):
        self.box.remember([update(1, text='/note "Other graph" First line\nDon\'t change "quotes"\n\nLast line'),
                           update(2, text='/graph "Other graph"'),
                           update(3, text='/note Today\'s note\nSecond line'),
                           update(4, text=f'/note {self.graph_id} By UUID')])
        self.assertEqual(self.record(1)["original_text"], 'First line\nDon\'t change "quotes"\n\nLast line')
        self.assertEqual(self.record(3)["original_text"], "Today's note\nSecond line")
        self.assertEqual(self.record(4)["original_text"], "By UUID")
        self.assertEqual(self.box.selected_graph(202), "")
        self.assertEqual(self.record(1)["graph_id"], self.graph_id)
        self.assertEqual(self.record(1)["analysis"]["status"], "not_requested")
        self.assertIn("note", {c["command"] for c in self.box.commands(101)})

    def test_empty_notes_and_missing_graph_notify_without_queuing(self):
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram") as telegram:
            self.box.remember([update(1, text='/note'), update(2, text='/note \n \t'),
                               update(3, text='/note Something to save'),
                               update(4, text='/note "Unknown" Secret'),
                               update(5, text='/note "Unclosed graph'),
                               update(6, text='/note "Other graph"  \n')])
        replies = [c.args[1]["text"] for c in telegram.call_args_list if c.args[0] == "sendMessage"]
        self.assertEqual(len(replies), 6)
        self.assertTrue(all("graph is required" in reply for reply in replies[:2]))
        self.assertIn("graph is required", replies[2])
        self.assertIn("not available", replies[3])
        self.assertIn("Note started", replies[5])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT offset FROM state").fetchone()[0], 7)

    def test_single_block_plain_capture_uses_local_journal_and_never_fetches_or_analyzes(self):
        message = update(1, text='/note "Other graph" Text\nhttps://youtu.be/example')
        message["message"]["date"] = int(datetime.fromisoformat("2026-10-28T23:30:00+00:00").timestamp())
        self.box.remember([message, message])
        self.box.ai.enabled = True
        self.box.bot_token = "test-token"
        with patch("src.inbox.request_json", return_value={"page-id": "journal"}) as request, \
                patch.object(self.box, "telegram") as telegram, patch.object(self.box, "describe") as describe, \
                patch.object(self.box.ai, "step") as ai:
            self.box.capture(1)
            self.box.capture(1)
        request.assert_called_once()
        url, payload, headers = request.call_args.args
        self.assertEqual(url, f"http://sync/api/v1/graphs/{self.graph_id}/capture")
        self.assertEqual(payload["journal-day"], 20261029)
        self.assertEqual(len(payload["blocks"]), 1)
        self.assertEqual(payload["blocks"][0]["title"], "Text\nhttps://youtu.be/example")
        self.assertEqual(headers, {"Authorization": "Bearer token"})
        describe.assert_not_called()
        ai.assert_not_called()
        self.assertIn("Note saved", telegram.call_args.args[1]["text"])
        self.assertEqual(self.box.db.execute("SELECT done FROM submissions WHERE id=1").fetchone()[0], 1)

    def test_encrypted_retry_keeps_payload_date_and_destination(self):
        self.box.extra_graphs[0]["key"] = base64.b64encode(b"k" * 32).decode()
        self.box.remember([update(1, text='/note "Other graph" Secret')])
        captures = []
        def request(url, payload=None, headers=None):
            self.assertIn(self.graph_id, url)
            if url.endswith("/encrypt"):
                self.assertEqual(payload["texts"], ["Secret"])
                return {"texts": ["ciphertext"]}
            captures.append(payload)
            if len(captures) == 1:
                raise TimeoutError("Lost reply")
            return {"page-id": "journal"}
        with patch("src.inbox.request_json", side_effect=request) as mocked:
            self.box.capture(1)
            self.box.capture(1)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(captures[0], captures[1])
        self.assertEqual(captures[0]["blocks"][0]["title"], "ciphertext")
        self.assertEqual(self.record(1)["publish_status"], "published")

    def test_note_permissions_and_removed_graph_are_enforced(self):
        self.box.remember([update(1, sender=999, text='/note "Other graph" Secret'),
                           update(2, chat_type="group", text='/note "Other graph" Secret'),
                           update(3, text='/note "Other graph" Saved')])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.box.extra_graphs.clear()
        with patch("src.inbox.request_json") as request:
            self.box.capture(3)
        request.assert_not_called()
        self.assertNotEqual(self.record(3)["publish_status"], "published")

    def test_note_draft_survives_restart_and_preserves_multiline_text_graph_and_date(self):
        message = update(1, text='/note "Other graph"')
        message["message"]["date"] = int(datetime.fromisoformat("2026-10-28T23:30:00+00:00").timestamp())
        self.box.remember([message, update(2, text="First line\nSecond line"),
                           update(3, text="\nThird line\n\nFourth line\n")])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        restored = Inbox(self.box.data, "101,202", graphs_file=self.catalog, api_url="http://sync")
        try:
            restored.db.execute("INSERT INTO user_graphs VALUES (?,?)", (101, "unavailable"))
            restored.db.commit()
            restored.remember([update(4, text="/help"), update(5, text="/done"), update(5, text="/done")])
            self.assertEqual(restored.db.execute("SELECT count(*) FROM note_drafts").fetchone()[0], 0)
        finally:
            restored.db.close()
        record = self.record(5)
        self.assertEqual(record["original_text"], "First line\nSecond line\nThird line\n\nFourth line")
        self.assertEqual(record["graph_id"], self.graph_id)
        self.assertEqual(record["journal_day"], 20261029)
        self.assertEqual(record["kind"], "note")

    def test_empty_draft_invalid_input_and_cancel_do_not_save_notes(self):
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram") as telegram:
            self.box.remember([update(1, text='/graph "Other graph"'), update(2, text='/note'),
                               update(3, text='/done'), update(4, text=' \n'),
                               update(5, text='Caption', attachment={"file_id": "video"}),
                               update(6, text='/cook'), update(7, text='/checklist'),
                               update(8, sender=202, text='/done')])
            replies = [c.args[1]["text"] for c in telegram.call_args_list if c.args[0] == "sendMessage"]
            self.assertTrue(any("note is empty" in reply for reply in replies))
            self.assertEqual(self.box.db.execute("SELECT count(*) FROM note_drafts").fetchone()[0], 1)
            self.box.remember([update(9, text='/cancel')])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM note_drafts").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 0)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM checklist_drafts").fetchone()[0], 0)

    def test_draft_retained_when_graph_access_removed_and_no_competing_drafts(self):
        self.box.remember([update(1, text='/note "Other graph"'), update(2, text='Keep me')])
        self.box.extra_graphs.clear()
        self.box.remember([update(3, text='/done')])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM note_drafts").fetchone()[0], 1)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)
        self.box.extra_graphs = json.loads(self.catalog.read_text())
        self.box.remember([update(4, text='/cancel'), update(5, text='/graph "Other graph"'),
                           update(6, text='/checklist'), update(7, text='/note'),
                           update(8, text='/cancel'), update(9, text='/cook'), update(10, text='/note')])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM note_drafts").fetchone()[0], 0)

    def test_note_leaves_checklist_and_cooking_requests_intact(self):
        self.box.remember([update(1, text='/graph "Other graph"'), update(2, text='/checklist Shopping'),
                           update(3, text='Milk'), update(4, text='/note Separate note'),
                           update(5, text='/done'), update(6, text='/cook'),
                           update(7, text='/note Another note')])
        self.assertEqual(self.record(4)["original_text"], "Separate note")
        self.assertEqual(self.record(5)["items"], ["Milk"])
        self.assertEqual(self.record(7)["kind"], "note")
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 1)

    def test_unavailable_and_ambiguous_graphs_are_rejected(self):
        with patch.object(self.box, "graph_catalog", return_value=[
                {"id": self.graph_id, "name": "Other graph", "enabled": False}]):
            with self.assertRaisesRegex(ValueError, "setup is required"):
                self.box.parse_submission('/note "Other graph" Secret', 101)
        with patch.object(self.box, "graph_catalog", return_value=[
                {"id": self.graph_id, "name": "Same", "enabled": True},
                {"id": "other", "name": "Same", "enabled": True}]):
            with self.assertRaisesRegex(ValueError, "Several graphs"):
                self.box.parse_submission('/note Same Secret', 101)

    def test_training_and_cooking_share_inline_graph_syntax_without_drafts(self):
        self.box.remember([update(1, text='/training "Other graph" https://youtu.be/workout'),
                           update(2, text='/cook "Other graph" https://youtu.be/recipe'),
                           update(3, text='/graph "Other graph"'),
                           update(4, text='/training https://youtu.be/selected'),
                           update(5, text='/cook Recipe text\nDon\'t discard line breaks'),
                           update(6, text=f'/training {self.graph_id} Squats: 3 sets')])
        for identifier in (1, 2, 4, 5, 6):
            record = self.record(identifier)
            self.assertEqual(record["graph_id"], self.graph_id)
            self.assertEqual(record["publish_status"], "pending")
            self.assertFalse(record["original_text"].startswith("/"))
        self.assertEqual(self.record(2)["analysis"]["type"], "cooking")
        self.assertNotIn("type", self.record(1)["analysis"])
        self.assertEqual(self.record(1)["source_urls"], ["https://youtu.be/workout"])
        self.assertEqual(self.record(5)["original_text"], "Recipe text\nDon't discard line breaks")
        self.assertEqual(self.record(6)["original_text"], "Squats: 3 sets")
        for table in ("note_drafts", "checklist_drafts", "cooking_requests"):
            self.assertEqual(self.box.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertIn("training", {c["command"] for c in self.box.commands(101)})

    def test_link_commands_reject_missing_graph_empty_content_and_unknown_graph(self):
        self.box.bot_token = "test-token"
        with patch.object(self.box, "telegram") as telegram:
            self.box.remember([update(1, text='/training https://youtu.be/workout'),
                               update(2, text='/cook https://youtu.be/recipe'),
                               update(3, text='/training'),
                               update(4, text='/training "Other graph"'),
                               update(5, text='/cook "Other graph"'),
                               update(6, text='/training "Unknown" https://youtu.be/workout'),
                               update(7, text='/cook "Unknown" https://youtu.be/recipe')])
        replies = [c.args[1]["text"] for c in telegram.call_args_list if c.args[0] == "sendMessage"]
        self.assertIn("/training shorthand", replies[0])
        self.assertIn("/cook shorthand", replies[1])
        self.assertTrue(all("empty" in text for text in replies[2:5]))
        self.assertTrue(all("not available" in text for text in replies[5:]))
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_training_and_cooking_video_captions_support_graph_override(self):
        for identifier, command in enumerate(("/training", "/cook"), 1):
            message = update(identifier, text='', attachment={"file_id": "video", "mime_type": "video/mp4"})
            message["message"].pop("text")
            message["message"]["caption"] = f'{command} "Other graph"'
            self.box.remember([message])
            self.assertEqual(self.record(identifier)["graph_id"], self.graph_id)
            self.assertEqual(self.record(identifier)["original_text"], "")
            self.assertEqual(self.record(identifier)["attachment"]["file_id"], "video")

    def test_training_does_not_consume_cooking_request_and_overrides_do_not_select_graph(self):
        self.box.remember([update(1, text='/cook'),
                           update(2, text='/training "Other graph" https://youtu.be/workout')])
        self.assertEqual(self.box.selected_graph(101), "")
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM cooking_requests").fetchone()[0], 1)
        self.assertNotIn("type", self.record(2)["analysis"])


if __name__ == "__main__":
    unittest.main()
