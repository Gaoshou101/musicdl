"""Deterministic primary plugin for the in-process download vertical slice.

``search``/``resolve``/``health`` are the only operations the main process may call. The
compatibility ``download`` operation is declared by the protocol but never executed: the main
process resolves a descriptor and streams the media itself, so a runner call would be a defect.
"""


def handle(request):
    operation = request["operation"]
    if operation == "search":
        return [{"source_id": "primary", "source_version": "1", "item_id": "primary-1",
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
