import asyncio, json, socket, subprocess, wave
import traceback
from datetime import datetime
import numpy as np
from openai import OpenAI
import wakeword
from bindings import build_voice_tools
from dotenv import load_dotenv
load_dotenv()

class VoiceAssistant:
    RATE, FRAME = 16000, 1280          # 80 ms
    SILENCE_RMS, SILENCE_SEC, MAX_SEC = 500, 1.5, 15
    MAX_ACTIONS = 8                    # per utterance, guards against a runaway fan-out

    def __init__(self, actions, sound, port=5005):
        self.actions = actions
        self.sound = sound
        self.client = OpenAI()
        self.oww, self.wake_keys = wakeword.load_model()   # 'hey jarvis' only

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', port))
        self.buf = np.zeros(0, dtype=np.int16)
        self.busy = False

        self.tools = build_voice_tools(actions)

    # --- audio ---------------------------------------------------------
    def _next_frame(self):
        while len(self.buf) < self.FRAME:
            pkt, _ = self.sock.recvfrom(2048)
            self.buf = np.concatenate([self.buf, np.frombuffer(pkt[4:], dtype='<i2')])
        f, self.buf = self.buf[:self.FRAME], self.buf[self.FRAME:]
        return f

    def _record_until_silence(self):
        frames, quiet = [], 0.0
        while True:
            f = self._next_frame()
            frames.append(f)
            rms = np.sqrt(np.mean(f.astype(np.float32) ** 2))
            quiet = quiet + 0.08 if rms < self.SILENCE_RMS else 0.0
            if quiet >= self.SILENCE_SEC or len(frames) * 0.08 > self.MAX_SEC:
                return np.concatenate(frames)

    def _save(self, audio, path="in.wav"):
        with wave.open(path, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.RATE)
            w.writeframes(audio.tobytes())
        return path

    def _speak(self, text):
        r = self.client.audio.speech.create(
            model="tts-1", voice="alloy", input=text, response_format="wav")
        with open("out.wav", "wb") as fh:
            fh.write(r.content)
        subprocess.run(["aplay", "-q", "out.wav"])

    # --- one exchange, all blocking; runs in a thread -------------------
    def _listen_and_think(self):
        audio = self._record_until_silence()
        with open(self._save(audio), "rb") as fh:
            text = self.client.audio.transcriptions.create(
                model="whisper-1", file=fh).text.strip()
        if not text:
            return [], None
        print(f"  You: {text}")

        msg = self.client.chat.completions.create(
            model="gpt-4o-mini",
            tools=self.tools,
            parallel_tool_calls=True,
            messages=[
                {"role": "system", "content":
                 "You control a smart home. Call a function for each action the user "
                 "asks for -- call several in one turn when they ask for several "
                 "things. Only call functions that exist. Otherwise answer in one or "
                 "two short sentences.\n"
                 # Dated per request, not at startup: this process stays up for
                 # days, so a baked-in date would quietly drift and every
                 # "tomorrow" would land on the wrong day.
                 f"Today is {datetime.now().strftime('%A %d %B %Y')}. Use it to turn "
                 "any deadline the user speaks into an ISO 8601 date."},
                {"role": "user", "content": text},
            ]).choices[0].message

        calls = [(c.function.name, json.loads(c.function.arguments or "{}"))
                 for c in (msg.tool_calls or [])]
        if len(calls) > self.MAX_ACTIONS:
            dropped = [name for name, _ in calls[self.MAX_ACTIONS:]]
            print(f"  ⚠ Too many actions, skipping: {', '.join(dropped)}")
            calls = calls[:self.MAX_ACTIONS]
        return calls, msg.content

    # --- main loop -----------------------------------------------------
    async def run(self):
        loop = asyncio.get_event_loop()
        print(f"  Voice: say '{wakeword.WAKEWORD}'")
        while True:
            frame = await loop.run_in_executor(None, self._next_frame)
            if self.busy:
                continue
            score = wakeword.score(self.oww.predict(frame), self.wake_keys)
            if score < wakeword.THRESHOLD:
                continue

            self.sound.play_voice_assistant_activated()
            self.busy = True
            self.oww.reset()
            print(f"  Wake word detected ({score:.2f})")

            try:
                calls, reply = await loop.run_in_executor(
                    None, self._listen_and_think
                )

                self.sound.play_voice_assistant_deactivated()

                # Sequential on purpose: the wrappers are not reentrant, and
                # "everything off, then the kitchen on" only works in order.
                for name, args in calls:
                    action = self.actions.get(name)
                    if action is None:
                        print(f"  ⚠ Unknown action: {name}")
                        continue
                    print(f"  → {name}{args or ''}")
                    await self.handler(action, args)

                if reply:
                    print(f"  Bot: {reply}")
                    await loop.run_in_executor(None, self._speak, reply)
            except Exception as e:
                print(f"  ⚠ Voice error: {e}")
                traceback.print_exc()
            finally:
                self.buf = np.zeros(0, dtype=np.int16)   # drop self-heard audio
                self.busy = False

    def set_action_handler(self, handler):
        self.handler = handler