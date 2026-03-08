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
│  0x03  GPS         4× float32  lat lon alt speed            │
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
    └── obc_simulator.py          # REXUS-Flugsimulator — sendet UDP-Pakete
```

---

## Entwicklung

### OBC-Simulator

Simuliert einen vollständigen REXUS-Flug und sendet UDP-Pakete an den Server:

```bash
python tools/obc_simulator.py
```

Der Simulator durchläuft automatisch alle Missionsphasen:
`STARTUP → PREFLIGHT_CHECK → STANDBY → ASCENT → MICROGRAVITY → DESCENT → RECOVERY`

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
