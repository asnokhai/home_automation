import pygame

class SoundPlayer:
    def __init__(self):
        pygame.mixer.init()
        self._sound = pygame.mixer.Sound('button-click.wav')

    def play(self):
        self._sound.play()
