import socket, numpy as np, wave, subprocess, os, sys, time
from openai import OpenAI
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import wakeword
from dotenv import load_dotenv
load_dotenv()

client = OpenAI()                      # reads OPENAI_API_KEY
oww, wake_keys = wakeword.load_model()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5005))

RATE, FRAME = 16000, 1280              # 80 ms
SILENCE_RMS = 500                      # tune this
SILENCE_SEC = 1.5
MAX_SEC = 15

buf = np.zeros(0, dtype=np.int16)

def next_frame():
    global buf
    while len(buf) < FRAME:
        pkt, _ = sock.recvfrom(2048)
        buf = np.concatenate([buf, np.frombuffer(pkt[4:], dtype='<i2')])
    f, buf = buf[:FRAME], buf[FRAME:]
    return f

def rms(f):
    return np.sqrt(np.mean(f.astype(np.float32) ** 2))

def record_until_silence():
    frames, quiet = [], 0.0
    while True:
        f = next_frame()
        frames.append(f)
        quiet = quiet + 0.08 if rms(f) < SILENCE_RMS else 0.0
        if quiet >= SILENCE_SEC or len(frames) * 0.08 > MAX_SEC:
            return np.concatenate(frames)

def save_wav(audio, path):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(audio.tobytes())

print(f"Ready. Say '{wakeword.WAKEWORD}'.")
while True:
    f = next_frame()
    print(rms(f))
    if wakeword.score(oww.predict(f), wake_keys) < wakeword.THRESHOLD:
        continue

    print("Wake word! Listening...")
    oww.reset()
    audio = record_until_silence()
    save_wav(audio, "in.wav")

    with open("in.wav", "rb") as fh:
        text = client.audio.transcriptions.create(
            model="whisper-1", file=fh).text
    print("You:", text)
    if not text.strip():
        continue

    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a home assistant. Answer in one or two short sentences."},
            {"role": "user", "content": text}
        ]).choices[0].message.content
    print("Bot:", reply)

    speech = client.audio.speech.create(
        model="tts-1", voice="alloy", input=reply, response_format="wav")
    with open("out.wav", "wb") as fh:
        fh.write(speech.content)

    subprocess.run(["aplay", "-q", "out.wav"])
    buf = np.zeros(0, dtype=np.int16)     # drop audio captured during playback
    print("\nReady.")