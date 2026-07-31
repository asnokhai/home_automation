import socket, numpy as np
from openwakeword.model import Model
import openwakeword

openwakeword.utils.download_models()
oww = Model()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5005))
sock.settimeout(5)

FRAME = 1280                      # 80 ms at 16 kHz
buf = np.zeros(0, dtype=np.int16)

print("Listening... say 'hey jarvis' or 'alexa'")
while True:
    pkt, _ = sock.recvfrom(2048)
    buf = np.concatenate([buf, np.frombuffer(pkt[4:], dtype='<i2')])
    while len(buf) >= FRAME:
        frame, buf = buf[:FRAME], buf[FRAME:]
        for name, score in oww.predict(frame).items():
            if score > 0.5:
                print(f"DETECTED: {name}  ({score:.2f})")