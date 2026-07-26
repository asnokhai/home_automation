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

    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self._joy = None
        self._button_actions = {}   # button_id (or d-pad name) → action tuple
        self._on_action = None      # async callback
        self._connected = False
        self._waiting = False
        self._last_reconnect = 0
        self._hat_state = (0, 0)    # last known D-pad position
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

    def _connect(self):
        pygame.joystick.quit()
        pygame.joystick.init()

        if pygame.joystick.get_count() > 0:
            self._joy = pygame.joystick.Joystick(0)
            self._joy.init()
            self._connected = True
            self._waiting = False
            self._hat_state = (0, 0)
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
            self._hat_state = (0, 0)

    @property
    def connected(self):
        return self._connected

    def map_button(self, button_name, action):
        """Bind a button to an action tuple, e.g. ("toggle", "Kitchen").

        Accepts face/shoulder button names as well as the D-pad directions
        "up", "down", "left" and "right".
        """
        name = button_name.lower()
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
                    elif event.type == pygame.JOYDEVICEREMOVED:
                        self._connected = False
                        self._joy = None
                        self._hat_state = (0, 0)
                        print("Controller disconnected. Waiting for reconnect...")
            except pygame.error:
                self._connected = False
                self._joy = None
                self._hat_state = (0, 0)
                print("Controller lost. Waiting for reconnect...")

            await asyncio.sleep(POLL_INTERVAL)

    def close(self):
        pygame.quit()