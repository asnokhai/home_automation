"""Voice input, two pathways behind one facade.

`VoiceAssistant` is the only thing main.py builds. It owns the shared mic and
routes it to whichever backend its current mode selects:

    classic.py    wake word -> record -> Whisper -> chat completion -> TTS
    realtime.py   wake word -> a live Realtime API session, many turns
"""

import os as _os
import sys as _sys

# The rest of the project is a flat pile of modules in src/ (bindings, wakeword,
# sound_player). Put src/ on the path so they import no matter which directory
# this was launched from.
_SRC = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from .modes import CLASSIC, MODES, REALTIME
from .assistant import VoiceAssistant
from .mic_stream import MicStream

__all__ = ["VoiceAssistant", "MicStream", "CLASSIC", "REALTIME", "MODES"]
