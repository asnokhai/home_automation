#!/bin/bash
# reconnect_phone.sh — re-establish wireless ADB after a phone reboot
PHONE_IP="192.168.68.100"
PORT=5555

echo "Trying wireless connect to $PHONE_IP:$PORT..."
adb connect "$PHONE_IP:$PORT" >/dev/null 2>&1
sleep 1
if adb devices | grep -q "$PHONE_IP:$PORT.*device$"; then
    echo "Connected wirelessly."
    exit 0
fi

echo "Wireless failed (phone likely rebooted). Plug it in via USB now."
for i in $(seq 1 30); do
    if adb devices | grep -vE "$PHONE_IP" | grep -q "device$"; then
        break
    fi
    if adb devices | grep -q "unauthorized"; then
        echo "Phone shows 'unauthorized' — tap Allow on the screen."
    fi
    sleep 1
done

if ! adb devices | grep -vE "$PHONE_IP" | grep -q "device$"; then
    echo "No authorized USB device found. Check cable / USB debugging prompt."
    exit 1
fi

echo "USB device found. Switching to TCP/IP mode..."
adb tcpip "$PORT"
sleep 2
echo "Connecting wirelessly..."
adb connect "$PHONE_IP:$PORT"
sleep 1

if adb devices | grep -q "$PHONE_IP:$PORT.*device$"; then
    echo "Connected. You can unplug USB now."
else
    echo "Still failed. Check the phone's IP ($PHONE_IP)."
    exit 1
fi