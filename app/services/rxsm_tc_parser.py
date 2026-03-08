"""
MAGGIE – RXSM Telecommand Parser
=================================

Parst die 24-Byte GMSK-Telecommand-Pakete, die der RXSM mit 60 Hz
an den OBC sendet (und die der OBC als UART-Stream empfängt).

Das MAGGIE Ground-Station-System empfängt diese Pakete NICHT direkt –
der OBC übernimmt die RXSM-UART-Verarbeitung und spiegelt relevante
Signale (SODS, SOEX, LiftOff …) als Flag-Bits in seinen eigenen
64-Byte UDP-Telemetrie-Paketen an den MAGGIE-Server.

Dieser Parser ist für:
  • Debugging / Ground-Simulation (OBC-Ersatz)
  • Unit-Tests der OBC-Firmware-Logik in Python
  • Dokumentation des RXSM-TC-Protokolls

RXSM-TC-Paketstruktur (REXUS User Manual, Kap. 4.x):
┌────────┬──────────────────────────────────────────────────────┐
│ Byte   │ Bedeutung                                            │
├────────┼──────────────────────────────────────────────────────┤
│  [0]   │ SYNC1          – fester Wert 0xEB                    │
│  [1]   │ SYNC2          – fester Wert 0x90                    │
│  [2]   │ MSGID          – 0x00=SMC, 0xA5=SDC                  │
│  [3]   │ MCNT           – Message Counter (0-255, wraps)       │
│  [4]   │ Data[0]        – PWR CTRL   (siehe SMC-Tabelle unten)│
│  [5]   │ Data[1]        – SODS-Bits                           │
│  [6]   │ Data[2]        – SOE-Bits                            │
│  [7]   │ Data[3]        – UTE-Bits                            │
│  [8]   │ Data[4]        – Status-Bits                         │
│  [9]   │ Data[5]        – RCS CTRL                            │
│ [10-19]│ Data[6-15]     – für SMC unbenutzt / SDC: Nutzdaten  │
│ [20]   │ CSM            – Checksumme Byte 1                   │
│ [21]   │ CSM            – Checksumme Byte 2                   │
│ [22]   │ CRC            – CRC Byte 1 (Big-Endian)             │
│ [23]   │ CRC            – CRC Byte 2 (Big-Endian)             │
└────────┴──────────────────────────────────────────────────────┘

SMC (MSGID=0x00) – Data[0] PWR CTRL Bit-Mapping:
  Bit 7 (MSB): TM  – Telemetry TX power
  Bit 6:       TV  – TV downlink power
  Bit 5:       E1  – Experiment 1 power switch
  Bit 4:       E2  – Experiment 2 power switch
  Bit 3:       E3  – Experiment 3 power switch
  Bit 2:       E4  – Experiment 4 power switch
  Bit 1:       E5  – Experiment 5 power switch
  Bit 0 (LSB): E6  – Experiment 6 power switch

SDC (MSGID=0xA5) – Data[0] DEST+LEN Bit-Mapping:
  Bits [7:5]: DEST  – UART-Kanal-Adresse
  Bits [4:0]: LEN   – Anzahl Nutzdatenbytes in Data[1..n]
"""

from __future__ import annotations

from dataclasses import dataclass

# ─── Protokoll-Konstanten ────────────────────────────────────────────────────

TC_PACKET_SIZE = 24

SYNC1 = 0xEB
SYNC2 = 0x90

MSGID_SMC = 0x00   # System Management Command
MSGID_SDC = 0xA5   # Serial Data Command

# SMC Data[0] PWR CTRL Bit-Masken
PWR_TM = 0x80
PWR_TV = 0x40
PWR_E1 = 0x20
PWR_E2 = 0x10
PWR_E3 = 0x08
PWR_E4 = 0x04
PWR_E5 = 0x02
PWR_E6 = 0x01


# ─── Datenklassen ────────────────────────────────────────────────────────────

@dataclass
class SmcCommand:
    """Geparster SMC-Befehl (MSGID=0x00) vom RXSM."""
    mcnt: int           # Message Counter
    pwr_ctrl: int       # Data[0] – Power-Control-Byte (Rohwert)
    sods: int           # Data[1] – SODS-Bits (Rohwert)
    soe: int            # Data[2] – SOE-Bits  (Rohwert)
    ute: int            # Data[3] – UTE-Bits  (Rohwert)
    status: int         # Data[4] – Status-Bits (Rohwert)
    rcs_ctrl: int       # Data[5] – RCS-Control-Byte (Rohwert)
    data_raw: bytes     # alle 16 Data-Bytes (Data[0..15]) für Debugging

    # Convenience-Properties
    @property
    def tm_on(self) -> bool:
        """True wenn der TM-Power-Switch (Telemetrie-Sender) eingeschaltet ist."""
        return bool(self.pwr_ctrl & PWR_TM)

    @property
    def tv_on(self) -> bool:
        """True wenn der TV-Power-Switch (Video-Downlink) eingeschaltet ist."""
        return bool(self.pwr_ctrl & PWR_TV)

    @property
    def experiments_on(self) -> list[int]:
        """Gibt Liste der eingeschalteten Experiment-Nummern zurück (1-6)."""
        masks = [(PWR_E1, 1), (PWR_E2, 2), (PWR_E3, 3),
                 (PWR_E4, 4), (PWR_E5, 5), (PWR_E6, 6)]
        return [num for mask, num in masks if self.pwr_ctrl & mask]

    @property
    def sods_active(self) -> bool:
        """True wenn mindestens ein SODS-Bit gesetzt ist."""
        return self.sods != 0

    @property
    def soex_active(self) -> bool:
        """True wenn mindestens ein SOE-Bit gesetzt ist."""
        return self.soe != 0


@dataclass
class SdcCommand:
    """Geparster SDC-Befehl (MSGID=0xA5) vom RXSM."""
    mcnt: int           # Message Counter
    dest: int           # UART-Ziel-Adresse (Bits [7:5] von Data[0])
    length: int         # Anzahl Nutzdatenbytes (Bits [4:0] von Data[0])
    payload: bytes      # eigentliche Nutzdaten (max. 15 Bytes)
    data_raw: bytes     # alle 16 Data-Bytes für Debugging


@dataclass
class TcParseResult:
    """Ergebnis eines geparseten TC-Pakets."""
    raw: bytes          # Original-Rohbytes
    msgid: int          # 0x00 oder 0xA5
    mcnt: int           # Message Counter
    smc: SmcCommand | None = None
    sdc: SdcCommand | None = None
    csm: int = 0        # 16-Bit Checksumme
    crc: int = 0        # 16-Bit CRC
    crc_valid: bool = False

    @property
    def is_smc(self) -> bool:
        """True wenn das Paket ein SMC-Befehl ist (MSGID=0x00)."""
        return self.msgid == MSGID_SMC

    @property
    def is_sdc(self) -> bool:
        """True wenn das Paket ein SDC-Befehl ist (MSGID=0xA5)."""
        return self.msgid == MSGID_SDC


# ─── CRC-Berechnung ─────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
    """
    CRC-16 wie im REXUS-Manual spezifiziert.
    REXUS nutzt CRC-16/CCITT (Polynom 0x1021, Init 0xFFFF).
    Prüfe gegen das echte RXSM-Handbuch, falls abweichend.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def _csm(data: bytes) -> int:
    """
    Einfache 16-Bit Quersumme (Modulo 2^16) über die übergebenen Bytes.
    (CSM-Berechnung gem. REXUS User Manual – bitte gegen Handbuch verifizieren.)
    """
    total = sum(data) & 0xFFFF
    return total


# ─── Haupt-Parse-Funktion ───────────────────────────────────────────────────

def parse_tc_packet(raw: bytes) -> TcParseResult:
    """
    Parst ein 24-Byte RXSM-Telecommand-Paket.

    Args:
        raw: Exakt 24 Bytes Rohdaten vom RXSM-UART.

    Returns:
        TcParseResult mit dem geparseten Inhalt.

    Raises:
        ValueError: Bei falscher Länge oder ungültigem Sync.
    """
    if len(raw) != TC_PACKET_SIZE:
        raise ValueError(
            f"TC-Paket muss {TC_PACKET_SIZE} Bytes haben, bekommen: {len(raw)}"
        )

    sync1, sync2, msgid, mcnt = raw[0], raw[1], raw[2], raw[3]

    if sync1 != SYNC1 or sync2 != SYNC2:
        raise ValueError(
            f"Ungültiger Sync: 0x{sync1:02X} 0x{sync2:02X} "
            f"(erwartet 0x{SYNC1:02X} 0x{SYNC2:02X})"
        )

    data = raw[4:20]           # Data[0..15] — 16 Bytes
    csm_val = (raw[20] << 8) | raw[21]
    crc_val = (raw[22] << 8) | raw[23]

    # CRC über Bytes 0..21 (SYNC+MSGID+MCNT+Data+CSM)
    expected_crc = _crc16(raw[:22])
    crc_ok = (crc_val == expected_crc)

    result = TcParseResult(
        raw=raw,
        msgid=msgid,
        mcnt=mcnt,
        csm=csm_val,
        crc=crc_val,
        crc_valid=crc_ok,
    )

    if msgid == MSGID_SMC:
        result.smc = SmcCommand(
            mcnt=mcnt,
            pwr_ctrl=data[0],
            sods=data[1],
            soe=data[2],
            ute=data[3],
            status=data[4],
            rcs_ctrl=data[5],
            data_raw=bytes(data),
        )

    elif msgid == MSGID_SDC:
        dest = (data[0] >> 5) & 0x07
        length = data[0] & 0x1F
        payload = bytes(data[1:1 + length]) if length > 0 else b""
        result.sdc = SdcCommand(
            mcnt=mcnt,
            dest=dest,
            length=length,
            payload=payload,
            data_raw=bytes(data),
        )

    return result


# ─── Hilfsfunktion: TC-Paket bauen (für Tests / OBC-Simulation) ─────────────

def build_smc_packet(
    mcnt: int = 0,
    pwr_ctrl: int = 0x00,
    sods: int = 0x00,
    soe: int = 0x00,
    ute: int = 0x00,
    status: int = 0x00,
    rcs_ctrl: int = 0x00,
) -> bytes:
    """
    Baut ein gültiges 24-Byte SMC-TC-Paket (MSGID=0x00).

    Args:
        mcnt:     Message Counter (0-255).
        pwr_ctrl: Data[0] PWR-CTRL-Byte (Bit-Masken: PWR_TM, PWR_E1, …).
        sods:     Data[1] SODS-Bits.
        soe:      Data[2] SOE-Bits.
        ute:      Data[3] UTE-Bits.
        status:   Data[4] Status-Bits.
        rcs_ctrl: Data[5] RCS-CTRL-Byte.

    Returns:
        Gültige 24 Bytes (inkl. SYNC, CSM, CRC).
    """
    data = bytes([pwr_ctrl, sods, soe, ute, status, rcs_ctrl]) + bytes(10)

    header = bytes([SYNC1, SYNC2, MSGID_SMC, mcnt & 0xFF]) + data
    csm_val = _csm(header)
    header_with_csm = header + bytes([(csm_val >> 8) & 0xFF, csm_val & 0xFF])
    crc_val = _crc16(header_with_csm)
    packet = header_with_csm + bytes([(crc_val >> 8) & 0xFF, crc_val & 0xFF])

    assert len(packet) == TC_PACKET_SIZE
    return packet


def build_sdc_packet(
    mcnt: int = 0,
    dest: int = 0,
    payload: bytes = b"",
) -> bytes:
    """
    Baut ein gültiges 24-Byte SDC-TC-Paket (MSGID=0xA5).

    Args:
        mcnt:    Message Counter (0-255).
        dest:    UART-Ziel-Adresse (0-7).
        payload: Nutzdaten (max. 15 Bytes).

    Returns:
        Gültige 24 Bytes (inkl. SYNC, CSM, CRC).
    """
    if len(payload) > 15:
        raise ValueError("SDC-Payload darf maximal 15 Bytes haben.")

    dest_len_byte = ((dest & 0x07) << 5) | (len(payload) & 0x1F)
    data = bytes([dest_len_byte]) + payload + bytes(15 - len(payload))

    header = bytes([SYNC1, SYNC2, MSGID_SDC, mcnt & 0xFF]) + data
    csm_val = _csm(header)
    header_with_csm = header + bytes([(csm_val >> 8) & 0xFF, csm_val & 0xFF])
    crc_val = _crc16(header_with_csm)
    packet = header_with_csm + bytes([(crc_val >> 8) & 0xFF, crc_val & 0xFF])

    assert len(packet) == TC_PACKET_SIZE
    return packet
