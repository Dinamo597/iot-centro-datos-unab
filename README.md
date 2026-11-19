# Lab 5 – Plataforma IoT propia (MQTT → InfluxDB → Grafana)

```
publisher (Python) --MQTT--> Mosquitto --> Telegraf --> InfluxDB --> Grafana
```

Variables: temperature (°C), humidity (%), illuminance (lx). Escenario: sala de servidores UNAB.

## 1. Preparar la VM (Ubuntu 24.04)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # luego cerrar sesión SSH y volver a entrar
docker compose version
```

En Azure, abrir el puerto **3000/TCP** solo para tu IP (VM → Redes). El 1883 NO se abre.

## 2. Configurar y levantar

```bash
cp .env.example .env && nano .env     # cambiar TODAS las claves y el token
docker compose up -d --build
docker compose ps                     # los 5 servicios en "running"
```

## 3. Verificar

```bash
docker compose logs -f publisher                       # JSON enviándose
docker compose exec mosquitto mosquitto_sub -t 'unab/sala1/#' -C 3
docker compose exec influxdb influx query \
  'from(bucket:"iot") |> range(start:-5m) |> filter(fn:(r)=>r._measurement=="ambiente") |> last()' \
  --org unab --token "$(grep INFLUX_TOKEN .env | cut -d= -f2)"
```

Grafana: `http://IP_DE_LA_VM:3000` (usuario/clave del `.env`). El datasource y el
dashboard "Sala de servidores UNAB - Ambiente" ya vienen provisionados.

## 4. Alerta de umbral (Grafana → Alerting)

1. Alert rules → New alert rule. Nombre: `Temperatura alta`.
2. Query A (datasource InfluxDB, Flux):
   ```
   from(bucket: "iot")
     |> range(start: -5m)
     |> filter(fn: (r) => r._measurement == "ambiente" and r._field == "temperature")
   ```
3. Expressions: Reduce (Last) → Threshold `IS ABOVE 30`.
4. Evaluar cada 1 m, pending period 1 m.
5. Contact point: tipo Webhook (por ejemplo una URL de https://webhook.site) o un log.

Forzar y limpiar la condición:

```bash
docker compose exec mosquitto mosquitto_pub -t unab/sala1/control -m '{"temperature": 35}'
docker compose exec mosquitto mosquitto_pub -t unab/sala1/control -m '{}'
```

## 5. Evidencias (carpeta `evidencias/`)

- Consulta a InfluxDB con filas recientes.
- Dashboard con la ventana de 4 días.
- Alerta en estado Firing y la notificación recibida.

## Notas

- Las claves viven en `.env` (no se versiona). Ver `.gitignore`.
- Los volúmenes (`influx_data`, `grafana_data`, `mosquitto_data`) conservan los datos entre reinicios.
- Migración a Raspberry Pi: las imágenes usadas tienen versión arm64; basta copiar la carpeta y repetir el paso 2.

---

# Parcial 2 – Centro de datos UNAB (10 dispositivos, HTTPS público)

| # | Dispositivo | Origen | Protocolo | Variables |
|---|---|---|---|---|
| 1 | sala-01 | Digital Twin | MQTT (`unab/sala1/telemetry`, control `unab/sala1/control`) | temperature, humidity, illuminance |
| 2-4 | rack-a, rack-b, rack-c | Digital Twin | MQTT (`unab/dc/<id>/telemetry`) | temperature, humidity, power_kw |
| 5 | pasillo | Digital Twin | MQTT | temperature, humidity |
| 6 | puerta | Digital Twin | MQTT | door_open, vibration |
| 7 | ups | Digital Twin | MQTT | battery_pct, load_pct, input_voltage |
| 8 | clima-ext | API (Open-Meteo, HTTPS) → MQTT | MQTT | temperature, humidity, wind_kmh, pressure_hpa |
| 9 | historico | DataSet (histórico real Open-Meteo) → MQTT | MQTT | temperature, humidity, pressure_hpa |
| 10 | vm-host | Real (la VM de Azure, psutil) | MQTT | cpu_pct, mem_pct, disk_pct, load1 |

## Despliegue (sobre el stack del Lab 5 ya corriendo)

1. En Azure: IP pública de la VM → Configuración → **Nombre DNS** (por ejemplo `centrodatos-unab`). El FQDN queda `centrodatos-unab.<region>.cloudapp.azure.com`.
2. En Azure → Redes: reglas de entrada **80** y **443** (cualquier origen).
3. En la VM: `echo "DOMAIN=<fqdn>" >> .env` y `docker compose up -d --build`.
4. Abrir `https://<fqdn>` (Caddy obtiene el certificado Let's Encrypt solo). Los evaluadores entran en modo lectura (visor anónimo); el admin entra con usuario y clave del `.env`.

## Retroceso (comandos hacia el dispositivo)

```bash
# apagar / encender
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"power":"off"}'
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"power":"on"}'
# cambiar el intervalo de envío (s)
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"interval":5}'
# calibrar (offset aditivo) y forzar un valor
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"calibrate":{"temperature":-1.5}}'
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"force":{"temperature":35}}'
# volver a valores por defecto
docker compose exec mosquitto mosquitto_pub -t unab/dc/rack-a/cmd -m '{"reset":true}'
```

El estado reportado de cada dispositivo (`unab/dc/<id>/state`, retenido) aparece en la tabla del dashboard.

## Reglas de alerta sugeridas

- `Rack temperatura alta`: `temperature` de `rack-.*` > 30 °C durante 1 min.
- `UPS batería baja`: `battery_pct` de `ups` < 50 %.
- `Puerta abierta`: `door_open` = 1 sostenido por 1 min.
