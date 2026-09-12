import pytest

from musicdl.wecom.commands import CommandKind, parse_command


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        (" /c ", CommandKind.CANCEL, None),
        ("/cancel", CommandKind.CANCEL, None),
        ("n", CommandKind.NEXT, None),
        ("p", CommandKind.PREVIOUS, None),
        ("7", CommandKind.SELECT, 7),
        ("hello world", CommandKind.SEARCH, "hello world"),
        ("/search 周杰伦", CommandKind.SEARCH, "周杰伦"),
    ],
)
def test_parse_command_normalizes_supported_commands(text, kind, value):
    result = parse_command(text)
    assert result.kind is kind
    assert result.value == value


def test_parse_command_rejects_unknown_slash_command():
    assert parse_command("/wat").kind is CommandKind.UNSUPPORTED


def test_search_command_requires_a_separator():
    assert parse_command("/searchfoo").kind is CommandKind.UNSUPPORTED


@pytest.mark.parametrize("text", ["", "   ", "/search"])
def test_parse_command_rejects_empty_or_invalid_input(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_parse_command_rejects_overlong_search():
    with pytest.raises(ValueError):
        parse_command("x" * 513)


@pytest.mark.parametrize("text", ["0", "101"])
def test_out_of_range_numbers_are_search_text_not_selection(text):
    assert parse_command(text).kind is CommandKind.SEARCH
