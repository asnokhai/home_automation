"""
Simple Xbox controller wrapper using pygame.
Handles disconnects, reconnects, and async polling.
"""

import asyncio
import time
import pygame

RECONNECT_INTERVAL = 2
POLL_INTERVAL = 0.05


class XboxController:
    BUTTON_MAPPING = {
        "a": 0, "b": 1, "x": 3, "y": 4,
        "lb": 6, "rb": 7, "back": 10, "start": 11,
        "L": 13, "R": 14
    }

    # The D-pad is reported as a hat rather than as buttons, so these names
    # are resolved separately from BUTTON_MAPPING.
    DPAD_NAMES = ("up", "down", "left", "right")

    # Analog sticks are reported as axes. Each stick maps to its (x, y) axis
    # indices.
    STICK_AXES = {
        "LJ": (0, 1),
        "RJ": (2, 3),
    }

    # Names a stick can produce, e.g. "LJ-up". Bind these like any button.
    STICK_NAMES = tuple(
        f"{stick}-{direction}"
        for stick in STICK_AXES
        for direction in ("up", "down", "left", "right")
    )

    # Triggers are analog too, on one axis each. Bind them by these names.
    TRIGGER_AXES = {
        "LT": 5,
        "RT": 4,
    }
    TRIGGER_NAMES = tuple(TRIGGER_AXES)

    # Whether the triggers rest at -1.0 and read +1.0 fully pressed (the usual
    # Linux joystick behaviour) rather than resting at 0.0. Set False if --raw
    # shows a released trigger sitting at zero.
    TRIGGER_SIGNED = True

    # Hysteresis: an axis has to be pushed past PRESS to count as held, and
    # must fall back below RELEASE before it can retrigger. This stops an axis
    # resting near the threshold from spamming actions.
    AXIS_PRESS_THRESHOLD = 0.6
    AXIS_RELEASE_THRESHOLD = 0.4
    TRIGGER_PRESS_THRESHOLD = 0.6
    TRIGGER_RELEASE_THRESHOLD = 0.4

    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self._joy = None
        self._button_actions = {}   # button_id (or d-pad/stick name) → action tuple
        self._on_action = None      # async callback
        self._connected = False
        self._waiting = False
        self._last_reconnect = 0
        self._hat_state = (0, 0)    # last known D-pad position
        self._axis_values = {}      # axis index → last known value
        self._stick_state = {stick: set() for stick in self.STICK_AXES}
        self._trigger_state = {name: False for name in self.TRIGGER_AXES}
        self._connect()

        self._controller_modes_list = None
        self._active_mode = None
        self._active_mode_index = 0

        self.BUTTON_NAMES = {v: k for k, v in self.BUTTON_MAPPING.items()}

    def cycle_mode(self):
        self._active_mode_index = (self._active_mode_index + 1) % len(self._controller_modes_list)
        self._active_mode = self._controller_modes_list[self._active_mode_index]

        return self._active_mode

    def set_button_maps(self, maps):
        self._button_maps = maps
        self._controller_modes_list = list(maps.keys())
        self._active_mode_index = 0
        self._active_mode = self._controller_modes_list[0]

    def _resolve_button(self, button_name):
        mode_map = self._button_maps.get(self._active_mode, {})
        return mode_map.get(button_name)

    @staticmethod
    def _hat_directions(hat):
        """Convert a hat (x, y) tuple into the set of directions held down."""
        x, y = hat
        directions = set()
        if y > 0:
            directions.add("up")
        elif y < 0:
            directions.add("down")
        if x < 0:
            directions.add("left")
        elif x > 0:
            directions.add("right")
        return directions

    @classmethod
    def _stick_directions(cls, stick, x, y, previous):
        """Convert a stick's (x, y) position into the set of directions held.

        `previous` is the set of directions currently considered held for this
        stick, which selects the release threshold instead of the press one.
        Note the y axis is negative when the stick is pushed up.
        """
        directions = set()
        candidates = (
            (f"{stick}-left", -x),
            (f"{stick}-right", x),
            (f"{stick}-up", -y),
            (f"{stick}-down", y),
        )
        for name, magnitude in candidates:
            threshold = (cls.AXIS_RELEASE_THRESHOLD if name in previous
                         else cls.AXIS_PRESS_THRESHOLD)
            if magnitude >= threshold:
                directions.add(name)
        return directions

    def _reset_inputs(self):
        """Drop remembered stick/trigger/d-pad state, e.g. on (dis)connect."""
        self._hat_state = (0, 0)
        self._axis_values = {}
        self._stick_state = {stick: set() for stick in self.STICK_AXES}
        self._trigger_state = {name: False for name in self.TRIGGER_AXES}

    def _trigger_for_axis(self, axis):
        for name, trigger_axis in self.TRIGGER_AXES.items():
            if axis == trigger_axis:
                return name
        return None

    def _stick_for_axis(self, axis):
        for stick, axes in self.STICK_AXES.items():
            if axis in axes:
                return stick
        return None

    def _connect(self):
        pygame.joystick.quit()
        pygame.joystick.init()

        if pygame.joystick.get_count() > 0:
            self._joy = pygame.joystick.Joystick(0)
            self._joy.init()
            self._connected = True
            self._waiting = False
            self._reset_inputs()
            print(f"Controller connected: {self._joy.get_name()}")
        else:
            if not self._waiting:
                if self._connected:
                    print("Controller disconnected. Waiting for reconnect...")
                else:
                    print("No controller found. Will connect when available.")
                self._waiting = True
            self._connected = False
            self._joy = None
            self._reset_inputs()

    @property
    def connected(self):
        return self._connected

    def map_button(self, button_name, action):
        """Bind a button to an action tuple, e.g. ("toggle", "Kitchen").

        Accepts face/shoulder button names, the D-pad directions "up", "down",
        "left" and "right", the stick directions "LJ-up", "LJ-down", "LJ-left",
        "LJ-right" and their "RJ-" equivalents, and the triggers "LT" and "RT".
        """
        name = button_name.lower()

        for analog_name in self.STICK_NAMES + self.TRIGGER_NAMES:
            if name == analog_name.lower():
                self._button_actions[analog_name] = action
                return

        if name in self.DPAD_NAMES:
            self._button_actions[name] = action
            return

        button_id = self.BUTTON_MAPPING.get(name)
        if button_id is None:
            raise ValueError(f"Unknown button: {button_name}")
        self._button_actions[button_id] = action

    def set_action_handler(self, handler):
        """Set the async callback that receives action tuples."""
        self._on_action = handler

    @classmethod
    def _trigger_pressed(cls, value, previously_held):
        """Decide whether a trigger axis value counts as held down."""
        # Normalise to 0.0 (released) .. 1.0 (fully pulled).
        if cls.TRIGGER_SIGNED:
            value = (value + 1.0) / 2.0
        threshold = (cls.TRIGGER_RELEASE_THRESHOLD if previously_held
                     else cls.TRIGGER_PRESS_THRESHOLD)
        return value >= threshold

    async def _handle_axis_motion(self, axis, value):
        """Update stick/trigger state for one axis and fire new presses."""
        trigger = self._trigger_for_axis(axis)
        if trigger is not None:
            await self._handle_trigger_motion(trigger, value)
            return

        stick = self._stick_for_axis(axis)
        if stick is None:
            return  # unused axis

        self._axis_values[axis] = value
        x_axis, y_axis = self.STICK_AXES[stick]
        x = self._axis_values.get(x_axis, 0.0)
        y = self._axis_values.get(y_axis, 0.0)

        previous = self._stick_state[stick]
        current = self._stick_directions(stick, x, y, previous)
        self._stick_state[stick] = current

        # Same edge-triggered behaviour as the D-pad: a push fires once, and
        # the stick must recentre before that direction can fire again.
        for name in sorted(current - previous):
            action = self._resolve_button(name)
            if action:
                await self._on_action(action)

    async def _handle_trigger_motion(self, trigger, value):
        """Treat a trigger axis as a button, firing once per pull."""
        self._axis_values[self.TRIGGER_AXES[trigger]] = value

        previously_held = self._trigger_state[trigger]
        held = self._trigger_pressed(value, previously_held)
        self._trigger_state[trigger] = held

        # Fire only on the transition into "held", so a trigger kept down
        # doesn't repeat and a partial release doesn't retrigger.
        if held and not previously_held:
            action = self._resolve_button(trigger)
            if action:
                await self._on_action(action)

    async def run(self):
        """Poll forever, calling the action handler on button presses."""
        while True:
            if not self._connected:
                now = time.time()
                if now - self._last_reconnect >= RECONNECT_INTERVAL:
                    self._last_reconnect = now
                    self._connect()
                await asyncio.sleep(POLL_INTERVAL)
                continue

            try:
                for event in pygame.event.get():
                    if event.type == pygame.JOYBUTTONDOWN:
                        button_name = self.BUTTON_NAMES.get(event.button)
                        action = self._resolve_button(button_name)
                        if action:
                            await self._on_action(action)
                    elif event.type == pygame.JOYHATMOTION:
                        previous = self._hat_directions(self._hat_state)
                        current = self._hat_directions(event.value)
                        self._hat_state = event.value
                        # Fire only on newly pressed directions, so holding a
                        # diagonal doesn't retrigger the direction already held.
                        for name in sorted(current - previous):
                            action = self._resolve_button(name)
                            if action:
                                await self._on_action(action)
                    elif event.type == pygame.JOYAXISMOTION:
                        await self._handle_axis_motion(event.axis, event.value)
                    elif event.type == pygame.JOYDEVICEREMOVED:
                        self._connected = False
                        self._joy = None
                        self._reset_inputs()
                        print("Controller disconnected. Waiting for reconnect...")
            except pygame.error:
                self._connected = False
                self._joy = None
                self._reset_inputs()
                print("Controller lost. Waiting for reconnect...")

            await asyncio.sleep(POLL_INTERVAL)

    def close(self):
        pygame.quit()