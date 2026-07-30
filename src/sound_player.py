"""
Sound player with click feedback and pre-generated speech.
Generates missing speech files automatically on first run.
"""

import io
import os
import pygame
from gtts import gTTS
from pydub import AudioSegment
from pydub.generators import WhiteNoise
import os

SPEECH_DIR = "./resources/speech"

# Bluetooth speakers idle their amp between streams and clip the first
# fraction of a second when audio resumes. A short burst of very quiet noise
# in front of each clip wakes the speaker before the words start. Real noise
# rather than digital silence: many speakers gate a silent stream anyway.
WAKE_MS = 250
WAKE_DBFS = -50

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
    "play_song": "Playing Song",
    "set_alarm": "Setting alarm",
    "disconnect_from_speaker": "Disconnecting from speaker failed", # This runs after the cmd, so if you hear it, it failed
    "controller_mode_bluetooth": "Bluetooth Mode",
    "controller_mode_lights": "Lights Mode",
    "controller_mode_phone": "Phone Mode",
}


def _wake_padding(reference):
    """Near-silent noise matching the reference clip's audio parameters."""
    noise = WhiteNoise().to_audio_segment(duration=WAKE_MS, volume=WAKE_DBFS)
    return (noise
            .set_frame_rate(reference.frame_rate)
            .set_channels(reference.channels)
            .set_sample_width(reference.sample_width))


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
            speech = AudioSegment.from_mp3(mp3_buf)
            (_wake_padding(speech) + speech).export(path, format="wav")

    def _load_speech(self):
        for key in PHRASES:
            path = os.path.join(SPEECH_DIR, f"{key}.wav")
            if os.path.exists(path):
                self._speech[key] = pygame.mixer.Sound(path)

        print(f"  Loaded {len(self._speech)} speech sounds")
        print(os.path.abspath(SPEECH_DIR), os.listdir(SPEECH_DIR))

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