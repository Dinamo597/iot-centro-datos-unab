"""Flota del Parcial 2: 9 dispositivos que publican por MQTT y aceptan comandos.

Telemetría:  unab/dc/<id>/telemetry   (JSON con las variables y el origen)
Estado:      unab/dc/<id>/state       (retenido; estado reportado del gemelo)
Comandos:    unab/dc/<id>/cmd         (retroceso hacia el dispositivo)

Comandos (JSON):
  {"power": "off"} | {"power": "on"}
  {"interval": 5}
  {"calibrate": {"temperature": -1.5}}     # offset aditivo por variable
  {"force": {"temperature": 35}}           # fuerza un valor ({} lo quita)
  {"reset": true}                          # vuelve a valores por defecto
"""
import csv
import json
import math
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
import psutil
import requests

HOST = os.environ.get("MQTT_HOST", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
BASE = os.environ.get("MQTT_BASE", "unab/dc")
LAT = float(os.environ.get("LAT", "7.1182633"))
LON = float(os.environ.get("LON", "-73.1113583"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HEADERS = {"User-Agent": "unab-iot-lab"}

psutil.cpu_percent(interval=None)  # la primera lectura siempre es 0


def daily(peak=15.0):
    """Ciclo diario en [-1, 1] con máximo a la hora `peak` (hora local)."""
    t = time.localtime()
    hour = t.tm_hour + t.tm_min / 60
    return math.sin((hour - (peak - 6)) / 24 * 2 * math.pi)


# ---------- generadores (Digital Twin) ----------

def make_rack(base_temp, base_power):
    def sample():
        d = daily(15)
        power = base_power + 1.5 * d + random.gauss(0, 0.2)
        temp = base_temp + 2.0 * d + 0.5 * (power - base_power) + random.gauss(0, 0.2)
        hum = 45 - 5 * d + random.gauss(0, 0.8)
        return {"temperature": temp, "humidity": hum, "power_kw": power}
    return sample


def pasillo():
    d = daily(15)
    return {"temperature": 22.5 + 1.5 * d + random.gauss(0, 0.2),
            "humidity": 50 - 4 * d + random.gauss(0, 0.8)}


def make_door():
    st = {"left": 0}

    def sample():
        if st["left"] > 0:
            st["left"] -= 1
            is_open = 1
        elif random.random() < 0.02:
            st["left"] = random.randint(2, 6)
            is_open = 1
        else:
            is_open = 0
        vib = abs(random.gauss(0, 0.03)) + (0.3 * random.random() if is_open else 0)
        return {"door_open": is_open, "vibration": vib}
    return sample


def make_ups():
    st = {"outage": 0, "battery": 100.0}

    def sample():
        if st["outage"] == 0 and random.random() < 0.0001:
            st["outage"] = random.randint(6, 30)
        if st["outage"] > 0:
            st["outage"] -= 1
            st["battery"] = max(0.0, st["battery"] - 0.8)
            vin = 0.0
        else:
            st["battery"] = min(100.0, st["battery"] + 0.3)
            vin = 220 + random.gauss(0, 1.5)
        load = 55 + 10 * daily(15) + random.gauss(0, 1)
        return {"battery_pct": st["battery"], "load_pct": load, "input_voltage": vin}
    return sample


# ---------- dispositivo real: la propia VM de Azure ----------

def host():
    return {"cpu_pct": psutil.cpu_percent(interval=None),
            "mem_pct": psutil.virtual_memory().percent,
            "disk_pct": psutil.disk_usage("/").percent,
            "load1": os.getloadavg()[0]}


# ---------- API: Open-Meteo (clima actual) ----------

def weather():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast", headers=HEADERS, timeout=15,
            params={"latitude": LAT, "longitude": LON,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,surface_pressure"})
        r.raise_for_status()
        c = r.json()["current"]
        return {"temperature": c["temperature_2m"], "humidity": c["relative_humidity_2m"],
                "wind_kmh": c["wind_speed_10m"], "pressure_hpa": c["surface_pressure"]}
    except Exception as exc:  # noqa: BLE001
        print(f"[clima-ext] error de API: {exc}", flush=True)
        return None


# ---------- DataSet: histórico real de Open-Meteo reproducido ----------

_rows = []


def load_dataset():
    path = os.path.join(DATA_DIR, "dataset.csv")
    if not os.path.exists(path):
        end = datetime.now(timezone.utc).date() - timedelta(days=6)
        start = end - timedelta(days=29)
        r = requests.get(
            "https://archive-api.open-meteo.com/v1/archive", headers=HEADERS, timeout=30,
            params={"latitude": LAT, "longitude": LON, "start_date": start.isoformat(),
                    "end_date": end.isoformat(), "timezone": "America/Bogota",
                    "hourly": "temperature_2m,relative_humidity_2m,surface_pressure"})
        r.raise_for_status()
        h = r.json()["hourly"]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "temperature", "humidity", "pressure_hpa"])
            w.writerows(zip(h["time"], h["temperature_2m"],
                            h["relative_humidity_2m"], h["surface_pressure"]))
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def dataset():
    global _rows
    try:
        if not _rows:
            _rows = load_dataset()
        row = _rows[int(time.time() // 60) % len(_rows)]
        return {"temperature": float(row["temperature"]),
                "humidity": float(row["humidity"]),
                "pressure_hpa": float(row["pressure_hpa"])}
    except Exception as exc:  # noqa: BLE001
        print(f"[historico] error de dataset: {exc}", flush=True)
        return None


# ---------- dispositivo genérico con retroceso ----------

class Device(threading.Thread):
    def __init__(self, client, dev_id, origin, interval, sampler):
        super().__init__(daemon=True)
        self.client, self.id, self.origin = client, dev_id, origin
        self.default_interval, self.interval = interval, interval
        self.sampler = sampler
        self.enabled, self.offsets, self.forced = True, {}, {}
        self.wake = threading.Event()

    def apply(self, cmd):
        if "power" in cmd:
            self.enabled = str(cmd["power"]).lower() == "on"
        if "interval" in cmd:
            self.interval = max(1.0, float(cmd["interval"]))
        if "calibrate" in cmd:
            self.offsets = {k: float(v) for k, v in dict(cmd["calibrate"]).items()}
        if "force" in cmd:
            self.forced = {k: float(v) for k, v in dict(cmd["force"]).items()}
        if cmd.get("reset"):
            self.enabled, self.interval = True, self.default_interval
            self.offsets, self.forced = {}, {}
        print(f"[{self.id}] comando {cmd}", flush=True)
        self.publish_state()
        self.wake.set()

    def publish_state(self):
        state = {"origin": self.origin, "enabled": 1 if self.enabled else 0,
                 "interval": self.interval, "offsets": self.offsets, "forced": self.forced}
        self.client.publish(f"{BASE}/{self.id}/state", json.dumps(state), qos=1, retain=True)

    def run(self):
        self.publish_state()
        while True:
            if self.enabled:
                values = self.sampler()
                if values:
                    values = {k: round(v + self.offsets.get(k, 0.0), 2) for k, v in values.items()}
                    values.update(self.forced)
                    values["origin"] = self.origin
                    self.client.publish(f"{BASE}/{self.id}/telemetry", json.dumps(values), qos=1)
                    print(f"[{self.id}] {values}", flush=True)
            self.wake.wait(self.interval)
            self.wake.clear()


def build_devices(client):
    spec = [
        ("rack-a", "Digital Twin", 10, make_rack(26, 6)),
        ("rack-b", "Digital Twin", 10, make_rack(24.5, 5)),
        ("rack-c", "Digital Twin", 10, make_rack(27.5, 7)),
        ("pasillo", "Digital Twin", 10, pasillo),
        ("puerta", "Digital Twin", 10, make_door()),
        ("ups", "Digital Twin", 10, make_ups()),
        ("clima-ext", "API", 600, weather),
        ("historico", "DataSet", 60, dataset),
        ("vm-host", "Real", 10, host),
    ]
    return {d[0]: Device(client, *d) for d in spec}


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    devices = build_devices(client)

    def on_connect(c, userdata, flags, reason_code, properties):
        print(f"conectado a {HOST}:{PORT} ({reason_code})", flush=True)
        c.subscribe(f"{BASE}/+/cmd")

    def on_message(c, userdata, msg):
        dev = devices.get(msg.topic.split("/")[-2])
        if dev is None:
            return
        try:
            dev.apply(json.loads(msg.payload))
        except (ValueError, TypeError, AttributeError) as exc:
            print(f"[{dev.id}] comando ignorado: {exc}", flush=True)

    client.on_connect, client.on_message = on_connect, on_message
    while True:
        try:
            client.connect(HOST, PORT, keepalive=60)
            break
        except OSError as exc:
            print(f"broker no disponible ({exc}), reintento en 5 s", flush=True)
            time.sleep(5)
    client.loop_start()
    for dev in devices.values():
        dev.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
