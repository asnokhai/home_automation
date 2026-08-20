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
import hashlib

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
    "play_song": "Playing Song",
    "set_alarm": "Setting alarm",
    "disconnect_from_speaker": "Disconnecting from speaker failed", # This runs after the cmd, so if you hear it, it failed
    "controller_mode_bluetooth": "Bluetooth Mode",
    "controller_mode_lights": "Lights Mode",
    "controller_mode_phone": "Phone Mode",
}

class SoundPlayer:
    def __init__(self):
        pygame.mixer.init()
        self._click = pygame.mixer.Sound("./resources/button-click.wav")
        self._speech = {}
        self._generate_missing()
        self._load_speech()
        self._adhoc = {}  # cache for on-the-fly phrases

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
            speech.export(path, format="wav")

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
            self.say_text(key)

    def say_text(self, text, cache_to_disk=False):
        """Speak an arbitrary string, generating TTS on demand.

        Requires network access (gTTS). Results are cached in memory for the
        process lifetime; pass cache_to_disk=True to persist to SPEECH_DIR.
        """
        sound = self._adhoc.get(text)

        if sound is None:
            path = None
            if cache_to_disk:
                digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
                path = os.path.join(SPEECH_DIR, f"adhoc_{digest}.wav")

            if path and os.path.exists(path):
                sound = pygame.mixer.Sound(path)
            else:
                try:
                    mp3_buf = io.BytesIO()
                    gTTS(text).write_to_fp(mp3_buf)
                    mp3_buf.seek(0)
                    speech = AudioSegment.from_mp3(mp3_buf)
                    padded = speech
                except Exception as e:
                    print(f"  ⚠ TTS failed for {text!r}: {e}")
                    return

                if path:
                    os.makedirs(SPEECH_DIR, exist_ok=True)
                    padded.export(path, format="wav")
                    sound = pygame.mixer.Sound(path)
                else:
                    wav_buf = io.BytesIO()
                    padded.export(wav_buf, format="wav")
                    wav_buf.seek(0)
                    sound = pygame.mixer.Sound(wav_buf)

            self._adhoc[text] = sound

        sound.play()