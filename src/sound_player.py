"""
Sound player with click feedback and pre-generated speech.
Generates missing speech files automatically on first run.
"""

import io
import os
import queue
import threading
import time
import pygame
from gtts import gTTS
from pydub import AudioSegment
from pydub.generators import Sine, WhiteNoise
import os
import hashlib

SPEECH_DIR = "./resources/speech"
ALARM_PATH = "./resources/timer_done.wav"

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
    "controller_mode_misc": "Miscellaneous Mode",
    "timer_stopped": "Timer stopped",
    "voice_mode_classic": "Classic voice mode",
    "voice_mode_realtime": "Realtime voice mode",
    "rebooting": "Rebooting",
}

class SoundPlayer:
    def __init__(self):
        pygame.mixer.init()
        self._click_sound = pygame.mixer.Sound("./resources/button-click.wav")
        self._voice_assistant_activate_sound = pygame.mixer.Sound("./resources/voice_assistant_activated.wav")
        self._voice_assistant_deactivate_sound = pygame.mixer.Sound("./resources/voice_assistant_deactivated.wav")

        self._generate_alarm()
        self._alarm_sound = pygame.mixer.Sound(ALARM_PATH)
        # Quiet enough that the spoken timer name stays intelligible over it.
        self._alarm_sound.set_volume(0.6)
        self._alarm_channel = None

        self._speech = {}
        self._generate_missing()
        self._load_speech()
        self._adhoc = {}  # cache for on-the-fly phrases

        # Speech plays through a queue so clips never overlap: one voice command
        # can now trigger several actions, and each wants its own confirmation.
        self._speech_queue = queue.Queue()
        threading.Thread(target=self._speech_worker, daemon=True).start()

    def _speech_worker(self):
        """Play queued speech clips one after another, forever."""
        while True:
            sound = self._speech_queue.get()
            channel = sound.play()
            while channel and channel.get_busy():
                time.sleep(0.02)

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

    def _generate_alarm(self):
        """Synthesize the timer alarm clip on first run, so no asset is needed.

        Three short beeps and a long gap, because the clip is played on repeat
        while a timer rings: a solid tone would be unpleasant and impossible to
        talk over.
        """
        if os.path.exists(ALARM_PATH):
            return

        print("  Generating timer alarm")
        beep = Sine(880).to_audio_segment(duration=250).fade_in(10).fade_out(10)
        gap = AudioSegment.silent(duration=150, frame_rate=beep.frame_rate)
        tail = AudioSegment.silent(duration=1000, frame_rate=beep.frame_rate)
        clip = beep + gap + beep + gap + beep + tail
        os.makedirs(os.path.dirname(ALARM_PATH), exist_ok=True)
        clip.export(ALARM_PATH, format="wav")

    def _load_speech(self):
        for key in PHRASES:
            path = os.path.join(SPEECH_DIR, f"{key}.wav")
            if os.path.exists(path):
                self._speech[key] = pygame.mixer.Sound(path)

        print(f"  Loaded {len(self._speech)} speech sounds")
        print(os.path.abspath(SPEECH_DIR), os.listdir(SPEECH_DIR))

    def play_click(self):
        """Play the button click sound_player."""
        self._click_sound.play()

    def play_voice_assistant_activated(self):
        """Play the activate voice assistant sound_player."""
        self._voice_assistant_activate_sound.play()

    def play_voice_assistant_deactivated(self):
        """Play the deactivate voice assistant sound_player."""
        self._voice_assistant_deactivate_sound.play()

    def play_timer_alarm(self):
        """Ring the timer alarm on repeat until stop_timer_alarm() is called.

        Deliberately not routed through the speech queue: that worker waits for
        each clip to finish, and a looping clip never does. Any alarm already
        ringing is stopped first, so a second timer firing cannot leave an
        orphaned loop with nothing holding its channel.
        """
        self.stop_timer_alarm()
        self._alarm_channel = self._alarm_sound.play(loops=-1)

    def stop_timer_alarm(self):
        """Silence the timer alarm. Returns True if it was actually ringing."""
        if self._alarm_channel is None:
            return False

        self._alarm_channel.stop()
        self._alarm_channel = None
        return True

    def say(self, key):
        """Play a pre-generated speech clip by key, e.g. 'kitchen_on'."""
        sound = self._speech.get(key)
        if sound:
            self._speech_queue.put(sound)
        else:
            print(f"  ⚠ No speech sound_player for '{key}'")
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

        self._speech_queue.put(sound)