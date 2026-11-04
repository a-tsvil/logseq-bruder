import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.inbox import Inbox
from test_inbox import update


GRAPH = "11111111-1111-4111-8111-111111111111"


class ReactionFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = Inbox(temporary.name, "101", bot_token="test", graph_id=GRAPH, graph_name="Shared",
                         api_url="http://sync", logseq_users='{"101":"owner@example.com"}',
                         admin_id="101", allowlist_token="test")
        self.addCleanup(self.box.db.close)
        self.box.telegram = Mock(return_value=True)
        request = patch("src.inbox.request_json", return_value={"graphs": [{"id": GRAPH, "name": "Shared"}]})
        self.request = request.start()
        self.addCleanup(request.stop)

    def reactions(self, message_id=1):
        return [c.args[1]["reaction"][0]["emoji"] for c in self.box.telegram.call_args_list
                if c.args[0] == "setMessageReaction" and c.args[1]["message_id"] == message_id]

    def test_note_keeps_eyes_across_validation_queueing_and_delivery(self):
        self.box.remember([update(text="/note Remember milk")])
        self.assertEqual(self.reactions(), ["👀"])
        record = json.loads(self.box.db.execute("SELECT record FROM submissions").fetchone()[0])
        def publish(note):
            self.assertEqual(self.reactions()[-1], "👀")
            note["publish_status"] = "published"
        with patch.object(self.box, "publish_note", side_effect=publish):
            self.box.capture(record["id"])
        self.assertEqual(self.reactions(), ["👀", "👀", "👌"])
        self.assertFalse(any(c.args[1].get("reaction") == [] for c in self.box.telegram.call_args_list))

    def test_handled_graph_discovery_failure_gets_error_instead_of_success(self):
        self.request.side_effect = OSError("offline")
        self.box.remember([update(text="/select_graph")])
        self.assertEqual(self.reactions(), ["👀", "🤬"])

    def test_validation_error_does_not_mark_the_next_message_as_failed(self):
        self.box.remember([update(1, text="/analyze invalid"), update(2, text="/help")])
        self.assertEqual(self.reactions(1), ["👀", "🤬"])
        self.assertEqual(self.reactions(2), ["👀", "👌"])

    def test_failed_delivery_gets_error_and_leaves_submission_queued(self):
        self.box.remember([update(text="/note Remember milk")])
        with patch.object(self.box, "publish_note", side_effect=OSError("offline")):
            self.box.capture(1)
        self.assertEqual(self.reactions(), ["👀", "👀", "🤬"])
        self.assertEqual(self.box.db.execute("SELECT done FROM submissions").fetchone()[0], 0)

    def test_telegram_deduplicates_eyes_across_queue_and_capture_but_allows_future_changes(self):
        from src.activity import activity_result

        self.box.telegram = Inbox.telegram.__get__(self.box)
        with patch("src.inbox.request_json", return_value={"ok": True, "result": True}) as request:
            with self.box.activity(101, 1):
                activity_result(None)
            with self.box.activity(101, 1):
                pass
            with self.box.activity(101, 1):
                activity_result("🤬")
        self.assertEqual([c.args[1]["reaction"][0]["emoji"] for c in request.call_args_list
                          if c.args[0].endswith("/setMessageReaction")], ["👀", "👌", "👀", "🤬"])

    def test_deferred_second_search_has_no_reaction_until_it_is_processed(self):
        self.box.image_search_enabled = True
        with patch.object(self.box, "search_images", return_value="No matches"):
            self.box.remember([update(1, text="/search cat"), update(2, text="/search dog")])
        self.assertEqual(self.reactions(1), ["👀", "👌"])
        self.assertEqual(self.reactions(2), [])
        self.assertEqual(self.box.db.execute("SELECT offset FROM state").fetchone()[0], 2)
