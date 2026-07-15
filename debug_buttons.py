import pygame

pygame.init()
pygame.joystick.init()
joy = pygame.joystick.Joystick(0)
joy.init()
print(f"Connected: {joy.get_name()}")
print("Press buttons... (Ctrl+C to quit)\n")

while True:
    for event in pygame.event.get():
        if event.type == pygame.JOYBUTTONDOWN:
            print(f"Button {event.button} pressed")
        elif event.type == pygame.JOYBUTTONUP:
            print(f"Button {event.button} released")
        elif event.type == pygame.JOYAXISMOTION:
            if abs(event.value) > 0.2:
                print(f"Axis {event.axis} = {event.value:.2f}")