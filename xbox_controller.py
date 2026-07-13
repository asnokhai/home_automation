import pygame
import sys


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
        mapping = {
            "a": 0, "b": 1, "x": 2, "y": 3,
            "lb": 4, "rb": 5, "back": 6, "start": 7,
        }
        button_id = mapping.get(button_name.lower())
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