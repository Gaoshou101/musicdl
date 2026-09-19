"""The bounded, redacted window onto this process's own log records.

The panel's other two stores hold what the portal asked; this one holds what
the service printed, which is the only place a failure with no event of its own
can be explained.
"""

from __future__ import annotations

import logging
import sys

import pytest

from musicdl.admin.logs import LogBuffer


@pytest.fixture
def installed():
    """A window attached for one test, and detached however that test ends."""
    buffer = LogBuffer(capacity=20)
    buffer.install()
    try:
        yield buffer
    finally:
        buffer.uninstall()


def record(message: str = "line", *, level: int = logging.INFO, name: str = "musicdl.worker",
           args: tuple | None = None, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, exc_info)


# -- the ring ------------------------------------------------------------


def test_the_window_keeps_the_newest_records_and_counts_what_rolled_out():
    buffer = LogBuffer(capacity=3)

    for index in range(5):
        buffer.append(record(f"line {index}"))

    page = buffer.page()
    assert [item["message"] for item in page["items"]] == ["line 2", "line 3", "line 4"]
    assert [item["id"] for item in page["items"]] == [3, 4, 5]
    assert page["dropped"] == 2 and page["total"] == 3


def test_ids_are_never_reused_after_a_record_rolls_out():
    buffer = LogBuffer(capacity=2)
    buffer.append(record("first"))

    for index in range(3):
        buffer.append(record(f"later {index}"))

    assert [item["id"] for item in buffer.page()["items"]] == [3, 4]


def test_every_field_a_reader_needs_is_on_the_entry(installed, caplog):
    with caplog.at_level(logging.INFO, logger="musicdl.worker"):
        logging.getLogger("musicdl.worker").info("hywmusic-beta answered 502")

    entry = installed.page()["items"][-1]
    assert entry["logger"] == "musicdl.worker"
    assert entry["level"] == "info" and isinstance(entry["id"], int)
    assert entry["message"] == "hywmusic-beta answered 502"
    assert entry["time"].endswith("Z") and entry["traceback"] is None


def test_the_window_never_sees_records_the_logger_filtered_out(installed, caplog):
    """The window sits at INFO, so debug chatter never reaches it at all."""
    with caplog.at_level(logging.DEBUG, logger="musicdl.worker"):
        logging.getLogger("musicdl.worker").debug("quiet")
        logging.getLogger("musicdl.worker").info("ordinary")

    assert [item["message"] for item in installed.page()["items"]] == ["ordinary"]


# -- reading -------------------------------------------------------------


def test_the_level_filter_hides_everything_below_it():
    buffer = LogBuffer()
    for level, message in ((logging.DEBUG, "quiet"), (logging.INFO, "ordinary"),
                           (logging.WARNING, "worrying"), (logging.ERROR, "broken")):
        buffer.append(record(message, level=level))

    assert [item["message"] for item in buffer.page(level="warning")["items"]] == ["worrying", "broken"]
    assert [item["message"] for item in buffer.page(level="error")["items"]] == ["broken"]
    assert buffer.page(level="warning")["total"] == 2


def test_the_cursor_returns_only_what_arrived_after_it():
    buffer = LogBuffer()
    for index in range(4):
        buffer.append(record(f"line {index}"))

    head = buffer.page(limit=2)
    assert [item["message"] for item in head["items"]] == ["line 2", "line 3"]
    assert head["last_id"] == 4

    buffer.append(record("line 4"))
    tail = buffer.page(limit=2, after=head["last_id"])
    assert [item["message"] for item in tail["items"]] == ["line 4"] and tail["last_id"] == 5

    # A poll that finds nothing must not move the cursor, or it would skip the
    # records that arrive between this reply and the next one.
    empty = buffer.page(after=tail["last_id"])
    assert empty["items"] == [] and empty["last_id"] == tail["last_id"]


@pytest.mark.parametrize("kwargs, message", [
    ({"limit": 0}, "invalid limit"),
    ({"limit": 501}, "invalid limit"),
    ({"limit": True}, "invalid limit"),
    ({"after": -1}, "invalid pagination"),
    ({"after": True}, "invalid pagination"),
    ({"level": "verbose"}, "invalid level"),
])
def test_the_window_refuses_parameters_it_cannot_answer(kwargs, message):
    with pytest.raises(ValueError, match=message):
        LogBuffer().page(**kwargs)


@pytest.mark.parametrize("capacity", [0, -1, True, 100_001])
def test_an_unusable_capacity_is_refused(capacity):
    with pytest.raises(ValueError, match="invalid capacity"):
        LogBuffer(capacity=capacity)


# -- what must never leave the process -----------------------------------


def test_url_query_strings_and_credentials_never_reach_the_window():
    buffer = LogBuffer()
    buffer.append(record("GET https://api.example.com/music/url?key=SECRET123&sign=SECRET456 done"))
    buffer.append(record("Authorization: Bearer TOPSECRET123"))
    buffer.append(record("password=hunter2 token=tok_live_9 secret=s3cr3t"))

    rendered = repr(buffer.page()["items"])
    for secret in ("SECRET123", "SECRET456", "TOPSECRET123", "hunter2", "tok_live_9", "s3cr3t"):
        assert secret not in rendered
    assert "https://api.example.com/music/url" in rendered


def test_arguments_and_tracebacks_are_redacted_too():
    buffer = LogBuffer()
    buffer.append(record("fetching %s", args=("https://api.example.com/url?key=SECRET123",)))
    try:
        raise RuntimeError("token=SECRET456 rejected upstream")
    except RuntimeError:
        buffer.append(record("download failed", level=logging.ERROR, exc_info=sys.exc_info()))

    items = buffer.page()["items"]
    assert "SECRET123" not in items[0]["message"]
    assert items[1]["traceback"] is not None
    assert "SECRET456" not in items[1]["traceback"]
    assert "RuntimeError" in items[1]["traceback"]


def test_a_record_that_cannot_be_formatted_still_reaches_the_window():
    """A bad call site must not take the logging call down with it."""
    buffer = LogBuffer()
    buffer.emit(record("no placeholder here", args=("extra",)))

    assert [item["message"] for item in buffer.page()["items"]] == ["no placeholder here"]


# -- attaching to the process -------------------------------------------


def test_installing_twice_attaches_once_and_uninstalling_restores_every_logger():
    buffer = LogBuffer()
    root, error, access = logging.getLogger(), logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")
    before = (list(root.handlers), error.propagate, access.propagate)

    buffer.install()
    buffer.install()

    assert root.handlers.count(buffer) == 1
    assert error.handlers.count(buffer) == 1 and access.handlers.count(buffer) == 1
    # Uvicorn configures these with ``propagate = False``; attaching the window
    # must not become the reason a line arrives twice.
    assert error.propagate is False

    buffer.uninstall()
    assert buffer not in root.handlers and buffer not in error.handlers and buffer not in access.handlers
    assert root.handlers == before[0]
    assert (error.propagate, access.propagate) == (before[1], before[2])
