"""Publicador MQTT: envía temperatura, humedad e iluminancia cada PUBLISH_INTERVAL s.

Para forzar valores (probar la alerta) se publica en CONTROL_TOPIC un JSON, p. ej.
{"temperature": 35}. Con {} se vuelve a los valores normales.
"""
import json
import math
import os
import random
import time

import paho.mqtt.client as mqtt

HOST = os.environ.get("MQTT_HOST", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC = os.environ.get("MQTT_TOPIC", "unab/sala1/telemetry")
CONTROL = os.environ.get("CONTROL_TOPIC", "unab/sala1/control")
INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "10"))

forced = {}


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"conectado a {HOST}:{PORT} ({reason_code})", flush=True)
    client.subscribe(CONTROL)


def on_message(client, userdata, msg):
    global forced
    try:
        data = json.loads(msg.payload)
        forced = {k: float(v) for k, v in data.items()
                  if k in ("temperature", "humidity", "illuminance")}
        print(f"valores forzados: {forced}", flush=True)
    except (ValueError, TypeError, AttributeError) as exc:
        print(f"control ignorado: {exc}", flush=True)


def sample():
    """Valores simulados con ciclo diario (hora local de la VM) y ruido."""
    t = time.localtime()
    hour = t.tm_hour + t.tm_min / 60
    daily = math.sin((hour - 9) / 24 * 2 * math.pi)  # pico ~15:00
    temperature = 24 + 4 * daily + random.gauss(0, 0.3)
    humidity = 55 - 10 * daily + random.gauss(0, 1.0)
    sun = max(0.0, math.sin((hour - 6) / 12 * math.pi))  # 6:00 a 18:00
    illuminance = 20 + 480 * sun + random.gauss(0, 5)
    values = {
        "temperature": round(temperature, 2),
        "humidity": round(min(max(humidity, 0), 100), 2),
        "illuminance": round(max(illuminance, 0), 1),
    }
    values.update(forced)
    return values


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(HOST, PORT, keepalive=60)
            break
        except OSError as exc:
            print(f"broker no disponible ({exc}), reintento en 5 s", flush=True)
            time.sleep(5)
    client.loop_start()
    while True:
        payload = json.dumps(sample())
        client.publish(TOPIC, payload, qos=1)
        print(payload, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
