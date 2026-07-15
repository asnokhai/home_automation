"""
Simple Xbox controller wrapper using pygame.
Handles disconnects and reconnects automatically.
"""

import time
import pygame
from config import BUTTON_MAPPING

RECONNECT_INTERVAL = 2


class XboxController:
    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self._joy = None
        self._button_callbacks = {}
        self._connected = False
        self._waiting = False
        self._last_reconnect = 0
        self._connect()

    def _connect(self):
        """Try to find and connect to a joystick."""
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

    def on_button(self, button_name, callback):
        """Register a callback for a button press."""
        button_id = BUTTON_MAPPING.get(button_name.lower())
        if button_id is None:
            raise ValueError(f"Unknown button: {button_name}")
        self._button_callbacks[button_id] = callback

    def update(self):
        """Poll events and fire callbacks. Handles reconnect automatically."""
        if not self._connected:
            now = time.time()
            if now - self._last_reconnect >= RECONNECT_INTERVAL:
                self._last_reconnect = now
                self._connect()
            return

        try:
            for event in pygame.event.get():
                if event.type == pygame.JOYBUTTONDOWN:
                    cb = self._button_callbacks.get(event.button)
                    if cb:
                        cb()
                elif event.type == pygame.JOYDEVICEREMOVED:
                    self._connected = False
                    self._joy = None
                    print("Controller disconnected. Waiting for reconnect...")
        except pygame.error:
            self._connected = False
            self._joy = None
            print("Controller lost. Waiting for reconnect...")

    def close(self):
        pygame.quit()