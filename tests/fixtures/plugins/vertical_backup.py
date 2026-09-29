"""Deterministic backup plugin for the in-process download vertical slice.

It serves the same recording as ``vertical_primary`` from a different source id. Its lossless
capability is undeclared, so it can appear as a replacement and is attempted after a qualifying
content failure when no artifact has yet been reserved.
"""


def handle(request):
    operation = request["operation"]
    if operation == "search":
        return [{"source_id": "backup", "source_version": "1", "item_id": "backup-1",
                 "title": "Song", "artist": "Artist", "format": "mp3"}]
    if operation == "resolve":
        candidate = request["payload"]["candidate"]
        return {"candidate_id": candidate["item_id"], "url": "https://media.example/song.mp3",
                "extension": "mp3", "media_type": "audio/mpeg", "declared_size": 13}
    if operation == "health":
        return True
    if operation == "download":
        raise RuntimeError("compatibility operation must not be called")
    raise ValueError("unsupported operation")
