import os, sys, socket, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import wakeword

oww, wake_keys = wakeword.load_model()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5005))
sock.settimeout(5)

FRAME = 1280                      # 80 ms at 16 kHz
buf = np.zeros(0, dtype=np.int16)

print(f"Listening... say '{wakeword.WAKEWORD}'")
while True:
    pkt, _ = sock.recvfrom(2048)
    buf = np.concatenate([buf, np.frombuffer(pkt[4:], dtype='<i2')])
    while len(buf) >= FRAME:
        frame, buf = buf[:FRAME], buf[FRAME:]
        score = wakeword.score(oww.predict(frame), wake_keys)
        if score > wakeword.THRESHOLD:
            print(f"DETECTED: {wakeword.WAKEWORD}  ({score:.2f})")