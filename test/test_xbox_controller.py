"""
Manual test harness for XboxController.

Every button, D-pad direction and stick direction is bound to an action that
just prints itself, so you can verify bindings by pressing things.

    python3 controller_test.py           # normal binding test
    python3 controller_test.py --raw     # also dump raw pygame events

Use --raw to discover the real axis/button indices on this machine; the
numbers printed there are what belong in BUTTON_MAPPING and STICK_AXES.
Press "start" to cycle modes, Ctrl+C to quit.
"""

import asyncio
import sys

import pygame

from xbox_controller.xbox_controller import XboxController


def build_button_map(prefix):
    """Bind every known input name to a ("press", prefix, name) action."""
    names = (
        list(XboxController.BUTTON_MAPPING)
        + list(XboxController.DPAD_NAMES)
        + list(XboxController.STICK_NAMES)
        + list(XboxController.TRIGGER_NAMES)
    )
    return {name: ("press", prefix, name) for name in names}


async def raw_event_dump():
    """Print raw joystick events, ignoring the binding layer entirely."""
    watched = {
        pygame.JOYBUTTONDOWN: "BUTTON DOWN",
        pygame.JOYBUTTONUP: "BUTTON UP",
        pygame.JOYHATMOTION: "HAT",
        pygame.JOYAXISMOTION: "AXIS",
    }
    while True:
        for event in pygame.event.get(list(watched)):
            label = watched[event.type]
            if event.type == pygame.JOYAXISMOTION:
                # Axes emit constant noise near centre; only show real movement.
                if abs(event.value) < 0.5:
                    continue
                print(f"[raw] {label} axis={event.axis} value={event.value:+.2f}")
            elif event.type == pygame.JOYHATMOTION:
                print(f"[raw] {label} value={event.value}")
            else:
                print(f"[raw] {label} button={event.button}")
        await asyncio.sleep(0.02)


async def main():
    raw = "--raw" in sys.argv

    controller = XboxController()
    controller.set_button_maps({
        "mode-one": build_button_map("mode-one"),
        "mode-two": build_button_map("mode-two"),
    })

    async def on_action(action):
        _, mode, name = action
        if name == "start":
            print(f"--> mode is now {controller.cycle_mode()}")
            return
        print(f"[{mode}] {name}")

    controller.set_action_handler(on_action)

    print("Press buttons, the D-pad, or push the sticks. Ctrl+C to quit.")
    print("'start' cycles between mode-one and mode-two.\n")

    tasks = [controller.run()]
    if raw:
        tasks.append(raw_event_dump())

    try:
        await asyncio.gather(*tasks)
    finally:
        controller.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")