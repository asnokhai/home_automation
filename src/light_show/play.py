"""
play.py — run a saved show.

    python -m light_show.play <name>

The show file records which Spotify recording it was designed against, so
playback pins to that URI rather than whatever a name search turns up today.
"""

import asyncio

def _bootstrap():
    """Allow this file to be run directly, not just as `python -m`.

    Executing a file inside a package gives it no parent, so every relative
    import in it fails. Put src/ on the path, import the package properly, and
    hand this module its package back.
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.dirname(here)
    if src not in sys.path:
        sys.path.insert(0, src)
    import light_show                      # noqa: F401  (establishes the parent)
    return "light_show"


if __package__ in (None, ""):
    __package__ = _bootstrap()

from .show import Show
from .player import ShowPlayer


def main(name):
    from spotify_player import SpotifyPlayer

    show = Show.load(name)
    print(show.summary())
    asyncio.run(ShowPlayer(show, SpotifyPlayer()).run())


if __name__ == "__main__":
    main("nightmare")
