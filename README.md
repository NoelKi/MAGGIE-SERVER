# MAGGIE Ground Station Server

**REXUS Programme — v0.2.0**

REST-API-Backend der MAGGIE Ground Station. Empfängt Telemetrie vom Onboard Computer (OBC) via UDP, speichert sie in InfluxDB und stellt sie dem MAGGIE-GS-Frontend über eine JWT-gesicherte REST-API bereit. Nutzerkonten und Authentifizierung laufen über PostgreSQL.

---

## Inhaltsverzeichnis

- [Voraussetzungen](#voraussetzungen)
- [Schnellstart](#schnellstart)
- [Konfiguration](#konfiguration)
- [Docker-Container](#docker-container)
- [Datenbankmigrationen](#datenbankmigrationen)
- [Standardnutzer](#standardnutzer)
- [API-Endpunkte](#api-endpunkte)
- [OBC-UDP-Protokoll](#obc-udp-protokoll)
- [RXSM-TC-Protokoll](#rxsm-tc-protokoll)
- [Bodentest (TEST-State)](#bodentest-test-state)
- [Projektstruktur](#projektstruktur)
- [Entwicklung](#entwicklung)

---

## Voraussetzungen

| Tool | Version |
|------|---------|
| Python | ≥ 3.10 |
| Docker | ≥ 24 |
| Docker Compose | v2 |

---

## Schnellstart

```bash
# 1. Repository klonen
git clone <repo-url>
cd MAGGIE_server

# 2. Virtuelle Umgebung erstellen und Abhängigkeiten installieren
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Umgebungsvariablen konfigurieren
cp .env.example .env
# → .env anpassen (JWT-Secret, DB-Passwörter, InfluxDB-Token)

# 4. Docker-Container starten (PostgreSQL + InfluxDB)
docker compose up -d

# 5. Datenbank initialisieren und migrieren
flask db upgrade

# 6. Server starten
python run.py
```

Der Server läuft danach auf **http://localhost:3000** und hört auf UDP-Port **9000** für OBC-Pakete.

---

## Konfiguration

Alle Einstellungen werden über Umgebungsvariablen (`.env`) eingelesen.

| Variable | Beschreibung | Beispiel |
|---|---|---|
| `JWT_SECRET_KEY` | Geheimer Schlüssel für JWT-Signierung | `103039dfa2...` |
| `POSTGRES_USER` | PostgreSQL-Benutzer | `maggie` |
| `POSTGRES_PASSWORD` | PostgreSQL-Passwort | `maggie_secret` |
| `POSTGRES_HOST` | PostgreSQL-Host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL-Port | `5432` |
| `POSTGRES_DB` | Datenbankname | `maggie_users` |
| `INFLUX_URL` | InfluxDB-URL | `http://localhost:8086` |
| `INFLUX_TOKEN` | InfluxDB-API-Token | `XZpMX...` |
| `INFLUX_ORG` | InfluxDB-Organisation | `MAGGIE` |
| `INFLUX_BUCKET` | InfluxDB-Bucket | `MAGGIE_DB` |
| `UDP_HOST` | UDP-Listener-Adresse | `0.0.0.0` |
| `UDP_PORT` | UDP-Listener-Port | `9000` |

Eine Vorlage befindet sich in `.env.example`.

---

## Docker-Container

`docker-compose.yml` definiert zwei persistente Container:

| Container | Image | Port | Beschreibung |
|---|---|---|---|
| `postgres` | postgres:16 | 5432 | Nutzerdatenbank |
| `influxdb` | influxdb:2.7 | 8086 | Zeitreihendatenbank für Telemetrie |

```bash
# Container starten
docker compose up -d

# Bereits existierende Container (ohne Compose) starten
docker start influxdb postgres

# Logs anzeigen
docker compose logs -f
```

---

## Datenbankmigrationen

Das Projekt verwendet **Flask-Migrate** (Alembic) für Schemamigrationen.

```bash
# Aktuelle Migration anzeigen
flask db current

# Alle Migrationen auflisten
flask db history

# Neue Migration nach Modelländerung erstellen
flask db migrate -m "beschreibung"

# Migrationen anwenden
flask db upgrade

# Eine Migration zurückrollen
flask db downgrade
```

> **Hinweis:** Der Ordner `migrations/` wird von Pylance/Pyright absichtlich ausgeschlossen (`pyrightconfig.json`), da Alembic dynamische Proxys verwendet, die False-Positives erzeugen.

---

## Standardnutzer

Beim ersten Start werden folgende Nutzer automatisch in der Datenbank angelegt:

| Benutzername | Passwort | Rolle |
|---|---|---|
| `admin` | `maggie2026` | `admin` |
| `operator` | `rexus` | `operator` |

Rollen und ihre Berechtigungen:

| Rolle | Login | Telemetrie lesen | Telemetrie schreiben | Nutzer anlegen |
|---|:---:|:---:|:---:|:---:|
| `viewer` | ✅ | ✅ | ❌ | ❌ |
| `operator` | ✅ | ✅ | ✅ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ |

---

## API-Endpunkte

Alle Endpunkte leben unter dem Präfix `/api`. JWT-Token werden im Header übergeben:
```
Authorization: Bearer <access_token>
```

### Authentifizierung

#### `POST /api/auth/login`
Gibt ein JWT-Token zurück.

**Body:**
```json
{
  "username": "admin",
  "password": "maggie2026"
}
```

**Response `200`:**
```json
{
  "access_token": "eyJ...",
  "user": { "id": 1, "username": "admin", "role": "admin" }
}
```

---

#### `POST /api/auth/register`
Legt einen neuen Nutzer an. Nur für Admins.

**Header:** `Authorization: Bearer <token>`

**Body:**
```json
{
  "username": "alice",
  "password": "sicheres_passwort",
  "role": "operator",
  "email": "alice@example.com"
}
```

**Response `201`:**
```json
{
  "message": "User angelegt",
  "user": { "id": 3, "username": "alice", "role": "operator" }
}
```

---

#### `GET /api/auth/me`
Gibt den aktuell eingeloggten Nutzer zurück.

**Header:** `Authorization: Bearer <token>`

**Response `200`:**
```json
{
  "id": 1,
  "username": "admin",
  "role": "admin",
  "email": null,
  "created_at": "2025-01-01T00:00:00"
}
```

---

### Telemetrie

#### `POST /api/telemetry`
Schreibt einen einzelnen Messwert in InfluxDB.
Erfordert Rolle `operator` oder `admin`.

**Body:**
```json
{
  "measurement": "sensors",
  "fields": {
    "temperature": 23.4,
    "pressure": 1013.25
  },
  "tags": {
    "sensor_id": "imu_1"
  }
}
```

**Response `201`:**
```json
{ "status": "ok", "measurement": "sensors" }
```

---

#### `POST /api/telemetry/batch`
Schreibt mehrere Messpunkte in einem Request.
Erfordert Rolle `operator` oder `admin`.

**Body:**
```json
{
  "points": [
    {
      "measurement": "imu",
      "fields": { "ax": 0.12, "ay": -0.03, "az": 9.81 },
      "tags": { "phase": "ascent" }
    },
    {
      "measurement": "environment",
      "fields": { "temperature": 21.3, "pressure": 1012.1 }
    }
  ]
}
```

**Response `201`:**
```json
{ "status": "ok", "written": 2 }
```

---

#### `GET /api/telemetry`
Liest Messpunkte aus InfluxDB.

**Query-Parameter:**

| Parameter | Typ | Pflicht | Standard | Beschreibung |
|---|---|:---:|---|---|
| `measurement` | string | ✅ | — | Name der Messung |
| `start` | string | ❌ | `-1h` | Flux-Zeitangabe (z.B. `-30m`, `2025-01-01T00:00:00Z`) |
| `stop` | string | ❌ | `now()` | Flux-Zeitangabe |
| `limit` | int | ❌ | `500` | Maximale Anzahl Datenpunkte |

**Beispiel:**
```
GET /api/telemetry?measurement=imu&start=-30m&limit=100
```

**Response `200`:**
```json
{
  "measurement": "imu",
  "count": 42,
  "data": [ { "time": "...", "ax": 0.12, ... } ]
}
```

---

#### `GET /api/telemetry/ping`
Prüft ob InfluxDB erreichbar ist.

**Response `200`:**
```json
{ "influx": true }
```

---

### Telecommands

Der Server verpackt jedes Kommando als 6-Byte-Frame in ein 24-Byte-RXSM-SDC-Paket
und schreibt es auf `TC_SERIAL_PORT`. Vollständige Protokollreferenz:
`MAGGIE_OBC/docs/TESTSTATE_PROTOKOLL.md`.

#### `POST /api/command/test`
Schaltet den OBC in den Bodentest-Zustand oder zurück.

**Body:** `{ "action": "enter" | "exit", "dest": 0 }`

`enter` akzeptiert der OBC nur aus `PRE_LAUNCH`. Nur im Zustand `TEST` führt er
Motorkommandos aus — Ausnahme ist `off`, das als Sicherheitskommando immer gilt.

---

#### `POST /api/command/motor`
Steuert den Motor.

**Body:** `{ "action": "on"|"off"|"zero"|"turn"|"goto", "speed": 220, "angle": 180, "dest": 0 }`

| Aktion | Wirkung |
|---|---|
| `on` | Motor dreht mit `speed` als PWM, bis `off` kommt |
| `off` | Motor stoppen (in jedem Zustand erlaubt) |
| `zero` | Encoder-Zähler auf 0 setzen, Motor stoppt dabei |
| `turn` | Drehung um `angle` Grad **relativ**, der OBC stoppt am Encoder-Ziel |
| `goto` | Fahrt auf `angle` Grad **absolut** zur Encoder-Null |

`speed` gilt nur bei `on`: das Vorzeichen gibt die Drehrichtung vor. Fehlt der
Wert oder ist er `0`, nimmt der OBC seine `DEFAULT_ON_SPEED`.

**Der Betrag muss mindestens `MOTOR_MIN_DRIVE = 220` sein**, gültig ist also `0`
oder `220..255`. Darunter läuft der Motor nicht an — am Aufbau gemessen: unter
etwa 150 dreht er selbst ohne Last nicht mehr. Kleinere Beträge liefern `400`,
statt still angehoben zu werden.

`angle` ist bei `turn` und `goto` Pflicht: Grad `-3600..3600`. Die
Geschwindigkeit setzt der OBC fest (`TURN_SPEED`), `speed` wirkt dort nicht.
Ohne angeschlossenen Encoder verwirft der OBC beide Kommandos.

**Für wiederholtes Auf/Zu `goto` nehmen, nicht `turn`.** Jede Fahrt endet ein
Stück hinter dem Ziel, und dieser Nachlauf ist richtungsabhängig. Bei `turn`
erbt jede Fahrt die Endlage der vorherigen, der Fehler addiert sich also auf und
die Nullage wandert. Bei `goto` bezieht sich jedes Ziel auf denselben
Nullpunkt, der Fehler bleibt beschränkt. `goto` fährt nicht, wenn die Position
schon im Zielfenster (`POS_DEADBAND`) liegt.

Werte außerhalb der Bereiche werden mit `400` abgewiesen, statt still geklemmt
zu werden.

**Response `201`:**
```json
{ "status": "ok", "action": "motor on (speed=120)", "opcode": 1, "arg": 120,
  "mcnt": 4, "dest": 0, "sent_hex": "eb90a504067e0100788c7f…" }
```

Ein `on`-Dauerlauf stoppt nicht von selbst. Neben `off` beenden auch
`test {"action":"exit"}` und `abort` die Fahrt; zusätzlich schaltet der OBC den
Motor nach `MOTOR_ON_TIMEOUT_MS` (30 s) selbsttätig ab — das ist auch die
Rückfallebene, falls bei `turn` der Encoder ausfällt.

---

#### `POST /api/command/abort`
Missionsabbruch: der OBC geht nach `ABORT` und stoppt alle Aktoren.

---

#### `GET /api/command/status`, `POST /api/command/connect|disconnect`
Status bzw. Auswahl des seriellen TC-Ports zur Laufzeit.

---

### System

#### `GET /api/health`
Gibt den Server-Status zurück. Kein Auth erforderlich.

**Response `200`:**
```json
{ "status": "ok" }
```

---

## OBC-UDP-Protokoll

Der OBC sendet Telemetriepakete über Ethernet/UDP an Port **9000**. Jedes Paket ist exakt **64 Bytes** groß (Big-Endian).

### Kommunikationsarchitektur

```
BODEN ──[TC 60 Hz GMSK]──► RXSM ──[UART]──► OBC (Experiment)
BODEN ◄─[TM Downlink]───── RXSM ◄─[UDP/ETH]─ OBC (Experiment)
```

### Paketstruktur

```
┌─────────────────────────────────────────────────────────────┐
│ HEADER  (16 Bytes)                                          │
│  [0:2]   Magic          0x4D41 ('MA')           uint16      │
│  [2:4]   Version        0x0001                  uint16      │
│  [4:8]   Sequence       Paketzähler seit Boot   uint32      │
│  [8:12]  Timestamp      ms seit Lift-off        uint32      │
│  [12:13] Type           Measurement-ID          uint8       │
│  [13:14] Flags          RXSM-Signale (s.u.)     uint8       │
│  [14:16] CRC16          XModem über Bytes 0–13  uint16      │
├─────────────────────────────────────────────────────────────┤
│ PAYLOAD (48 Bytes)  — abhängig vom Type                     │
│  0x01  IMU         6× float32  ax ay az gx gy gz            │
│  0x02  ENVIRONMENT 3× float32  temp pressure humidity       │
│  0x04  SYSTEM      4× float32  cpu_temp bat_v bat_i uptime  │
│  0xFF  HEARTBEAT   1× uint32   boot_count                   │
└─────────────────────────────────────────────────────────────┘
```

### Flag-Byte

| Bit | Maske | Signal | Quelle |
|-----|-------|--------|--------|
| 0 | `0x01` | Lift-Off | RXSM LO-Pins |
| 1 | `0x02` | SODS aktiv | RXSM SMC, Byte 1 |
| 2 | `0x04` | SOEX aktiv | RXSM SMC, Byte 2 |
| 3 | `0x08` | Motor-Burnout | OBC (IMU-Erkennung) |
| 4 | `0x10` | Apogee | OBC (Baro/IMU) |
| 5 | `0x20` | Parachute deployed | OBC (Baro-Erkennung) |

### C-Struct (OBC-seitig)

```c
#pragma pack(push, 1)
typedef struct {
    uint16_t magic;         // 0x4D41 ('MA')
    uint16_t version;       // 0x0001
    uint32_t sequence;      // Paketzähler seit Boot
    uint32_t timestamp_ms;  // ms seit Lift-off
    uint8_t  type;          // Measurement-Type
    uint8_t  flags;         // RXSM-Signale (Bit-Feld)
    uint16_t crc16;         // CRC-16/XModem über Bytes 0..13
} MaggieHeader;             // = 16 Bytes
#pragma pack(pop)
```

---

## RXSM-TC-Protokoll

Das RXSM sendet Telekommandos mit **60 Hz** über GMSK. Das Format ist **24 Bytes**:

```
[SYNC1][SYNC2][MSGID][MCNT][Data 0..15][CSM][CSM][CRC][CRC]
  0xEB   0x90
```

| MSGID | Typ | Beschreibung |
|-------|-----|--------------|
| `0x00` | SMC | System Management Command: Power, SODS, SOEX, Status |
| `0xA5` | SDC | Serial Data Command → UART-Weiterleitung an Experiment |

Die SODS- und SOEX-Flags im MAGGIE-Paketheader werden vom OBC gesetzt, nachdem er sie über den RXSM-UART-Kanal empfangen hat (SMC, MSGID=`0x00`, Data[1]/Data[2]).

---

## Bodentest (TEST-State)

Aufbau für Integration und Review: den Motor aus der Ground Station drehen und
stoppen und dabei IMU- sowie Encoder-Telemetrie live sehen — über denselben Weg
wie im Flug (GS → Server → RXSM Test Module → OBC).

### Ports konfigurieren

```bash
# .env
SERIAL_PORT=/dev/tty.usbserial-DOWNLINK     # RXSM -> Server (Telemetrie)
TC_SERIAL_PORT=/dev/tty.usbserial-UPLINK    # Server -> RXSM (Telecommands)
SERIAL_BAUD=38400
TC_SERIAL_BAUD=38400
```

Beide Ports lassen sich auch zur Laufzeit setzen
(`POST /api/downlink/connect`, `POST /api/command/connect`).

### Ablauf

```bash
# 1. Test-Zustand betreten — erst danach nimmt der OBC Aktorbefehle an
curl -X POST localhost:3000/api/command/test  -H 'Content-Type: application/json' -d '{"action":"enter"}'

# 2. Encoder nullen, dann vorwärts drehen und wieder stoppen
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"zero"}'
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"on","speed":220}'
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"off"}'

# 3. Rückwärts (negatives Vorzeichen = andere Drehrichtung)
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"on","speed":-220}'

# 3b. Halbe Umdrehung relativ — der OBC stoppt selbst am Encoder-Ziel
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"turn","angle":180}'

# 3c. HDRM auf/zu über absolute Positionen (driftet auch über viele Zyklen nicht)
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"goto","angle":180}'
curl -X POST localhost:3000/api/command/motor -H 'Content-Type: application/json' -d '{"action":"goto","angle":0}'

# 4. Zustand und Encoder-Position mitlesen
curl "localhost:3000/api/downlink/frames?since=0" | python3 -m json.tool

# 5. Test-Zustand verlassen — der Motor stoppt, Aktoren sind wieder gesperrt
curl -X POST localhost:3000/api/command/test  -H 'Content-Type: application/json' -d '{"action":"exit"}'
```

In der Ground Station läuft derselbe Ablauf über *Telecommands* (Zustand,
PWM-Sollwert, Drehen/Stopp) und *Telemetrie* (IMU-Verläufe, Encoder-Counts,
Winkel, PWM).

### Ohne Hardware proben

`tools/rxsm_obc_simulator.py` spielt den OBC inklusive Zustandsmaschine und
Motormodell nach:

```bash
python tools/rxsm_obc_simulator.py
#   Downlink (Server liest):    SERIAL_PORT=/dev/ttys012
#   Uplink   (Server schreibt): TC_SERIAL_PORT=/dev/ttys013
```

Die beiden ausgegebenen Ports in die `.env` eintragen, Server starten — Ground
Station, Telecommands und Telemetrie verhalten sich wie am echten Aufbau.

---

## Projektstruktur

```
MAGGIE_server/
├── .env                          # Echte Credentials (nicht committen!)
├── .env.example                  # Vorlage für neue Entwickler
├── .venv/                        # Python-Virtualenv
├── app/
│   ├── __init__.py               # create_app() Factory: DB, Migrate, JWT, CORS, UDP-Start
│   ├── extensions.py             # db = SQLAlchemy(), migrate = Migrate() (circular-import-safe)
│   ├── models/
│   │   └── user.py               # User-Modell (id, username, email, password_hash, role, ...)
│   ├── routes/
│   │   ├── auth.py               # POST /login, POST /register, GET /me
│   │   ├── health.py             # GET /api/health
│   │   └── telemetry.py          # POST/GET /telemetry, POST /telemetry/batch, GET /telemetry/ping
│   └── services/
│       ├── auth_service.py       # authenticate(), register_user(), get_user(), seed_default_users()
│       ├── influx_service.py     # write_telemetry(), write_telemetry_batch(), query_telemetry()
│       ├── packet_listener.py    # UDP-Daemon-Thread, Port 9000
│       ├── packet_parser.py      # 64-Byte-Binärprotokoll (parse_packet, build_packet, CRC-16/XModem)
│       └── rxsm_tc_parser.py     # 24-Byte GMSK TC-Pakete (SMC/SDC-Typen)
├── config/
│   └── settings.py               # Config-Klasse: JWT, PostgreSQL, InfluxDB, UDP
├── docker-compose.yml            # PostgreSQL 16 + InfluxDB 2.7 mit persistenten Volumes
├── migrations/                   # Alembic/Flask-Migrate (nicht manuell bearbeiten)
│   └── versions/
│       └── c434ae0b5186_initial_users_table.py
├── pyrightconfig.json            # Schließt migrations/ aus der Pylance-Analyse aus
├── requirements.txt              # Python-Abhängigkeiten
├── run.py                        # Einstiegspunkt, Banner, Port 3000
└── tools/
    ├── obc_simulator.py          # REXUS-Flugsimulator — sendet UDP-Pakete
    └── rxsm_obc_simulator.py     # OBC-Simulator für den seriellen RXSM-Pfad
```

---

## Entwicklung

### OBC-Simulator (UDP-Flugpfad)

Simuliert einen vollständigen REXUS-Flug und sendet UDP-Pakete an den Server:

```bash
python tools/obc_simulator.py
```

Der Simulator durchläuft automatisch alle Missionsphasen:
`STARTUP → PREFLIGHT_CHECK → STANDBY → ASCENT → MICROGRAVITY → DESCENT → RECOVERY`

### OBC-Simulator (serieller RXSM-Pfad)

Spielt den OBC am seriellen Downlink/Uplink nach — inklusive Zustandsmaschine,
Open-Loop-Motormodell (die Encoder-Position läuft proportional zur PWM) und
IMU-Telemetrie. Siehe
[Bodentest (TEST-State)](#bodentest-test-state).

```bash
python tools/rxsm_obc_simulator.py
```

### Linting

```bash
pylint app/ config/ run.py
```

### Tests

```bash
# Gesundheitsstatus prüfen
curl http://localhost:3000/api/health

# Einloggen und Token holen
curl -s -X POST http://localhost:3000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"maggie2026"}'

# Telemetrie abfragen (Token einsetzen)
curl http://localhost:3000/api/telemetry?measurement=imu \
  -H "Authorization: Bearer <token>"
```

### Neue Migration erstellen

```bash
# Nach Änderungen an app/models/
flask db migrate -m "add_column_xyz"
flask db upgrade
```
