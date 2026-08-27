"""Design and play light shows for the Tapo bulbs.

    python -m light_show.editor <name>    design a show in the browser
    python -m light_show.play   <name>    play it against Spotify
"""

import os as _os
import sys as _sys

# The rest of the project is a flat pile of modules in src/ (tapo_controller,
# spotify_player, config). Put src/ on the path so they import no matter which
# directory the editor or player was launched from.
_SRC = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from .show import Show, LIGHTS
from .player import ShowPlayer

__all__ = ["Show", "ShowPlayer", "LIGHTS"]
