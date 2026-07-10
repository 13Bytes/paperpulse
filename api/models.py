from typing import TypedDict


class Paper(TypedDict):
    """Paper metadata passed through the Paperpulse pipeline."""

    title: str
    authors: list[str]
    summary: str
    url: str
