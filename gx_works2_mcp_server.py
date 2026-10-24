"""
GX Works2 MCP Server (Codex-Gx / Antigravity / VS Code Compatible)
==================================================================
A complete Model Context Protocol (MCP) stdio server for Mitsubishi MELSOFT
GX Works2 (FXCPU, QCPU, and LCPU projects).

This server replicates the exact workflow seen in the "Codex-Gx" demonstration
and incorporates patterns from open-source PLC MCP projects (such as
github.com/Arienax/gxworks-agent):

1. Pre-Flight family-aware Mnemonic and Device Validator:
   - Rejects unsupported opcodes, invalid operands and out-of-range devices before GX Works2 I/O.
   - Enforces octal X/Y addressing and conservative linear-rung structure.

2. Static Safety & Interlock Analyzer:
   - Detects same-scan Star-Delta contactor switching hazards (warns if Star turns
     off and Delta turns on in the exact same scan without a dead-time timer).
   - Detects duplicate OUT coils and unused commented I/O devices.

3. Serialized GX Works2 GUI Automation + Native CSV Round-Trip Bridge (pywinauto):
   - Writes ladder logic into MAIN via UTF-16LE Tab-delimited CSV import
     ("Edit -> Read from CSV File...") after saving a verified backup.
   - Reads the compiled program back via "Edit -> Write to CSV File..." into
     ~/Documents/Codex-Gx/verify-MAIN-<timestamp>.csv to verify step counts.
   - Generates Device Comment CSV artifacts (automatic GUI import is not enabled).
   - Triggers "Compile -> Build (F4)" and "Tool -> Check Program...".

Installation (Windows 10/11 with GX Works2 installed):
------------------------------------------------------
    pip install "mcp[cli]" pywinauto

Running via Antigravity or VS Code MCP Configuration:
-----------------------------------------------------
    Command: python
    Args: ["C:/path/to/gxworks2_mcp_server.py"]
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import unicodedata
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure standard I/O encoding is UTF-8 for MCP stdio transport
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise SystemExit(
        "Missing 'mcp' package. Install it with: pip install \"mcp[cli]\" pywinauto"
    ) from exc

# Optional Windows GUI automation imports (gracefully handled if tested offline)
try:
    from pywinauto import Application
    from pywinauto.keyboard import send_keys

    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False


# ============================================================================
# 1. CONFIGURATION & CONSTANTS
# ============================================================================

WORKSPACE_DIR = Path.home() / "Documents" / "Codex-Gx"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

GX_WINDOW_TITLE_REGEX = r".*MELSOFT Series GX Works2.*"
GX_WM_COMMAND = 0x0111
GX_READ_CSV_COMMAND = 0x755
GX_WRITE_CSV_COMMAND = 0x756
GX_CSV_DIALOG_TIMEOUT = 15.0
GX_CSV_OPERATION_TIMEOUT = 30.0
GXWORKS2_EXECUTABLE_ENV = "GXWORKS2_EXE"
GUI_TRANSACTION_LOCK = threading.RLock()
GUI_ACTIVE_WRITE: Dict[str, Any] = {}


@dataclass(frozen=True)
class PLCProfile:
    """Safe, family-level capabilities used for CSV and device validation."""
    name: str
    csv_prefix: str
    x_y_base: int
    ranges: Dict[str, Optional[Tuple[int, int]]]
    m_segments: Optional[Tuple[Tuple[int, int], ...]] = None


PLC_PROFILES: Dict[str, PLCProfile] = {
    "FXCPU": PLCProfile(
        "FX3U/FX3UC", "FXCPU", 8,
        {"X": (0, 0o377), "Y": (0, 0o377), "M": None, "S": (0, 4095),
         "T": (0, 511), "C": (0, 255), "D": (0, 8511), "V": (0, 7), "Z": (0, 7)},
        ((0, 7679), (8000, 8511)),
    ),
    "QCPU": PLCProfile(
        "QCPU", "QCPU", 16,
        {"X": (0, 0xFFF), "Y": (0, 0xFFF), "M": (0, 8191), "S": (0, 2047),
         "T": (0, 2047), "C": (0, 1023), "D": (0, 12287), "W": (0, 0x7FFF),
         "R": (0, 32767), "Z": (0, 19)},
    ),
    "LCPU": PLCProfile(
        "LCPU", "LCPU", 16,
        {"X": (0, 0xFFF), "Y": (0, 0xFFF), "M": (0, 8191), "S": (0, 2047),
         "T": (0, 2047), "C": (0, 1023), "D": (0, 9999), "W": (0, 0x7FFF),
         "R": (0, 32767), "Z": (0, 19)},
    ),
}

PLC_TYPE_ALIASES: Dict[str, str] = {
    "FX": "FXCPU", "FXCPU": "FXCPU", "FX1S": "FXCPU", "FX1N": "FXCPU",
    "FX2N": "FXCPU", "FX2NC": "FXCPU", "FX3S": "FXCPU", "FX3G": "FXCPU",
    "FX3U": "FXCPU", "FX3UC": "FXCPU", "FX3U/FX3UC": "FXCPU",
    "Q": "QCPU", "QCPU": "QCPU", "L": "LCPU", "LCPU": "LCPU",
}


def resolve_plc_profile(plc_type: str) -> Tuple[Optional[PLCProfile], Optional[str]]:
    """Resolve a family or known model name without guessing unknown models."""
    if not isinstance(plc_type, str) or not plc_type.strip():
        return None, "plc_type must be a non-empty string."
    requested = re.sub(r"\s+", "", plc_type.strip().upper())
    family_key = PLC_TYPE_ALIASES.get(requested)
    if family_key is None:
        if requested.startswith("FX"):
            family_key = "FXCPU"
        elif requested.startswith("Q"):
            family_key = "QCPU"
        elif requested.startswith("L"):
            family_key = "LCPU"
    if family_key is None or family_key not in PLC_PROFILES:
        return None, (
            f"Unsupported plc_type '{plc_type}'. Use an FXCPU, QCPU, or LCPU family/model "
            "known to GX Works2."
        )
    base = PLC_PROFILES[family_key]
    return PLCProfile(
        plc_type.strip(), base.csv_prefix, base.x_y_base, base.ranges, base.m_segments
    ), None

# Supported conservative FX3U/FX3UC instruction subset and operand counts.
SUPPORTED_OPCODES: Dict[str, Tuple[int, int]] = {
    # Contacts (1 operand, or 2 for inline comparisons like LD= D0 K10)
    "LD": (1, 1),
    "LDI": (1, 1),
    "LDP": (1, 1),
    "LDF": (1, 1),
    "AND": (1, 1),
    "ANI": (1, 1),
    "ANDP": (1, 1),
    "ANDF": (1, 1),
    "OR": (1, 1),
    "ORI": (1, 1),
    "ORP": (1, 1),
    "ORF": (1, 1),
    # Inline Comparison Contacts (2 operands: S1, S2)
    "LD=": (2, 2),
    "LD>": (2, 2),
    "LD<": (2, 2),
    "LD<>": (2, 2),
    "LD<=": (2, 2),
    "LD>=": (2, 2),
    "AND=": (2, 2),
    "AND>": (2, 2),
    "AND<": (2, 2),
    "AND<>": (2, 2),
    "AND<=": (2, 2),
    "AND>=": (2, 2),
    "OR=": (2, 2),
    "OR>": (2, 2),
    "OR<": (2, 2),
    "OR<>": (2, 2),
    "OR<=": (2, 2),
    "OR>=": (2, 2),
    # Branch merges & stack ops (0 operands)
    "ANB": (0, 0),
    "ORB": (0, 0),
    "MPS": (0, 0),
    "MRD": (0, 0),
    "MPP": (0, 0),
    "INV": (0, 0),
    "MEP": (0, 0),
    "MEF": (0, 0),
    "NOP": (0, 0),
    "END": (0, 0),
    # Coils & Bit instructions
    "OUT": (1, 2),   # 1 operand for bit (OUT Y0), 2 for Timer/Counter (OUT T0 K50)
    "SET": (1, 1),
    "RST": (1, 1),
    "PLS": (1, 1),
    "PLF": (1, 1),
    # Basic Application Instructions
    "MOV": (2, 2),
    "MOVP": (2, 2),
    "DMOV": (2, 2),
    "CMP": (3, 3),
    "CMPP": (3, 3),
    "ZCP": (4, 4),
    "ADD": (3, 3),
    "ADDP": (3, 3),
    "SUB": (3, 3),
    "SUBP": (3, 3),
    "MUL": (3, 3),
    "MULP": (3, 3),
    "DIV": (3, 3),
    "DIVP": (3, 3),
    "INC": (1, 1),
    "INCP": (1, 1),
    "DEC": (1, 1),
    "DECP": (1, 1),
    "ZRST": (2, 2),
    "alt": (1, 1),
    "ALT": (1, 1),
    "ALTP": (1, 1),
}

# Common LLM hallucinations mapped to valid Mitsubishi mnemonics
NEGATIVE_OPCODE_HINTS: Dict[str, str] = {
    "LDN": "LDI",
    "LDNOT": "LDI",
    "LD_NOT": "LDI",
    "!LD": "LDI",
    "ANDN": "ANI",
    "ANDNOT": "ANI",
    "AND_NOT": "ANI",
    "!AND": "ANI",
    "ORN": "ORI",
    "ORNOT": "ORI",
    "OR_NOT": "ORI",
    "!OR": "ORI",
    "TON": "OUT T<n> K<val>",
    "TOF": "OUT T<n> K<val>",
    "CTU": "OUT C<n> K<val>",
}

OCTAL_DEVICE_REGEX = re.compile(r"^([XY])(\d+)$", re.IGNORECASE)
VALID_DEVICE_REGEX = re.compile(
    r"^(X[0-9A-Fa-f]+|Y[0-9A-Fa-f]+|M\d+|S\d+|T\d+|C\d+|D\d+|V\d+|Z\d+|W[0-9A-Fa-f]+|R\d+|K-?\d+|H[0-9A-Fa-f]+)$",
    re.IGNORECASE,
)

BIT_DEVICES = {"X", "Y", "M", "S", "T", "C"}
WORD_DEVICES = {"D", "T", "C", "V", "Z", "W", "R"}
CONTACT_OPCODES = {
    "LD", "LDI", "LDP", "LDF", "AND", "ANI", "ANDP", "ANDF",
    "OR", "ORI", "ORP", "ORF",
}
COMPARISON_OPCODES = {
    "LD=", "LD>", "LD<", "LD<>", "LD<=", "LD>=",
    "AND=", "AND>", "AND<", "AND<>", "AND<=", "AND>=",
    "OR=", "OR>", "OR<", "OR<>", "OR<=", "OR>=",
}
DATA_OPCODES = {
    "MOV", "MOVP", "DMOV", "CMP", "CMPP", "ZCP", "ADD", "ADDP",
    "SUB", "SUBP", "MUL", "MULP", "DIV", "DIVP", "INC", "INCP",
    "DEC", "DECP", "ZRST", "ALT", "ALTP",
}


# ============================================================================
# 2. DATA MODELS
# ============================================================================

@dataclass
class RungSpec:
    """Represents a single ladder rung with an optional statement comment."""
    statement: str = ""
    instructions: List[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    """Holds deterministic validation and static safety analysis results."""
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    normalized_rungs: List[RungSpec] = field(default_factory=list)
    estimated_steps: int = 0
    devices_used: set = field(default_factory=set)


# ============================================================================
# 3. DETERMINISTIC MNEMONIC VALIDATOR & SAFETY ANALYZER
# ============================================================================

def calculate_instruction_steps(opcode: str, operands: List[str]) -> int:
    """Estimates Mitsubishi FX3U step count for a single instruction."""
    op = opcode.upper()
    if op in ("LD", "LDI", "AND", "ANI", "OR", "ORI", "ANB", "ORB", "MPS", "MRD", "MPP", "INV", "NOP", "END"):
        return 1
    if op in ("LDP", "LDF", "ANDP", "ANDF", "ORP", "ORF", "MEP", "MEF"):
        return 2
    if op == "OUT":
        if len(operands) == 2 and operands[0].upper().startswith("T"):
            return 3  # Timer OUT T0 K50 = 3 steps
        if len(operands) == 2 and operands[0].upper().startswith("C"):
            return 3  # Counter OUT C0 K10 = 3 steps
        if operands[0].upper().startswith("M") and int(operands[0][1:]) >= 1536:
            return 2
        return 1
    if op in ("SET", "RST"):
        if operands and operands[0].upper().startswith(("D", "T", "C")):
            return 2
        return 1
    if op in ("PLS", "PLF", "INC", "INCP", "DEC", "DECP", "ALT", "ALTP"):
        return 3
    if op.startswith(("LD=", "LD>", "LD<", "AND=", "AND>", "AND<", "OR=", "OR>", "OR<")):
        return 5
    if op in ("MOV", "MOVP", "ZRST"):
        return 5
    if op in ("CMP", "CMPP", "ADD", "ADDP", "SUB", "SUBP", "MUL", "MULP", "DIV", "DIVP"):
        return 7
    if op == "ZCP":
        return 9
    return 1


def _device_range_error(operand: str, profile: PLCProfile = PLC_PROFILES["FXCPU"]) -> Optional[str]:
    """Return a range error for the selected GX Works2 PLC family."""
    if not VALID_DEVICE_REGEX.fullmatch(operand):
        return f"Invalid device or constant syntax '{operand}'."
    m = re.fullmatch(r"([A-Z]+)([0-9A-F]+)", operand)
    if not m:
        return None
    prefix, raw_number = m.groups()
    if profile.x_y_base == 8 and prefix in ("X", "Y"):
        if any(digit in "89ABCDEFabcdef" for digit in raw_number):
            return f"Invalid octal address '{operand}' for {profile.name}."
    try:
        number = int(raw_number, profile.x_y_base if prefix in ("X", "Y") else 10)
    except ValueError:
        return f"Invalid {profile.name} device address '{operand}'."
    ranges = profile.ranges
    if prefix not in ranges:
        return f"Unsupported {profile.name} device '{operand}'."
    range_value = ranges[prefix]
    if range_value is None:
        if profile.m_segments and prefix == "M" and not any(low <= number <= high for low, high in profile.m_segments):
            return f"Device '{operand}' is outside the supported {profile.name} M-device ranges."
        return None
    low, high = range_value
    if not low <= number <= high:
        return f"Device '{operand}' is outside the {profile.name} range for {prefix} (0-{high})."
    return None


def _operand_kind(operand: str) -> str:
    if re.fullmatch(r"K-?\d+", operand):
        return "constant"
    if re.fullmatch(r"H[0-9A-F]+", operand):
        return "constant"
    m = re.fullmatch(r"([A-Z]+)[0-9A-F]+", operand)
    return m.group(1) if m else "invalid"


def _canonical_device(operand: str) -> str:
    match = re.fullmatch(r"([A-Z]+)([0-9A-F]+)", operand.upper())
    if not match:
        return operand.upper()
    prefix, digits = match.groups()
    return prefix + (digits.lstrip("0") or "0")


def _constant_range_error(operand: str) -> Optional[str]:
    if re.fullmatch(r"K-?\d+", operand):
        value = int(operand[1:])
        if not -32768 <= value <= 65535:
            return f"Decimal constant '{operand}' is outside the supported 16-bit range."
    elif re.fullmatch(r"H[0-9A-F]+", operand):
        if int(operand[1:], 16) > 0xFFFF:
            return f"Hex constant '{operand}' is outside the supported 16-bit range H0-HFFFF."
    return None


def _validate_comment_map(device_comments: Any, profile: PLCProfile = PLC_PROFILES["FXCPU"]) -> List[str]:
    errors: List[str] = []
    if device_comments is None:
        return errors
    if not isinstance(device_comments, dict):
        return ["device_comments must be an object mapping device names to text."]
    seen_devices = set()
    for key, value in device_comments.items():
        if not isinstance(key, str) or not isinstance(value, str):
            errors.append("Every device comment key and value must be a string.")
            continue
        dev = _canonical_device(key.strip())
        if _operand_kind(dev) in ("constant", "invalid") or not VALID_DEVICE_REGEX.fullmatch(dev) or _device_range_error(dev, profile):
            errors.append(f"Invalid or unsupported {profile.name} device comment address '{key}'.")
        if dev in seen_devices:
            errors.append(f"Duplicate device comment address after normalization: '{key}'.")
        seen_devices.add(dev)
        if not value.strip():
            errors.append(f"Comment for '{key}' must not be empty.")
        if len(value) > 32:
            errors.append(f"Comment for '{key}' exceeds the 32-character limit.")
        if any(unicodedata.category(ch) == "Cc" or ch in '\"\u2028\u2029' for ch in value):
            errors.append(f"Comment for '{key}' contains a CSV-breaking or unsupported character.")
    return errors


def validate_and_analyze_program(
    rungs_input: List[Dict[str, Any]],
    device_comments: Optional[Dict[str, str]] = None,
    auto_fix_negatives: bool = False,
    plc_type: str = "FX3U/FX3UC",
) -> ValidationReport:
    """
    Validates ladder mnemonics and runs static engineering checks.
    Replicates the strict validation shown in the video where negative opcodes
    (like LDN/ANDN) or duplicate timer coils are caught before touching GX Works2.
    """
    profile, profile_error = resolve_plc_profile(plc_type)
    report = ValidationReport()
    if profile_error:
        report.valid = False
        report.errors.append(profile_error)
        return report
    report.errors.extend(_validate_comment_map(device_comments, profile))
    if report.errors:
        report.valid = False
    out_coils_seen: Dict[str, int] = {}
    timer_coils_seen: Dict[str, int] = {}
    total_steps = 0

    if not isinstance(rungs_input, list) or not rungs_input:
        report.valid = False
        report.errors.append("Provide at least one rung as a list of rung objects.")
        return report

    for rung_idx, raw_rung in enumerate(rungs_input, start=1):
        if not isinstance(raw_rung, dict):
            report.valid = False
            report.errors.append(f"Rung {rung_idx} must be an object.")
            continue
        unknown_fields = set(raw_rung) - {"statement", "instructions"}
        if unknown_fields:
            report.valid = False
            report.errors.append(f"Rung {rung_idx} has unsupported fields: {', '.join(sorted(map(str, unknown_fields)))}.")
        raw_stmt = raw_rung.get("statement", "")
        if not isinstance(raw_stmt, str):
            report.valid = False
            report.errors.append(f"Rung {rung_idx} statement must be a string.")
            continue
        stmt = raw_stmt.strip()
        if stmt.startswith("//"):
            stmt = stmt[2:].strip()
        if len(stmt) > 64 or any(unicodedata.category(ch) == "Cc" or ch in '\"\u2028\u2029' for ch in stmt):
            report.valid = False
            report.errors.append(f"Rung {rung_idx} statement is too long or contains unsupported CSV/control characters.")

        raw_insts = raw_rung.get("instructions")
        if not isinstance(raw_insts, list) or not raw_insts:
            report.valid = False
            report.errors.append(f"Rung {rung_idx} instructions must be a non-empty list of strings.")
            continue

        norm_insts: List[str] = []
        rung_has_output = False
        for inst_idx, inst_line in enumerate(raw_insts, start=1):
            if not isinstance(inst_line, str):
                report.valid = False
                report.errors.append(f"Rung {rung_idx}, line {inst_idx}: instruction must be a string.")
                continue
            clean_line = inst_line.strip()
            if not clean_line or clean_line.startswith("//") or clean_line.startswith(";"):
                report.valid = False
                report.errors.append(f"Rung {rung_idx}, line {inst_idx}: blank lines and inline comments are not accepted as instructions.")
                continue
            tokens = clean_line.split()
            opcode = tokens[0].upper()
            operands = [token.upper() for token in tokens[1:]]
            if opcode in NEGATIVE_OPCODE_HINTS:
                suggestion = NEGATIVE_OPCODE_HINTS[opcode]
                if auto_fix_negatives and suggestion in ("LDI", "ANI", "ORI"):
                    opcode = suggestion
                    report.warnings.append(f"Rung {rung_idx}, line {inst_idx}: converted negative opcode to {opcode}.")
                else:
                    report.valid = False
                    report.errors.append(f"Rung {rung_idx}, line {inst_idx}: unsupported opcode {opcode}; use {suggestion}.")
                    continue
            if re.fullmatch(r"[TC]\d+", opcode) and len(operands) == 1:
                operands = [opcode, operands[0]]
                opcode = "OUT"
            if opcode not in SUPPORTED_OPCODES:
                report.valid = False
                report.errors.append(f"Rung {rung_idx}, line {inst_idx}: unsupported {profile.name} opcode '{opcode}'.")
                continue
            if opcode == "END":
                report.valid = False
                report.errors.append("Do not include END; GX Works2 adds the terminal END.")
                continue
            minimum, maximum = SUPPORTED_OPCODES[opcode]
            if not minimum <= len(operands) <= maximum:
                report.valid = False
                report.errors.append(f"Rung {rung_idx}, line {inst_idx}: {opcode} expects {minimum}..{maximum} operands, got {len(operands)}.")
                continue

            op_valid = True
            for operand in operands:
                kind = _operand_kind(operand)
                if kind == "invalid":
                    report.errors.append(f"Rung {rung_idx}, line {inst_idx}: invalid operand '{operand}'.")
                    op_valid = False
                elif kind == "constant" and _constant_range_error(operand):
                    report.errors.append(f"Rung {rung_idx}, line {inst_idx}: {_constant_range_error(operand)}")
                    op_valid = False
                elif kind not in ("constant",) and _device_range_error(operand, profile):
                    report.errors.append(f"Rung {rung_idx}, line {inst_idx}: {_device_range_error(operand, profile)}")
                    op_valid = False
                if kind not in ("constant", "invalid"):
                    report.devices_used.add(_canonical_device(operand))

            kinds = [_operand_kind(operand) for operand in operands]
            if opcode in CONTACT_OPCODES:
                if len(kinds) != 1 or kinds[0] not in BIT_DEVICES:
                    report.errors.append(f"{opcode} requires one bit device operand.")
                    op_valid = False
            elif opcode in COMPARISON_OPCODES and any(kind not in WORD_DEVICES | {"constant"} for kind in kinds):
                report.errors.append(f"{opcode} operands must be word devices or constants.")
                op_valid = False
            elif opcode == "OUT":
                target = kinds[0] if kinds else "invalid"
                if target in ("T", "C"):
                    if len(kinds) != 2 or kinds[1] not in ("D", "constant"):
                        report.errors.append("Timer/counter OUT requires a timer/counter device and K constant or D preset.")
                        op_valid = False
                    elif kinds[1] == "constant" and operands[1].startswith("K-"):
                        report.errors.append("Timer/counter presets cannot be negative.")
                        op_valid = False
                elif len(kinds) != 1 or target not in {"Y", "M", "S"}:
                    report.errors.append("OUT target must be Y, M, S, or a T/C device with preset.")
                    op_valid = False
            elif opcode in {"SET", "RST", "PLS", "PLF", "ALT", "ALTP"}:
                if len(kinds) != 1 or kinds[0] not in {"Y", "M", "S"}:
                    report.errors.append(f"{opcode} requires one Y, M, or S device.")
                    op_valid = False
            elif opcode in DATA_OPCODES:
                compare_dest = opcode in {"CMP", "CMPP", "ZCP"}
                if any(kind not in WORD_DEVICES | {"constant"} | ({"M"} if compare_dest and i == len(kinds) - 1 else set()) for i, kind in enumerate(kinds)):
                    report.errors.append(f"{opcode} operands must be word devices or constants.")
                    op_valid = False
                if opcode in {"MOV", "MOVP", "DMOV", "ADD", "ADDP", "SUB", "SUBP", "MUL", "MULP", "DIV", "DIVP", "INC", "INCP", "DEC", "DECP", "ZRST"} and kinds and kinds[-1] == "constant":
                    report.errors.append(f"{opcode} destination/range end must be a word device.")
                    op_valid = False
            else:
                # Complex branch/stack instructions are rejected until branch structure
                # is represented and round-tripped rather than flattened into CSV rows.
                report.errors.append(f"{opcode} is not supported by the safe linear-rung format.")
                op_valid = False

            if not op_valid:
                report.valid = False
                continue
            if opcode in CONTACT_OPCODES | COMPARISON_OPCODES and rung_has_output:
                report.valid = False
                report.errors.append(f"Rung {rung_idx}: contacts cannot follow an output/data instruction in the linear rung format.")
                continue
            if opcode in {"OUT", "SET", "RST", "PLS", "PLF"} | DATA_OPCODES:
                rung_has_output = True
            if opcode == "OUT":
                target = operands[0]
                if target.startswith(("T", "C")):
                    if target in timer_coils_seen:
                        report.valid = False
                        report.errors.append(f"Duplicate timer/counter coil '{target}' in rungs {timer_coils_seen[target]} and {rung_idx}.")
                    timer_coils_seen[target] = rung_idx
                else:
                    if target in out_coils_seen:
                        report.warnings.append(f"Duplicate OUT coil '{target}' in rungs {out_coils_seen[target]} and {rung_idx}.")
                    out_coils_seen[target] = rung_idx
            norm_insts.append(f"{opcode} {' '.join(operands)}".strip())
            total_steps += calculate_instruction_steps(opcode, operands)

        if not norm_insts:
            report.valid = False
            report.errors.append(f"Rung {rung_idx} contains no valid instructions.")
        elif not norm_insts[0].split()[0].startswith("LD"):
            report.valid = False
            report.errors.append(f"Rung {rung_idx} must begin with an LD-family instruction.")
        elif not rung_has_output:
            report.valid = False
            report.errors.append(f"Rung {rung_idx} must contain an output or data instruction.")
        else:
            report.normalized_rungs.append(RungSpec(statement=stmt, instructions=norm_insts))

    report.estimated_steps = total_steps + 1

    # 6. Static Interlock Check: Star-Delta Same-Scan Overlap Detection
    # Looks for two Y outputs driven by the same timer (one via ANI Tn, one via AND Tn)
    # without an intermediate dead-time transition timer.
    timer_nc_outputs: Dict[str, List[Tuple[int, str]]] = {}
    timer_no_outputs: Dict[str, List[Tuple[int, str]]] = {}

    for r_idx, rung in enumerate(report.normalized_rungs, start=1):
        out_y = None
        for inst in rung.instructions:
            parts = inst.split()
            if parts[0] == "OUT" and parts[1].startswith("Y"):
                out_y = parts[1]
        if out_y:
            for inst in rung.instructions:
                parts = inst.split()
                if parts[0] in ("ANI", "LDI") and parts[1].startswith("T"):
                    timer_nc_outputs.setdefault(parts[1], []).append((r_idx, out_y))
                elif parts[0] in ("AND", "LD") and parts[1].startswith("T"):
                    timer_no_outputs.setdefault(parts[1], []).append((r_idx, out_y))

    for t_dev, nc_list in timer_nc_outputs.items():
        if t_dev in timer_no_outputs:
            for r_nc, y_star in nc_list:
                for r_no, y_delta in timer_no_outputs[t_dev]:
                    if y_star != y_delta:
                        report.warnings.append(
                            f"Star-Delta Same-Scan Transition Warning: {y_star} (Rung {r_nc}) turns OFF and "
                            f"{y_delta} (Rung {r_no}) turns ON in the exact same scan cycle via {t_dev}. "
                            f"Consider adding a 0.2s (K2) transition delay timer before {y_delta} to prevent contactor arcing."
                        )

    # 7. Check for commented I/O devices that are never used in logic
    if device_comments:
        unused_commented = []
        for dev_key in device_comments:
            norm_key = _canonical_device(dev_key.strip())
            if norm_key not in report.devices_used:
                unused_commented.append(norm_key)
        if unused_commented:
            report.warnings.append(
                f"Commented devices not used in ladder logic: {', '.join(sorted(unused_commented))}."
            )

    return report


# ============================================================================
# 4. GX WORKS2 NATIVE CSV GENERATOR & PARSER (UTF-16LE + TAB DELIMITED)
# ============================================================================

def generate_gxworks2_ladder_csv(
    rungs: List[RungSpec],
    project_title: str = "(Untitled Project)",
    plc_type: str = "FX3U/FX3UC",
) -> str:
    """
    Formats normalized ladder rungs into Mitsubishi GX Works2's exact
    'Write to CSV File' / 'Read from CSV File' text layout.
    Note: GX Works2 expects UTF-16LE with BOM and Tab-separated quoted fields.
    """
    profile, profile_error = resolve_plc_profile(plc_type)
    if profile_error or profile is None:
        raise ValueError(profile_error or f"Unsupported plc_type '{plc_type}'.")
    lines: List[List[str]] = [
        [project_title],
        ["PLC Information:", f"{profile.csv_prefix} {plc_type}"],
        [
            "Step No.",
            "Line Statement",
            "Instruction",
            "I/O(Device)",
            "Blank",
            "PI Statement",
            "Note",
        ],
    ]

    step_no = 0
    for rung in rungs:
        # If the rung has a Line Statement (// comment), GX Works2 outputs a statement row
        if rung.statement:
            lines.append(["", rung.statement, "", "", "", "", ""])

        for inst in rung.instructions:
            parts = inst.split()
            opcode = parts[0].upper()
            operands = parts[1:]

            inst_steps = calculate_instruction_steps(opcode, operands)

            if not operands:
                lines.append([str(step_no), "", opcode, "", "", "", ""])
            elif len(operands) == 1:
                lines.append([str(step_no), "", opcode, operands[0], "", "", ""])
            else:
                # Multi-operand instructions (e.g., OUT T0 K50, DIV D10 K40 D30, CMP D30 K25 M20)
                # First operand goes on the instruction row; subsequent operands go on continuation rows
                lines.append([str(step_no), "", opcode, operands[0], "", "", ""])
                for extra_op in operands[1:]:
                    lines.append(["", "", "", extra_op, "", "", ""])

            step_no += inst_steps

    # Final END instruction
    lines.append([str(step_no), "", "END", "", "", "", ""])

    output_rows: List[str] = []
    for row in lines:
        quoted = [f'"{col.replace(chr(34), chr(34)*2)}"' for col in row]
        output_rows.append("\t".join(quoted))

    return "\r\n".join(output_rows) + "\r\n"


def save_utf16le_csv(content: str, filepath: Path) -> Path:
    """Atomically writes a UTF-16LE CSV, refusing to overwrite an existing artifact."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if filepath.exists():
        raise FileExistsError(f"Refusing to overwrite existing artifact: {filepath}")
    temporary = filepath.with_name(f".{filepath.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as f:
            f.write(b"\xff\xfe")
            f.write(content.encode("utf-16-le", errors="strict"))
            f.flush()
            os.fsync(f.fileno())
        os.rename(temporary, filepath)
    finally:
        if temporary.exists():
            temporary.unlink()
    return filepath


def new_artifact_path(prefix: str, suffix: str = ".csv") -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return WORKSPACE_DIR / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


def parse_gxworks2_ladder_csv(filepath: Path) -> Dict[str, Any]:
    """
    Reads a CSV file exported by GX Works2 ('Write to CSV File...') and
    extracts all rungs, line statements, instructions, and total step counts.
    """
    try:
        raw_bytes = filepath.read_bytes()
        if not raw_bytes:
            return {"error": "CSV export is empty."}
        if raw_bytes.startswith(b"\xff\xfe"):
            text = raw_bytes[2:].decode("utf-16-le", errors="strict")
        elif raw_bytes.startswith(b"\xfe\xff"):
            text = raw_bytes[2:].decode("utf-16-be", errors="strict")
        elif raw_bytes.startswith(b"\xef\xbb\xbf"):
            text = raw_bytes.decode("utf-8-sig", errors="strict")
        else:
            return {"error": "CSV export is missing a recognized BOM."}
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t", quotechar='"', strict=True))
    except FileNotFoundError:
        return {"error": f"CSV file not found: {filepath}"}
    except (OSError, UnicodeError, csv.Error) as exc:
        return {"error": f"CSV export could not be decoded or parsed: {exc}"}

    header_idx = next((i for i, row in enumerate(rows) if row and row[0].strip() == "Step No."), None)
    if header_idx is None:
        return {"error": f"Not a recognized GX Works2 ladder CSV: {filepath}"}
    expected_header = ["Step No.", "Line Statement", "Instruction", "I/O(Device)", "Blank", "PI Statement", "Note"]
    header = [field.strip() for field in rows[header_idx]]
    if header != expected_header:
        return {"error": "GX Works2 CSV header columns do not match the supported format."}
    plc_information = ""
    for row in rows[:header_idx]:
        if len(row) >= 2 and row[0].strip() == "PLC Information:":
            plc_information = row[1].strip()
            break
    if not plc_information:
        return {"error": "GX Works2 CSV is missing PLC Information."}

    items: List[Dict[str, Any]] = []
    statements: List[str] = []
    max_step = 0
    last_instruction_step: Optional[int] = None
    pending_statement_step: Optional[int] = None
    terminal_seen = False
    current: Optional[Dict[str, Any]] = None
    for row_num, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if len(row) != len(expected_header):
            return {"error": f"Malformed CSV row {row_num}: expected 7 columns, found {len(row)}."}
        step_col, stmt_col, opcode_col, device_col = (cell.strip() for cell in row[:4])
        if terminal_seen:
            if any(cell.strip() for cell in row):
                return {"error": f"Unexpected data after terminal END at row {row_num}."}
            continue
        if stmt_col and not opcode_col and not device_col:
            if (step_col and not step_col.isdecimal()) or any(cell.strip() for cell in row[4:]):
                return {"error": f"Malformed statement row {row_num}."}
            pending_statement_step = int(step_col) if step_col else None
            statements.append(stmt_col)
            items.append({"type": "statement", "statement": stmt_col})
            continue
        if opcode_col:
            if any(cell.strip() for cell in row[4:]):
                return {"error": f"Instruction row {row_num} uses unsupported CSV columns."}
            if current:
                items.append(current)
            if not step_col.isdecimal():
                return {"error": f"Instruction row {row_num} has an invalid step number."}
            step_num = int(step_col)
            if last_instruction_step is None and step_num != 0:
                return {"error": f"First instruction row {row_num} must start at step zero."}
            if last_instruction_step is not None and step_num <= last_instruction_step:
                return {"error": f"Instruction row {row_num} has a non-increasing step number."}
            if pending_statement_step is not None and step_num != pending_statement_step:
                return {"error": f"Statement row before line {row_num} does not match its instruction step."}
            pending_statement_step = None
            max_step = step_num
            last_instruction_step = step_num
            current = {"type": "instruction", "step": step_num, "opcode": opcode_col.upper(), "operands": [device_col] if device_col else []}
            if opcode_col.strip().upper() == "END":
                if device_col:
                    return {"error": "Terminal END instruction must not have an operand."}
                terminal_seen = True
        elif device_col and current is not None:
            if step_col or stmt_col or any(cell.strip() for cell in row[4:]):
                return {"error": f"Malformed continuation operand row {row_num}."}
            current["operands"].append(device_col)
        elif any(cell.strip() for cell in row):
            return {"error": f"Unrecognized non-empty row {row_num}."}
    if current:
        items.append(current)
    instructions = [item for item in items if item.get("type") == "instruction"]
    if not instructions or instructions[-1].get("opcode") != "END" or not terminal_seen:
        return {"error": "CSV export has no valid terminal END instruction."}
    for item in instructions:
        if item["opcode"] not in SUPPORTED_OPCODES:
            return {"error": f"Unsupported opcode in CSV export: {item['opcode']}"}
        minimum, maximum = SUPPORTED_OPCODES[item["opcode"]]
        if not minimum <= len(item["operands"]) <= maximum:
            return {"error": f"Malformed operand count in CSV export for {item['opcode']}."}

    rung_count = sum(1 for item in instructions if item["opcode"].startswith("LD") and item["opcode"] != "END")
    return {
        "file": str(filepath),
        "plc_information": plc_information,
        "rung_count": rung_count,
        "last_step_no": max_step,
        "statements": statements,
        "items": items,
    }


def plc_family_from_csv_info(plc_information: str) -> Optional[str]:
    """Map GX Works2's CSV PLC Information prefix to a supported family."""
    prefix = str(plc_information).strip().split(maxsplit=1)[0].upper() if plc_information else ""
    return {"FXCPU": "FXCPU", "QCPU": "QCPU", "LCPU": "LCPU"}.get(prefix)


def instruction_signature(items: List[Dict[str, Any]]) -> List[Tuple[str, Tuple[str, ...]]]:
    """Return ordered instruction/operand pairs for candidate/readback comparison."""
    def canonical_operand(value: Any) -> str:
        operand = str(value).strip().upper()
        if re.fullmatch(r"H[0-9A-F]+", operand):
            return "H" + (operand[1:].lstrip("0") or "0")
        match = re.match(r"^([A-Z]+)(0*)([0-9]+)$", operand)
        if not match:
            return operand
        prefix, _, digits = match.groups()
        # GX Works2 pads device numbers (X0 -> X000) when exporting. Decimal
        # devices/constants and hexadecimal constants compare without padding.
        normalized = digits.lstrip("0") or "0"
        return prefix + normalized

    return [
        (str(item["opcode"]).strip().upper(), tuple(canonical_operand(op) for op in item.get("operands", [])))
        for item in items
        if item.get("type") == "instruction"
    ]


def statement_signature(statements: List[str]) -> List[str]:
    """Normalize statement text without changing its order or meaning."""
    return [str(statement).strip().removeprefix("//").strip() for statement in statements]


def candidate_signatures(rungs: List[RungSpec]) -> Tuple[List[Tuple[str, Tuple[str, ...]]], List[str]]:
    instructions: List[Tuple[str, Tuple[str, ...]]] = []
    statements: List[str] = []
    for rung in rungs:
        if rung.statement:
            statements.append(rung.statement.strip().removeprefix("//").strip())
        for line in rung.instructions:
            tokens = line.split()
            if tokens:
                instructions.append((tokens[0].upper(), tuple(token.upper() for token in tokens[1:])))
    # GX Works2 CSV generator adds the terminal END instruction itself.
    instructions.append(("END", ()))
    return instructions, statements


def generate_gxworks2_comment_csv(comments: Dict[str, str], label_name: str = "COMMENT") -> str:
    """
    Generates a GX Works2 Device Comment CSV file (UTF-16LE, Tab-separated).
    Columns: "Device Name", "Comment"
    """
    rows = [
        [label_name],
        ["Device Name", "Comment"],
    ]
    for dev, comm in comments.items():
        rows.append([_canonical_device(dev.strip()), comm.strip()])

    output_rows = []
    for r in rows:
        quoted = [f'"{c.replace(chr(34), chr(34)*2)}"' for c in r]
        output_rows.append("\t".join(quoted))
    return "\r\n".join(output_rows) + "\r\n"


# ============================================================================
# 5. GX WORKS2 GUI AUTOMATION CONTROLLER (PYWINAUTO)
# ============================================================================

class GXWorks2Controller:
    """
    Automates a serialized, backup-first CSV workflow for GX Works2 FX3U/FX3UC MAIN.
    Keyboard entry is retained as an unused legacy helper and is not part of the write path.
    """

    def __init__(self) -> None:
        self.app: Optional[Any] = None
        self.main_win: Optional[Any] = None
        self.project_key: Optional[str] = None
        self._last_csv_confirmation: List[str] = []
        self.import_started = False

    def connect(self) -> Tuple[bool, str]:
        """Connects to an active GX Works2 window."""
        if not PYWINAUTO_AVAILABLE:
            return (
                False,
                "pywinauto is not installed or not running on Windows. Install with: pip install pywinauto",
            )
        try:
            self.app = Application(backend="win32").connect(
                class_name="GXW2FrameWnd",
                timeout=3.0,
            )
            self.main_win = self.app.window(class_name="GXW2FrameWnd")
            self.main_win.set_focus()
            return True, self.main_win.window_text()
        except Exception as exc:
            return (
                False,
                f"Could not attach to GX Works2 window (make sure GX Works2 is open with a project): {exc}",
            )

    def launch(self) -> Tuple[bool, str]:
        """Launch GX Works2 from an explicit path or common installation locations."""
        if not PYWINAUTO_AVAILABLE:
            return False, "pywinauto is not installed."
        candidates = []
        configured = os.environ.get(GXWORKS2_EXECUTABLE_ENV, "").strip()
        if configured:
            candidates.append(Path(configured))
        candidates.extend(Path(value) for value in (
            r"C:\Program Files (x86)\MELSOFT\GX Works2\GXW2.exe",
            r"C:\Program Files\MELSOFT\GX Works2\GXW2.exe",
        ))
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            return False, (
                f"GX Works2 was not found. Set the {GXWORKS2_EXECUTABLE_ENV} environment "
                "variable to GXW2.exe."
            )
        try:
            subprocess.Popen([str(executable)], close_fds=True)
            deadline = time.time() + 20.0
            while time.time() < deadline:
                ok, title = self.connect()
                if ok:
                    return True, title
                time.sleep(0.5)
            return False, "GX Works2 launched but its main window did not become available within 20 seconds."
        except (OSError, ValueError) as exc:
            return False, f"Could not launch GX Works2: {exc}"

    @staticmethod
    def _control_text(control: Any) -> str:
        try:
            return control.window_text().replace("&", "").strip()
        except Exception:
            return ""

    def create_project(self, project_name: str, plc_type: str) -> Tuple[bool, str]:
        """Create a new GX Works2 project and select a verified PLC model."""
        profile, profile_error = resolve_plc_profile(plc_type)
        if profile_error or profile is None:
            return False, profile_error or "Unsupported PLC type."
        if not project_name or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", project_name):
            return False, "project_name must be 1-64 characters using letters, numbers, spaces, _, ., or -."
        ok, title = self.connect()
        if not ok:
            ok, title = self.launch()
        if not ok:
            return False, title
        try:
            self.main_win.set_focus()
            send_keys("^n")
            deadline = time.time() + 8.0
            dialog = None
            while time.time() < deadline:
                candidates = [
                    window for window in self.app.windows(visible_only=True)
                    if window.handle != self.main_win.handle and window.is_enabled()
                ]
                dialog = next((window for window in candidates if window.class_name() == "#32770"), None)
                if dialog is not None:
                    break
                time.sleep(0.1)
            if dialog is None:
                return False, "GX Works2 did not open its New Project dialog."
            edits = [child for child in dialog.descendants() if child.friendly_class_name() == "Edit"]
            combos = [child for child in dialog.descendants() if child.friendly_class_name() == "ComboBox"]
            if not edits or not combos:
                return False, "New Project dialog did not expose project-name and PLC-type controls."
            name_edit = next((edit for edit in edits if "name" in self._control_text(edit).lower()), edits[0])
            name_edit.set_edit_text(project_name)
            combo = None
            for candidate in combos:
                try:
                    texts = [item.window_text().strip() for item in candidate.descendants()]
                except Exception:
                    texts = []
                if any(profile.csv_prefix in text.upper() or plc_type.upper() in text.upper() for text in texts):
                    combo = candidate
                    break
            combo = combo or combos[-1]
            combo.select(plc_type)
            selected = self._control_text(combo)
            if plc_type.upper() not in selected.upper() and profile.csv_prefix not in selected.upper():
                return False, f"GX Works2 did not confirm PLC selection '{plc_type}' (selected '{selected}')."
            ok_button = next((
                child for child in dialog.descendants()
                if child.friendly_class_name() == "Button"
                and self._control_text(child).lower() in ("ok", "create")
            ), None)
            if ok_button is None:
                return False, "New Project dialog has no OK/Create button."
            ok_button.click()
            time.sleep(1.0)
            self.project_key = None
            return True, f"Created project '{project_name}' for {plc_type}."
        except Exception as exc:
            return False, f"Could not create GX Works2 project: {exc}"

    @staticmethod
    def project_key_from_title(title: str) -> str:
        return title.split(" - [[", 1)[0].strip().casefold()

    def main_editor_is_active(self, expected_project: Optional[str] = None) -> Tuple[bool, str]:
        """Require the visible program editor to be MAIN before any ladder I/O."""
        if not self.main_win:
            return False, "GX Works2 is not connected."
        title = self.main_win.window_text()
        actual_project = self.project_key_from_title(title)
        if (
            re.search(r"\bMAIN\b", title, re.IGNORECASE)
            and "[PRG]" in title.upper()
            and (expected_project is None or actual_project == expected_project)
        ):
            return True, title
        if expected_project is not None and actual_project != expected_project:
            return False, f"GX Works2 project changed during transaction: {title}"
        return False, f"MAIN ladder is not the active editor. Active GX Works2 window: {title}"

    def _dismiss_popups(self, confirm: bool = True, timeout: float = 0.8) -> List[str]:
        """Detects and dismisses modal dialogs owned by the GX Works2 process."""
        messages: List[str] = []
        if not PYWINAUTO_AVAILABLE or not self.app:
            return messages
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                main_handle = self.main_win.handle if self.main_win else None
                dialogs = []
                for window in self.app.windows(visible_only=True):
                    if (window.handle == main_handle or window.class_name() != "#32770"
                            or not window.is_enabled()):
                        continue
                    buttons = [
                        child.window_text().replace("&", "").strip().lower()
                        for child in window.children()
                        if child.friendly_class_name() == "Button"
                    ]
                    # Do not treat the CSV Open/Save picker as a confirmation popup.
                    has_message_buttons = any(label in buttons for label in ("yes", "no", "ok"))
                    has_melsoft_title = "melsoft" in window.window_text().lower()
                    has_text = any(
                        child.friendly_class_name() == "Static" and child.window_text().strip()
                        for child in window.children()
                    )
                    if has_message_buttons and has_melsoft_title and has_text:
                        dialogs.append(window)
                    elif not buttons:
                        # GX Works2 briefly creates an empty owner dialog while
                        # transitioning between its frame and the native picker.
                        continue
                    elif any(label in buttons for label in ("open", "save", "cancel")):
                        # The native CSV picker is not a confirmation popup.
                        continue
                    else:
                        raise RuntimeError(
                            f"Unrecognized modal dialog '{window.window_text()}' with buttons {buttons}."
                        )
                if not dialogs:
                    break
                dlg = dialogs[-1]
                texts = [
                    child.window_text().strip()
                    for child in dlg.children()
                    if child.friendly_class_name() == "Static" and child.window_text().strip()
                ]
                if texts:
                    messages.extend(texts)
                if confirm:
                    yes = next((
                        child for child in dlg.children()
                        if child.friendly_class_name() == "Button"
                        and child.window_text().replace("&", "").strip().lower() == "yes"
                    ), None)
                    ok_button = next((
                        child for child in dlg.children()
                        if child.friendly_class_name() == "Button"
                        and child.window_text().replace("&", "").strip().lower() == "ok"
                    ), None)
                    if yes:
                        yes.click()
                    elif ok_button:
                        ok_button.click()
                    else:
                        raise RuntimeError(f"Unrecognized confirmation buttons in '{dlg.window_text()}': {buttons}")
                else:
                    dlg.set_focus()
                    send_keys("{ESC}")
                time.sleep(0.2)
            except Exception as exc:
                raise RuntimeError(f"Could not safely identify or dismiss a GX Works2 dialog: {exc}") from exc
        return messages

    def _open_csv_dialog(self, command: int, title_regex: str) -> Any:
        """Invoke a native GX Works2 CSV command and wait for its file dialog."""
        if not self.main_win:
            raise RuntimeError("GX Works2 main window is not connected")
        is_main, active_msg = self.main_editor_is_active(self.project_key)
        if not is_main:
            raise RuntimeError(active_msg)

        self.main_win.set_focus()
        # Posting returns immediately. Sending synchronously deadlocks here because
        # GX Works2 opens a modal confirmation while processing WM_COMMAND and waits
        # for the user before the caller can reach the popup handler below.
        self.main_win.post_message(GX_WM_COMMAND, command, 0)
        self._last_csv_confirmation = []
        deadline = time.time() + GX_CSV_DIALOG_TIMEOUT
        while time.time() < deadline:
            # GX Works2 asks for confirmation before it opens the CSV file picker.
            # Handle that modal in this wait loop or the expected picker never appears.
            self._last_csv_confirmation.extend(self._dismiss_popups(confirm=True, timeout=0.15))
            for dialog in self.app.windows(title_re=title_regex):
                if dialog.is_visible() and dialog.is_enabled():
                    return self.app.window(handle=dialog.handle)
            time.sleep(0.1)
        raise RuntimeError(f"GX Works2 did not open the expected CSV dialog: {title_regex}")

    @staticmethod
    def _set_file_dialog_path(dialog: Any, path: Path, button_title: str) -> None:
        filename = dialog.child_window(control_id=1148, class_name="Edit")
        filename.set_edit_text(str(path))
        actual = filename.window_text().strip()
        if actual != str(path):
            raise RuntimeError(f"File dialog path did not match requested path: '{actual}'.")
        dialog.child_window(title=button_title, class_name="Button").click()

    def focus_main_ladder(self) -> None:
        """Brings the MAIN ladder editor window into focus and switches to Write mode (F2)."""
        if not self.main_win:
            return
        self.main_win.set_focus()
        time.sleep(0.15)
        # Ensure we are in Write Mode (F2 in GX Works2 switches to Write Mode)
        send_keys("{F2}")
        time.sleep(0.15)
        self._dismiss_popups(confirm=True, timeout=0.3)

    def export_current_csv(self, output_path: Path, require_main: bool = True) -> Tuple[bool, str]:
        """
        Triggers 'Edit -> Write to CSV File...' in GX Works2 and saves the
        currently active editor (MAIN ladder or Device Comment) to output_path.
        Replicates the exact verification step seen at 00:21, 00:54, 02:29 in the video.
        """
        ok, msg = self.connect()
        if not ok:
            return False, msg
        if require_main:
            is_main, active_msg = self.main_editor_is_active(self.project_key)
            if not is_main:
                return False, active_msg

        try:
            if output_path.exists():
                return False, f"Refusing to overwrite existing CSV artifact: {output_path}"

            self.main_win.set_focus()
            time.sleep(0.15)

            dialog = self._open_csv_dialog(GX_WRITE_CSV_COMMAND, r".*Write to CSV File.*")
            self._set_file_dialog_path(dialog, output_path, "&Save")
            close_deadline = time.time() + 2.0
            while time.time() < close_deadline and dialog.is_visible():
                time.sleep(0.1)
            if dialog.is_visible():
                return False, "CSV Save dialog remained visible after selecting the destination."
            deadline = time.time() + GX_CSV_OPERATION_TIMEOUT
            previous_size = -1
            stable_observations = 0
            while time.time() < deadline:
                self._dismiss_popups(confirm=False, timeout=0.1)
                if output_path.exists() and output_path.stat().st_size > 0:
                    size = output_path.stat().st_size
                    stable_observations = stable_observations + 1 if size == previous_size else 0
                    previous_size = size
                    if stable_observations >= 2:
                        is_main, active_msg = self.main_editor_is_active(self.project_key)
                        if require_main and not is_main:
                            return False, active_msg
                        return True, str(output_path)
                time.sleep(0.2)

            return False, f"CSV export timed out; file was not created at {output_path}"
        except Exception as exc:
            return False, f"Failed during Write to CSV File automation: {exc}"

    def import_csv_into_active_editor(self, csv_path: Path) -> Tuple[bool, str]:
        """
        Triggers 'Edit -> Read from CSV File...' in GX Works2 to load a
        UTF-16LE Tab-delimited CSV file directly into the active Ladder or Comment editor.
        """
        self.import_started = False
        ok, msg = self.connect()
        if not ok:
            return False, msg

        is_main, active_msg = self.main_editor_is_active()
        if not is_main:
            return False, active_msg

        if not csv_path.exists():
            return False, f"CSV file does not exist: {csv_path}"
        try:
            source = csv_path.read_bytes()
            if not source.startswith(b"\xff\xfe") or not source[2:]:
                return False, "Import CSV must be a non-empty UTF-16LE file with BOM."
            source[2:].decode("utf-16-le", errors="strict")
        except (OSError, UnicodeError) as exc:
            return False, f"Import CSV failed preflight: {exc}"

        try:
            self.main_win.set_focus()
            time.sleep(0.15)

            dialog = self._open_csv_dialog(GX_READ_CSV_COMMAND, r".*Read from CSV File.*")
            is_main, active_msg = self.main_editor_is_active(self.project_key)
            if not is_main:
                return False, f"Target project/editor changed before CSV selection: {active_msg}"
            self.import_started = True
            self._set_file_dialog_path(dialog, csv_path, "&Open")
            if dialog.is_visible():
                deadline = time.time() + 2.0
                while time.time() < deadline and dialog.is_visible():
                    time.sleep(0.1)
                if dialog.is_visible():
                    return False, "CSV Open dialog remained visible after selecting the candidate."
            popups = self._dismiss_popups(confirm=True, timeout=1.5)
            popup_text = " ".join(popups).lower()
            failure_terms = ("error", "failed", "cannot", "unable", "invalid", "not supported")
            if any(term in popup_text for term in failure_terms):
                return False, f"GX Works2 reported an import problem: {' | '.join(popups)}"
            is_main, active_msg = self.main_editor_is_active(self.project_key)
            if not is_main:
                return False, f"Active project/editor changed during import: {active_msg}"
            return True, f"GX Works2 accepted the CSV selection for {csv_path.name}; readback verification is still required. Dialog notes: {popups}"
        except Exception as exc:
            return False, f"Failed during Read from CSV File automation: {exc}"

    def compile_build(self) -> Tuple[bool, List[str]]:
        """Triggers 'Compile -> Build (F4)' in GX Works2 and captures any popup messages."""
        ok, msg = self.connect()
        if not ok:
            return False, [msg]
        is_main, active_msg = self.main_editor_is_active(self.project_key)
        if not is_main:
            return False, [active_msg]

        try:
            self.main_win.set_focus()
            time.sleep(0.15)
            try:
                self.main_win.menu_select("Compile->Build")
            except Exception:
                send_keys("{F4}")
            time.sleep(0.6)
            popups = self._dismiss_popups(confirm=True, timeout=3.0)
            popup_text = " ".join(popups).lower()
            failure_terms = ("error", "failed", "cannot", "unable", "invalid", "not supported")
            if any(term in popup_text for term in failure_terms):
                return False, popups
            if not re.search(r"\b(build|compile)\b.*\b(complete|completed|success|successful)\b|\b(complete|completed|success|successful)\b.*\b(build|compile)\b", popup_text):
                return False, popups + ["No explicit GX Works2 build-success result was observed."]
            is_main, active_msg = self.main_editor_is_active(self.project_key)
            if not is_main:
                return False, popups + [active_msg]
            return True, popups
        except Exception as exc:
            return False, [f"Compile Build failed: {exc}"]

    def run_check_program(self) -> Dict[str, Any]:
        """
        Triggers 'Tool -> Check Program...', clicks Execute, reads any reported
        errors/warnings from the dialog list view, and closes the dialog.
        Replicates the verification at 03:00 in the video.
        """
        ok, msg = self.connect()
        if not ok:
            return {"passed": False, "error": msg, "findings": []}
        is_main, active_msg = self.main_editor_is_active(self.project_key)
        if not is_main:
            return {"passed": False, "error": active_msg, "findings": []}

        try:
            self.main_win.set_focus()
            time.sleep(0.15)
            # GX Works2's XTP menu is owner-drawn and is not exposed to
            # pywinauto menu_select. Tool opens with the first row selected;
            # one Down selects "Check Program..." in the supported GX Works2 UI.
            send_keys("%t")
            time.sleep(0.15)
            send_keys("{DOWN}")
            send_keys("{ENTER}")
            time.sleep(0.5)

            chk_dlg = self.app.window(title_re=r".*Check Program.*")
            if chk_dlg.exists(timeout=2.0):
                chk_dlg.set_focus()
                execute = next((
                    child for child in chk_dlg.descendants()
                    if child.friendly_class_name() == "Button"
                    and "execute" in child.window_text().replace("&", "").lower()
                ), None)
                if not execute:
                    send_keys("{ESC}")
                    return {"passed": False, "error": "Check Program dialog has no Execute button.", "findings": []}
                execute.click()
                time.sleep(0.6)

                # Dismiss "Checking program completed." info box
                info_msgs = self._dismiss_popups(confirm=True, timeout=1.5)

                # Inspect ListView inside Check Program dialog for any syntax/duplicate errors
                findings: List[str] = []
                try:
                    for child in chk_dlg.descendants():
                        if child.friendly_class_name() == "ListView":
                            for item in child.items():
                                txt = item.text().strip()
                                if txt:
                                    findings.append(txt)
                        elif child.friendly_class_name() in ("Static", "Edit"):
                            txt = child.window_text().strip()
                            if txt and re.search(r"\b(error|warning)\s*:\s*[1-9]\d*", txt, re.IGNORECASE):
                                findings.append(txt)
                except Exception:
                    pass

                # Close Check Program dialog
                send_keys("{ESC}")
                time.sleep(0.2)

                popup_failures = [
                    message for message in info_msgs
                    if re.search(r"\b(error|failed|unable|cannot)\b", message, re.IGNORECASE)
                    and not re.search(r"\b(error|warning)\s*:\s*0\b|\bno errors? found\b", message, re.IGNORECASE)
                ]
                findings.extend(popup_failures)
                findings = [
                    finding for finding in findings
                    if not re.search(r"\bno errors? found\b|\berrors?\s*[:=]\s*0\b", finding, re.IGNORECASE)
                ]

                combined_text = " ".join(info_msgs + findings).lower()
                explicit_success = bool(
                    re.search(r"\bno errors? found\b", combined_text)
                    or re.search(r"\berrors?\s*[:=]\s*0\b", combined_text)
                )
                if not explicit_success:
                    findings.append("Program Check did not expose an explicit zero-error success result.")

                return {
                    "passed": explicit_success and len(findings) == 0,
                    "dialog_messages": info_msgs,
                    "findings": findings,
                }
            return {"passed": False, "error": "GX Works2 did not open the Check Program dialog.", "findings": []}
        except Exception as exc:
            return {"passed": False, "error": str(exc), "findings": []}


# ============================================================================
# 6. FASTMCP SERVER DEFINITION & TOOLS (FOR ANTIGRAVITY & VS CODE)
# ============================================================================

mcp = FastMCP(
    "gxworks2-mcp",
    instructions=(
        "GX Works2 offline ladder editing for FXCPU, QCPU, and LCPU families. Validate first, write only "
        "with MAIN active, and accept SUCCESS only after backup, build, Program Check, "
        "and exact readback verification. Never transfers to a PLC."
    ),
)

controller = GXWorks2Controller()


def serialized_gui(function):
    """Keep GUI stateful actions from interleaving across concurrent MCP calls."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with GUI_TRANSACTION_LOCK:
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                payload: Dict[str, Any] = {
                    "status": "GUI_OPERATION_FAILED",
                    "error": f"Unexpected {function.__name__} failure: {exc}",
                }
                if function.__name__ == "gx_write_program":
                    if GUI_ACTIVE_WRITE.get("import_attempted") or getattr(controller, "import_started", False):
                        rollback = GUI_ACTIVE_WRITE.get("rollback")
                        try:
                            payload["status"] = "WRITE_EXCEPTION"
                            payload["backup_csv"] = GUI_ACTIVE_WRITE.get("backup_csv")
                            payload["rollback"] = rollback() if rollback else {"restored": False, "error": "Rollback handler was unavailable."}
                        except Exception as rollback_exc:
                            payload["rollback"] = {"restored": False, "error": str(rollback_exc)}
                    else:
                        payload["status"] = "REJECTED_BEFORE_WRITE"
                else:
                    payload["success"] = False
                return json.dumps(payload, indent=2)
            finally:
                if function.__name__ == "gx_write_program":
                    GUI_ACTIVE_WRITE.clear()
    return wrapper


@mcp.tool()
@serialized_gui
def gx_get_status() -> str:
    """
    Checks whether Mitsubishi GX Works2 is running on Windows, returns the active
    window title, and lists the backup/verification workspace directory.
    """
    ok, title_or_err = controller.connect()
    main_active, _ = controller.main_editor_is_active() if ok else (False, "")
    if ok:
        controller.project_key = controller.project_key_from_title(title_or_err)
    recent_files = sorted(
        [f.name for f in WORKSPACE_DIR.glob("*.csv")],
        reverse=True,
    )[:10]
    return json.dumps(
        {
            "gx_works2_connected": ok,
            "main_editor_active": main_active,
            "window_info": title_or_err,
            "pywinauto_available": PYWINAUTO_AVAILABLE,
            "supported_plc_families": sorted(PLC_PROFILES),
            "workspace_dir": str(WORKSPACE_DIR),
            "recent_csv_artifacts": recent_files,
        },
        indent=2,
    )


@mcp.tool()
def gx_list_supported_plc_types() -> str:
    """Lists supported GX Works2 PLC families, aliases, and address conventions."""
    return json.dumps(
        {
            "families": [
                {
                    "family": key,
                    "csv_prefix": profile.csv_prefix,
                    "addressing": "octal X/Y" if profile.x_y_base == 8 else "hexadecimal X/Y/W",
                    "aliases": sorted(
                        alias for alias, family in PLC_TYPE_ALIASES.items() if family == key
                    ),
                }
                for key, profile in sorted(PLC_PROFILES.items())
            ],
            "note": (
                "Family profiles use safe GX Works2 envelopes. The native Build and "
                "Program Check remain authoritative for model-specific limits."
            ),
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def gx_create_project(project_name: str, plc_type: str) -> str:
    """
    Launches GX Works2 if necessary and creates a new project with the requested
    FXCPU, QCPU, or LCPU model selected in the native New Project dialog.
    """
    ok, message = controller.create_project(project_name, plc_type)
    if ok:
        return json.dumps(
            {
                "success": True,
                "project_name": project_name,
                "plc_type": plc_type,
                "message": message,
            },
            indent=2,
        )
    return json.dumps(
        {"success": False, "error": message, "plc_type": plc_type},
        indent=2,
    )


@mcp.tool()
@serialized_gui
def gx_read_program() -> str:
    """
    Exports the currently displayed MAIN ladder program from GX Works2 via
    'Edit -> Write to CSV File...' and parses all rungs, line statements,
    instructions, and step numbers.
    """
    ok, title = controller.connect()
    if not ok:
        return json.dumps({"success": False, "error": title}, indent=2)
    is_main, active_msg = controller.main_editor_is_active()
    if not is_main:
        return json.dumps({"success": False, "error": active_msg}, indent=2)
    controller.project_key = controller.project_key_from_title(title)
    csv_path = new_artifact_path("read-MAIN")

    ok, msg = controller.export_current_csv(csv_path)
    if not ok:
        return json.dumps({"success": False, "error": msg}, indent=2)

    parsed = parse_gxworks2_ladder_csv(csv_path)
    if parsed.get("error"):
        parsed["success"] = False
        return json.dumps(parsed, indent=2)
    parsed["success"] = True
    return json.dumps(parsed, indent=2)


@mcp.tool()
def gx_validate_program(
    rungs: List[Dict[str, Any]],
    device_comments: Optional[Dict[str, str]] = None,
    auto_fix_negatives: bool = False,
    plc_type: str = "FX3U/FX3UC",
) -> str:
    """
    Pre-validates the conservative family-aware linear-rung subset without modifying GX Works2.
    Rejects unsupported instruction structures, malformed operands, out-of-range devices,
    malformed comments, invalid octal addresses, and duplicate timer/counter coils.

    Args:
        rungs: List of rung objects, each with {"statement": "optional rung title", "instructions": ["LD X0", "OUT Y0"]}.
        device_comments: Optional dictionary mapping devices (e.g., {"X0": "START", "Y0": "MAIN CONTACTOR"}) to comments.
        plc_type: FXCPU/QCPU/LCPU family or a supported model name.
        auto_fix_negatives: If True, automatically converts LDN/ANDN/ORN to LDI/ANI/ORI.
    """
    if not isinstance(auto_fix_negatives, bool):
        return json.dumps({"valid": False, "errors": ["auto_fix_negatives must be a boolean."], "warnings": []}, indent=2)
    report = validate_and_analyze_program(
        rungs, device_comments, auto_fix_negatives=auto_fix_negatives, plc_type=plc_type
    )
    return json.dumps(
        {
            "valid": report.valid,
            "estimated_steps": report.estimated_steps,
            "rung_count": len(report.normalized_rungs),
            "errors": report.errors,
            "warnings": report.warnings,
            "devices_used": sorted(report.devices_used),
            "normalized_rungs": [
                {"statement": r.statement, "instructions": r.instructions}
                for r in report.normalized_rungs
            ],
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def gx_write_program(
    rungs: List[Dict[str, Any]],
    device_comments: Optional[Dict[str, str]] = None,
    delivery_mode: str = "csv_import",
    plc_type: str = "FX3U/FX3UC",
    run_compile_and_check: bool = True,
) -> str:
    """
    Validates, backs up, imports, builds, checks, and reads back a ladder in MAIN.
    Invalid candidates are rejected before GUI access. SUCCESS requires verified round-trip equality.

    Args:
        rungs: Ordered rung objects with optional statement and instruction list, e.g. [{"statement": "Motor start/stop", "instructions": ["LD X0", "OR M0", "ANI X1", "OUT M0"]}].
        device_comments: Optional dict of I/O comments, e.g. {"X0": "START", "X1": "STOP NO", "Y0": "MAIN CONTACTOR"}.
        delivery_mode: Must be "csv_import". Simulated keystroke entry is not used for writes.
        plc_type: Target GX Works2 PLC family/model for CSV header.
        run_compile_and_check: Must be True; a write is not successful without GX Works2 build/check.
    """
    controller.import_started = False
    GUI_ACTIVE_WRITE.clear()
    if not isinstance(delivery_mode, str) or delivery_mode.lower() != "csv_import":
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": "Only delivery_mode='csv_import' is supported for reliable writes."}, indent=2)
    if run_compile_and_check is not True:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": "Build and program check are required; run_compile_and_check must be True."}, indent=2)
    profile, profile_error = resolve_plc_profile(plc_type)
    if profile_error or profile is None:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": profile_error}, indent=2)

    # 1. Pre-flight deterministic validation (rejects bad opcodes before touching GX Works2)
    report = validate_and_analyze_program(
        rungs, device_comments, auto_fix_negatives=False, plc_type=plc_type
    )
    if not report.valid:
        return json.dumps(
            {
                "status": "REJECTED_BEFORE_WRITE",
                "message": "Program rejected before modifying ladder or comments due to validation errors.",
                "errors": report.errors,
                "warnings": report.warnings,
            },
            indent=2,
        )

    # 2. Always generate local UTF-16LE CSV artifacts in ~/Documents/Codex-Gx
    candidate_csv_content = generate_gxworks2_ladder_csv(
        report.normalized_rungs,
        project_title="(Untitled Project)",
        plc_type=plc_type,
    )
    after_csv_path = save_utf16le_csv(
        candidate_csv_content,
        new_artifact_path("main-after-MAIN"),
    )
    candidate_parsed = parse_gxworks2_ladder_csv(after_csv_path)
    expected_instructions, expected_statements = candidate_signatures(report.normalized_rungs)
    if (
        candidate_parsed.get("error")
        or instruction_signature(candidate_parsed.get("items", [])) != expected_instructions
        or statement_signature(candidate_parsed.get("statements", [])) != expected_statements
    ):
        return json.dumps(
            {
                "status": "CANDIDATE_FORMAT_FAILED",
                "error": candidate_parsed.get("error", "Generated CSV does not preserve the validated ladder."),
                "ladder_csv_path": str(after_csv_path),
            },
            indent=2,
        )

    comment_csv_path: Optional[Path] = None
    if device_comments:
        comment_csv_content = generate_gxworks2_comment_csv(device_comments)
        comment_csv_path = save_utf16le_csv(
            comment_csv_content,
            new_artifact_path("comments-backup"),
        )

    # 3. Connect to GX Works2 and perform backup + delivery
    ok, conn_msg = controller.connect()
    if not ok:
        return json.dumps(
            {
                "status": "OFFLINE_CSV_GENERATED",
                "message": (
                    "Validated program and generated GX Works2 UTF-16LE CSV files locally, "
                    f"but GX Works2 GUI was not reachable: {conn_msg}"
                ),
                "rung_count": len(report.normalized_rungs),
                "estimated_steps": report.estimated_steps,
                "warnings": report.warnings,
                "ladder_csv_path": str(after_csv_path),
                "comment_csv_path": str(comment_csv_path) if comment_csv_path else None,
            },
            indent=2,
        )

    is_main, active_msg = controller.main_editor_is_active()
    if not is_main:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "error": active_msg, "ladder_csv_path": str(after_csv_path)}, indent=2)
    project_key = controller.project_key_from_title(conn_msg)
    controller.project_key = project_key

    # The original program must be safely exported and parseable before importing.
    before_csv_path = new_artifact_path("main-before-MAIN")
    backup_ok, backup_msg = controller.export_current_csv(before_csv_path)
    if not backup_ok:
        return json.dumps({"status": "BACKUP_FAILED", "error": backup_msg, "ladder_csv_path": str(after_csv_path)}, indent=2)
    backup_parsed = parse_gxworks2_ladder_csv(before_csv_path)
    backup_signature = instruction_signature(backup_parsed.get("items", []))
    if backup_parsed.get("error") or not backup_signature:
        return json.dumps({
            "status": "BACKUP_FAILED",
            "error": backup_parsed.get("error", "Backup CSV contains no parseable ladder instructions."),
            "backup_csv": str(before_csv_path),
        }, indent=2)
    active_family = plc_family_from_csv_info(backup_parsed.get("plc_information", ""))
    requested_family = PLC_TYPE_ALIASES.get(
        re.sub(r"\s+", "", plc_type.strip().upper()),
        "FXCPU" if plc_type.strip().upper().startswith("FX")
        else "QCPU" if plc_type.strip().upper().startswith("Q")
        else "LCPU" if plc_type.strip().upper().startswith("L")
        else None,
    )
    if active_family is None:
        return json.dumps({
            "status": "REJECTED_BEFORE_WRITE",
            "error": f"Active project CSV reported an unsupported PLC family: {backup_parsed.get('plc_information')}",
            "backup_csv": str(before_csv_path),
        }, indent=2)
    if active_family != requested_family:
        return json.dumps({
            "status": "PLC_TYPE_MISMATCH",
            "error": (
                f"Active project is {active_family}, but plc_type='{plc_type}' "
                f"requests {requested_family}. Create/open the correct project first."
            ),
            "backup_csv": str(before_csv_path),
        }, indent=2)

    def restore_backup() -> Dict[str, Any]:
        restored, restore_msg = controller.import_csv_into_active_editor(before_csv_path)
        if not restored:
            return {"restored": False, "error": restore_msg}
        restore_build_ok, restore_build_messages = controller.compile_build()
        if not restore_build_ok:
            return {"restored": False, "error": "Backup reimported, but GX Works2 build failed.", "build_messages": restore_build_messages}
        restore_check = controller.run_check_program()
        if not restore_check.get("passed"):
            return {"restored": False, "error": "Backup reimported, but GX Works2 program check failed.", "check_program": restore_check}
        restored_csv = new_artifact_path("restore-verify-MAIN")
        export_ok, export_msg = controller.export_current_csv(restored_csv)
        if not export_ok:
            return {"restored": False, "error": f"Could not export restored MAIN: {export_msg}"}
        restored_parsed = parse_gxworks2_ladder_csv(restored_csv)
        same_program = (
            not restored_parsed.get("error")
            and backup_signature == instruction_signature(restored_parsed.get("items", []))
            and statement_signature(backup_parsed.get("statements", [])) == statement_signature(restored_parsed.get("statements", []))
        )
        return {
            "restored": same_program,
            "verify_csv": str(restored_csv),
            **({} if same_program else {"error": "Restored MAIN does not match the saved backup."}),
        }

    # Once import is attempted, any failure may leave MAIN partially changed.
    GUI_ACTIVE_WRITE.update({"backup_csv": str(before_csv_path), "rollback": restore_backup, "import_attempted": False})
    import_ok, import_msg = controller.import_csv_into_active_editor(after_csv_path)
    GUI_ACTIVE_WRITE["import_attempted"] = bool(getattr(controller, "import_started", import_ok))
    if not import_ok:
        if not GUI_ACTIVE_WRITE["import_attempted"]:
            return json.dumps({"status": "IMPORT_NOT_STARTED", "error": import_msg, "backup_csv": str(before_csv_path)}, indent=2)
        return json.dumps({"status": "WRITE_FAILED", "error": import_msg, "backup_csv": str(before_csv_path), "rollback": restore_backup()}, indent=2)

    build_ok, compile_messages = controller.compile_build()
    if not build_ok:
        return json.dumps({"status": "BUILD_FAILED", "error": compile_messages, "backup_csv": str(before_csv_path), "rollback": restore_backup()}, indent=2)

    check_result = controller.run_check_program()
    if not check_result.get("passed"):
        return json.dumps({"status": "PROGRAM_CHECK_FAILED", "check_program": check_result, "backup_csv": str(before_csv_path), "rollback": restore_backup()}, indent=2)

    verify_csv_path = new_artifact_path("verify-MAIN")
    verify_ok, verify_msg = controller.export_current_csv(verify_csv_path)
    if not verify_ok:
        return json.dumps({"status": "READBACK_FAILED", "error": verify_msg, "backup_csv": str(before_csv_path), "rollback": restore_backup()}, indent=2)

    verified_summary = parse_gxworks2_ladder_csv(verify_csv_path)
    actual_instructions = instruction_signature(verified_summary.get("items", []))
    actual_statements = statement_signature(verified_summary.get("statements", []))
    if verified_summary.get("error") or actual_instructions != expected_instructions or actual_statements != expected_statements:
        mismatch = {
            "error": verified_summary.get("error", "GX Works2 readback differs from the candidate program."),
            "expected_instruction_count": len(expected_instructions),
            "actual_instruction_count": len(actual_instructions),
            "expected_statements": expected_statements,
            "actual_statements": actual_statements,
        }
        return json.dumps({"status": "READBACK_MISMATCH", "verification": mismatch, "backup_csv": str(before_csv_path), "rollback": restore_backup()}, indent=2)

    return json.dumps(
        {
            "status": "SUCCESS",
            "write_message": import_msg,
            "rung_count": len(report.normalized_rungs),
            "verified_step_count": verified_summary.get("last_step_no"),
            "check_program": check_result,
            "compile_messages": compile_messages,
            "engineering_warnings": report.warnings,
            "artifacts": {
                "before_csv": str(before_csv_path),
                "after_csv": str(after_csv_path),
                "verify_csv": str(verify_csv_path),
                "comment_csv": str(comment_csv_path) if comment_csv_path else None,
            },
        },
        indent=2,
    )

@mcp.tool()
def gx_write_device_comments(
    device_comments: Dict[str, str],
    import_via_gui: bool = False,
) -> str:
    """
    Saves a validated GX Works2 Device Comment CSV artifact (UTF-16LE, tab-delimited).

    Args:
        device_comments: Mapping of device names to comments (max 32 chars recommended),
                         e.g. {"X0": "START", "X1": "STOP NO", "Y0": "KONTAKTOR MAIN"}.
        import_via_gui: Must be False; automatic Device Comment GUI import is unsupported.
    """
    if not isinstance(import_via_gui, bool):
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": ["import_via_gui must be a boolean."]}, indent=2)
    if not isinstance(device_comments, dict) or not device_comments:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": ["device_comments must be a non-empty object."]}, indent=2)
    errors = _validate_comment_map(device_comments)
    if errors:
        return json.dumps({"status": "REJECTED_BEFORE_WRITE", "errors": errors}, indent=2)
    content = generate_gxworks2_comment_csv(device_comments)
    csv_path = save_utf16le_csv(content, new_artifact_path("comments"))

    gui_status = "Saved CSV artifact only; import it from the GX Works2 Device Comment editor."
    if import_via_gui:
        return json.dumps(
            {
                "status": "GUI_IMPORT_UNSUPPORTED",
                "comment_count": len(device_comments),
                "comment_csv_path": str(csv_path),
                "error": (
                    "Automatic Device Comment import is not supported safely yet. "
                    "The CSV was saved; import it manually from the Device Comment editor."
                ),
            },
            indent=2,
        )

    return json.dumps(
        {
            "status": "SUCCESS",
            "comment_count": len(device_comments),
            "comment_csv_path": str(csv_path),
            "gui_status": gui_status,
        },
        indent=2,
    )


@mcp.tool()
@serialized_gui
def gx_compile_and_check() -> str:
    """
    Triggers 'Compile -> Build (F4)' and 'Tool -> Check Program...' in GX Works2
    and returns the compilation and program check status.
    """
    ok, title = controller.connect()
    if not ok:
        return json.dumps({"success": False, "error": title}, indent=2)
    is_main, active_msg = controller.main_editor_is_active()
    if not is_main:
        return json.dumps({"success": False, "error": active_msg}, indent=2)
    controller.project_key = controller.project_key_from_title(title)
    build_ok, popups = controller.compile_build()
    if not build_ok:
        return json.dumps({"success": False, "error": popups}, indent=2)

    check_res = controller.run_check_program()
    return json.dumps(
        {
            "success": bool(check_res.get("passed")),
            "compile_popups": popups,
            "check_program": check_res,
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
