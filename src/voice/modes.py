"""Voice mode names, kept in a module of their own.

bindings.py needs these to name its actions, and both voice backends import
bindings -- so putting them next to anything heavier would be a circular import.
"""

CLASSIC = "classic"
REALTIME = "realtime"
MODES = (CLASSIC, REALTIME)
