import serial, numpy as np, wave

print('Running...')

ser = serial.Serial('/dev/ttyUSB0', 230400, timeout=1)

def read_chunk():
    while True:
        if ser.read(1) == b'\xAA' and ser.read(1) == b'\x55':
            break
    return np.frombuffer(ser.read(512), dtype='<i2')

buf = [read_chunk() for _ in range(120)]   # ~2 seconds
audio = np.concatenate(buf)

print("samples:", len(audio), "min:", audio.min(), "max:", audio.max())

with wave.open('test.wav', 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(audio.tobytes())