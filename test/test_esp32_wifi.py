import socket, numpy as np, wave

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5005))
sock.settimeout(5)

expected, buf, lost = None, [], 0
while len(buf) < 60:                      # ~2 seconds
    pkt, _ = sock.recvfrom(2048)
    seq = int.from_bytes(pkt[:4], 'little')
    if expected is not None and seq != expected:
        lost += seq - expected
    expected = seq + 1
    buf.append(np.frombuffer(pkt[4:], dtype='<i2'))

audio = np.concatenate(buf)
print("samples:", len(audio), "lost packets:", lost)

with wave.open('wifi_test.wav', 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(audio.tobytes())