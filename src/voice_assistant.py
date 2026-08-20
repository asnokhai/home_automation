import asyncio, json, socket, subprocess, wave
import traceback
import numpy as np
from openai import OpenAI
import wakeword
from bindings import build_voice_tools
from dotenv import load_dotenv
load_dotenv()

class VoiceAssistant:
    RATE, FRAME = 16000, 1280          # 80 ms
    SILENCE_RMS, SILENCE_SEC, MAX_SEC = 500, 1.5, 15

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
            return None, None
        print(f"  You: {text}")

        msg = self.client.chat.completions.create(
            model="gpt-4o-mini",
            tools=self.tools,
            messages=[
                {"role": "system", "content":
                 "You control a smart home. Call a function if the user asks for an "
                 "action. Otherwise answer in one or two short sentences."},
                {"role": "user", "content": text},
            ]).choices[0].message

        if msg.tool_calls:
            return msg.tool_calls[0].function.name, None
        return None, msg.content

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

            self.busy = True
            self.oww.reset()
            print(f"  Wake word detected ({score:.2f})")
            try:
                action_name, reply = await loop.run_in_executor(
                    None, self._listen_and_think)

                if action_name:
                    print(f"  → {action_name}")
                    await self.handler(self.actions[action_name])
                elif reply:
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