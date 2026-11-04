from threading import Event
import unittest
from unittest.mock import Mock, patch

from src.activity import activity_result, telegram_activity
from src.image_search import render_matches


class ActivityTests(unittest.TestCase):
    def test_activity_replaces_working_with_success_without_clearing(self):
        ready = Event()
        calls = []
        def send(method, payload):
            calls.append((method, payload))
            if method == "sendChatAction":
                ready.set()
        with telegram_activity(send, 101, 55):
            self.assertTrue(ready.wait(1))
        self.assertEqual(calls[0], ("setMessageReaction", {"chat_id": 101, "message_id": 55, "reaction": [{"type": "emoji", "emoji": "👀"}], "is_big": False}))
        self.assertEqual(calls[-1], ("setMessageReaction", {"chat_id": 101, "message_id": 55, "reaction": [{"type": "emoji", "emoji": "👌"}], "is_big": False}))

    def test_activity_refreshes_before_telegram_status_expires(self):
        event = Mock()
        event.is_set.side_effect = [False, False, True]
        send = Mock()
        with patch("src.activity.Event", return_value=event):
            with telegram_activity(send, 101, action="upload_photo"):
                pass
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all(call.args == ("sendChatAction", {"chat_id": 101, "action": "upload_photo"}) for call in send.call_args_list))
        self.assertEqual([call.args for call in event.wait.call_args_list], [(4,), (4,)])

    def test_feedback_failure_does_not_hide_task_exception_and_marks_error(self):
        event = Mock()
        event.is_set.side_effect = [False, True]
        send = Mock(side_effect=OSError("unavailable"))
        with patch("src.activity.Event", return_value=event):
            with self.assertRaisesRegex(ValueError, "task failed"):
                with telegram_activity(send, 101, 55):
                    raise ValueError("task failed")
        self.assertEqual(send.call_args.args[1]["reaction"], [{"type": "emoji", "emoji": "🤬"}])

    def test_only_typing_refreshes_while_working_reaction_stays_in_place(self):
        event = Mock()
        event.is_set.side_effect = [False, False, False, True]
        send = Mock()
        with patch("src.activity.Event", return_value=event):
            with telegram_activity(send, 101, 55):
                pass
        reactions = [c.args[1]["reaction"] for c in send.call_args_list if c.args[0] == "setMessageReaction"]
        self.assertEqual(reactions, [[{"type": "emoji", "emoji": "👀"}], [{"type": "emoji", "emoji": "👌"}]])
        self.assertEqual(sum(c.args[0] == "sendChatAction" for c in send.call_args_list), 3)

    def test_handled_error_and_queued_work_have_distinct_results(self):
        for result, expected in [("🤬", ["👀", "🤬"]), (None, ["👀"])]:
            with self.subTest(result=result):
                send = Mock()
                with telegram_activity(send, 101, 55):
                    activity_result(result)
                self.assertEqual([c.args[1]["reaction"][0]["emoji"] for c in send.call_args_list
                                  if c.args[0] == "setMessageReaction"], expected)
        # A failed/queued operation must not affect the next message.
        send = Mock()
        with telegram_activity(send, 101, 56):
            pass
        self.assertEqual(send.call_args.args[1]["reaction"][0]["emoji"], "👌")

    def test_slow_typing_request_cannot_clear_or_replace_the_final_reaction(self):
        started, release, finished = Event(), Event(), Event()
        calls = []
        def send(method, payload):
            calls.append((method, payload))
            if method == "sendChatAction":
                started.set()
                release.wait(3)
                finished.set()
        try:
            with telegram_activity(send, 101, 55):
                self.assertTrue(started.wait(1))
            release.set()
            self.assertTrue(finished.wait(1))
        finally:
            release.set()
        self.assertEqual([p["reaction"][0]["emoji"] for m, p in calls if m == "setMessageReaction"], ["👀", "👌"])

    def test_search_summary_hides_descriptions_and_internal_references(self):
        text = render_matches([{"description": "Very long debug details", "chat_id": -452019584, "message_id": 123}])
        self.assertIn("Matching images: 1", text)
        self.assertNotIn("debug", text)
        self.assertNotIn("452019584", text)
