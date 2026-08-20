"""Single-wakeword setup for openWakeWord.

Model() with no arguments loads every pretrained model (alexa, hey mycroft,
hey rhasspy, timers, ...), so any of them can fire the assistant.  These
helpers download and load only 'hey jarvis'.
"""
import os
import openwakeword
from openwakeword.model import Model

WAKEWORD = "hey jarvis"
THRESHOLD = 0.5


def _norm(key):
    """'hey_jarvis_v0.1' / a model path -> 'hey jarvis v0.1'."""
    return os.path.splitext(os.path.basename(str(key)))[0].lower().replace("_", " ")


def is_wakeword(key, name=WAKEWORD):
    return _norm(key).startswith(name)


def _entry():
    """(MODELS key, model path) for the wakeword, whatever this version calls it."""
    for key, meta in getattr(openwakeword, "MODELS", {}).items():
        if is_wakeword(key):
            return key, (meta or {}).get("model_path", "")
    return WAKEWORD, ""


def load_model():
    """Download + load only the 'hey jarvis' model.  Returns (model, keys)."""
    key, _ = _entry()
    try:
        openwakeword.utils.download_models(model_names=[key])
    except (TypeError, ValueError, KeyError):
        openwakeword.utils.download_models()      # older API: no per-model select

    key, path = _entry()
    oww = Model(wakeword_models=[path if path and os.path.exists(path) else key])

    loaded = list(getattr(oww, "models", {}))
    keys = [k for k in loaded if is_wakeword(k)]
    if not keys:
        if len(loaded) != 1:
            raise RuntimeError(f"'{WAKEWORD}' model not loaded; got {loaded}")
        keys = loaded                              # single model, odd naming
    return oww, keys


def score(predictions, keys):
    """Highest score among the wakeword models only."""
    return max((predictions[k] for k in keys if k in predictions), default=0.0)
