"""
Sound player with click feedback and pre-generated speech.
Generates missing speech files automatically on first run.
"""

import io
import os
import pygame
from gtts import gTTS
from pydub import AudioSegment

SPEECH_DIR = "./resources/speech"

PHRASES = {
    "kitchen_on":    "Kitchen on",
    "kitchen_off":   "Kitchen off",
    "bathroom_on":   "Bathroom on",
    "bathroom_off":  "Bathroom off",
    "living_on":     "Living room on",
    "living_off":    "Living room off",
    "vibe_on":       "Vibe on",
    "vibe_off":      "Vibe off",
    "all_on":        "All lights on",
    "all_off":       "All lights off",
    "mode_night":    "Night mode",
    "mode_day":      "Day mode",
    "controller_mode_bluetooth": "Bluetooth Mode",
    "controller_mode_lights": "Lights Mode",
    "play_song": "Playing Song"
}


class SoundPlayer:
    def __init__(self):
        pygame.mixer.init()
        self._click = pygame.mixer.Sound("./resources/button-click-padded.wav")
        self._speech = {}
        self._generate_missing()
        self._load_speech()

    def _generate_missing(self):
        os.makedirs(SPEECH_DIR, exist_ok=True)

        for key, text in PHRASES.items():
            path = os.path.join(SPEECH_DIR, f"{key}.wav")
            if os.path.exists(path):
                continue

            print(f"  Generating speech: {key}")
            mp3_buf = io.BytesIO()
            gTTS(text).write_to_fp(mp3_buf)
            mp3_buf.seek(0)
            AudioSegment.from_mp3(mp3_buf).export(path, format="wav")

    def _load_speech(self):
        for key in PHRASES:
            path = os.path.join(SPEECH_DIR, f"{key}.wav")
            if os.path.exists(path):
                self._speech[key] = pygame.mixer.Sound(path)

        print(f"  Loaded {len(self._speech)} speech sounds")

    def play(self):
        """Play the button click sound."""
        self._click.play()

    def say(self, key):
        """Play a pre-generated speech clip by key, e.g. 'kitchen_on'."""
        sound = self._speech.get(key)
        if sound:
            sound.play()
        else:
            print(f"  ⚠ No speech sound for '{key}'")