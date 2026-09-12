from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    CANCEL = "cancel"
    NEXT = "next"
    PREVIOUS = "previous"
    SELECT = "select"
    SEARCH = "search"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ParsedCommand:
    kind: CommandKind
    value: str | int | None = None


def parse_command(text: str, *, max_length: int = 512) -> ParsedCommand:
    if not isinstance(text, str):
        raise ValueError("command must be text")
    value = text.strip()
    if not value or len(value) > max_length:
        raise ValueError("command is empty or too long")
    if value in {"/c", "/cancel"}:
        return ParsedCommand(CommandKind.CANCEL)
    if value == "n":
        return ParsedCommand(CommandKind.NEXT)
    if value == "p":
        return ParsedCommand(CommandKind.PREVIOUS)
    if value.isdecimal():
        index = int(value)
        return ParsedCommand(CommandKind.SELECT, index) if 1 <= index <= 100 else ParsedCommand(CommandKind.SEARCH, value)
    if value == "/search":
        raise ValueError("search query is empty")
    if value.startswith("/search "):
        query = value[8:].strip()
        if not query:
            raise ValueError("search query is empty")
        return ParsedCommand(CommandKind.SEARCH, query)
    if value.startswith("/"):
        return ParsedCommand(CommandKind.UNSUPPORTED)
    return ParsedCommand(CommandKind.SEARCH, value)
