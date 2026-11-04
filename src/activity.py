"""Best-effort Telegram activity feedback around slow operations."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
from urllib.error import HTTPError, URLError


_current = ContextVar("telegram_activity", default=None)


def activity_result(reaction):
    """Record a handled error, or None while a queued submission is still processing."""
    current = _current.get()
    if current is not None:
        current["result"] = reaction


@contextmanager
def telegram_activity(send, chat_id, message_id=None, action="typing"):
    current = {"result": "👌"}
    token = _current.set(current)
    stopped = Event()

    def notify(method, payload):
        try:
            send(method, payload)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            pass

    def react(emoji):
        if message_id is not None and emoji is not None:
            notify("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                         "reaction": [{"type": "emoji", "emoji": emoji}], "is_big": False})

    def refresh():
        while not stopped.is_set():
            notify("sendChatAction", {"chat_id": chat_id, "action": action})
            stopped.wait(4)

    react("👀")
    thread = Thread(target=refresh, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        activity_result("🤬")
        raise
    finally:
        stopped.set()
        thread.join(timeout=1)
        react(current["result"])
        _current.reset(token)
