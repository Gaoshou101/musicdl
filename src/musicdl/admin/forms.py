"""Reading the portal's own HTML forms without another dependency.

``request.form()`` raises for every form body, including a urlencoded one,
unless ``python-multipart`` is installed -- and it is deliberately not part of
this project's runtime.  The portal's pages post urlencoded fields and nothing
else, so the body is parsed here instead of taking on a dependency to read two
strings.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.requests import Request


URLENCODED = "application/x-www-form-urlencoded"


def is_urlencoded(request: Request) -> bool:
    """Whether the request carries the body shape these pages produce."""
    return URLENCODED in request.headers.get("content-type", "")


async def form_fields(request: Request) -> dict[str, str]:
    """The urlencoded body as a flat mapping; anything else reads as empty.

    A body that is not urlencoded, or is not text, yields no fields at all --
    which a caller checking a token treats as a missing token, not as a match.
    """
    if not is_urlencoded(request):
        return {}
    try:
        parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return {}
    return {key: values[0] for key, values in parsed.items() if values}
