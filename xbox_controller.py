"""
Simple Xbox controller wrapper using pygame.
Handles disconnects, reconnects, and async polling.
"""

import asyncio
import time
import pygame
from config import BUTTON_MAPPING

RECONNECT_INTERVAL = 2
POLL_INTERVAL = 0.05


class XboxController:
    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self._joy = None
        self._button_actions = {}   # button_id → action tuple
        self._on_action = None      # async callback
        self._connected = False
        self._waiting = False
        self._last_reconnect = 0
        self._connect()

    def _connect(self):
        pygame.joystick.quit()
        pygame.joystick.init()

        if pygame.joystick.get_count() > 0:
            self._joy = pygame.joystick.Joystick(0)
            self._joy.init()
            self._connected = True
            self._waiting = False
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

    @property
    def connected(self):
        return self._connected

    def map_button(self, button_name, action):
        """Bind a button to an action tuple, e.g. ("toggle", "Kitchen")."""
        button_id = BUTTON_MAPPING.get(button_name.lower())
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
                        action = self._button_actions.get(event.button)
                        if action and self._on_action:
                            await self._on_action(action)
                    elif event.type == pygame.JOYDEVICEREMOVED:
                        self._connected = False
                        self._joy = None
                        print("Controller disconnected. Waiting for reconnect...")
            except pygame.error:
                self._connected = False
                self._joy = None
                print("Controller lost. Waiting for reconnect...")

            await asyncio.sleep(POLL_INTERVAL)

    def close(self):
        pygame.quit()