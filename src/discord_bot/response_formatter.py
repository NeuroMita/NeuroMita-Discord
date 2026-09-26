from __future__ import annotations


DISCORD_MESSAGE_LIMIT = 2000


def split_response(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    remaining = str(text or "")
    if not remaining:
        return ["(empty response)"]

    chunks: list[str] = []
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    if remaining:
        chunks.append(remaining)
    return chunks
