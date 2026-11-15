# CLAUDE.md – MAGGIE Server

Flask-Bodenstationsserver: Downlink dekodieren (`app/services/downlink_frame_parser.py`),
Telemetrie in InfluxDB, Telecommands an den OBC (`app/routes/command.py`).
Firmware-Gegenstück: `../MAGGIE_OBC` (siehe dort `CLAUDE.md` und `docs/HAL_OVERVIEW.md`).

## Feste Regeln

### 1. Roboterarm nicht unter Schwerkraft fahren

Die Gelenke des Arms sind nur für Mikrogravitation ausgelegt. Am Boden können
sie das Gewicht des Arms nicht halten – wird der ganze Arm unter Schwerkraft
bewegt, gehen die Gelenke kaputt. Der vollständige Arm wird erst in
Mikrogravitation getestet.

- Am Boden ist nur der **Einzelgelenk-Test** zulässig: ein Gelenk ohne
  Armlast, in kleinen Jog-Schritten, langsam (`POST /api/command/arm`).
- Keine Endpunkte, Skripte oder Befehlsfolgen bauen oder ausführen, die
  mehrere Gelenke oder den ganzen Arm am Boden bewegen (z. B. Trajektorien,
  Homing-Fahrten, mehrere Gelenke gleichzeitig joggen).
- Die Grenzen in `command.py` nicht lockern (`ARM_JOG_MAX_DEG`, Gelenk 0 =
  "alle" nur für stop/inhibit). Soll daran etwas geändert werden, vorher beim
  Nutzer nachfragen.

### 2. LISA-Referenzparser nicht verändern

`../MAGGIE_OBC/extern/lisa-if/` ist eine unveränderte Kopie aus einem fremden
Repo und darf nicht bearbeitet werden – nur lesen bzw. zum Gegenprüfen der
Skalierungen ausführen (mit `PYTHONDONTWRITEBYTECODE=1`). Sieht dort etwas
falsch aus: dem Nutzer melden.
