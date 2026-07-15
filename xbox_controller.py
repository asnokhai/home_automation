import pygame
import sys
from config import BUTTON_MAPPING

class XboxController:
    def __init__(self):
        pygame.init()
        pygame.joystick.init()

        if pygame.joystick.get_count() == 0:
            print("No controller found! Connect your Xbox controller and retry.")
            sys.exit(1)

        self._joy = pygame.joystick.Joystick(0)
        self._joy.init()
        print(f"Connected: {self._joy.get_name()}")

        self._button_callbacks = {}

    def on_button(self, button_name, callback):
        """Register a callback for a button press.
        Names: a, b, x, y, lb, rb, start, back
        """

        button_id = BUTTON_MAPPING.get(button_name.lower())
        if button_id is None:
            raise ValueError(f"Unknown button: {button_name}")
        self._button_callbacks[button_id] = callback

    def update(self):
        """Poll events and fire any registered callbacks."""
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                cb = self._button_callbacks.get(event.button)
                if cb:
                    cb()

    def close(self):
        pygame.quit()