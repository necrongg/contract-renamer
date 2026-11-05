"""Offline 180-day activation helpers. The RSA private key stays outside the app."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

APP_TAG = b"PHARM-RENAMER-180D-V1|"
PUBLIC_N = int.from_bytes(base64.b64decode("nOK8fa4T6CGlbcpyTco2pK1e3l+DsEZ9M7oeHVQZLV2uortIxfe1tkqMV5ZoPnMvezS6MCV2LoCfDRwdpPHHI1hfB+2XFLzJKr4BpkHjSQYVrKgqoBPYqJfpOKVIVQUqHXXx2b97R3LzIn5l3aAolR16uxwCdO82UskG6GWceUXPB56Z3tzZ5APtUG3rnNfdQMlytcjj6gU5AItQswOVUFUArPQTQT1AODr9TkCBw0T7JA13Gv9rgudxw32kIYRu4Yd8vszeNBL2MO5JJWyDl+cB3UfkoRuY4aNbdbkAELNHf7ws7iwiWKPD4p6mXVcWr0YY3tbiuJUWhuyPFNP/cQ=="), "big")
PUBLIC_E = 65537
SIG_BYTES = 256
SHA256_DER_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def device_id() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
    except Exception:
        machine_guid = os.environ.get("COMPUTERNAME", "unknown")
    return hashlib.sha256(b"PetPharm-renamer-v1|" + str(machine_guid).encode("utf-8")).hexdigest()[:20].upper()


def license_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return base / "PetPharm" / "ContractRenamer" / "license.json"


def verify_code(code: str, expected_device: str) -> date:
    normalized = "".join(code.split())
    raw = base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
    if len(raw) <= SIG_BYTES:
        raise ValueError("인증 코드 형식이 올바르지 않습니다.")
    payload, signature = raw[:-SIG_BYTES], raw[-SIG_BYTES:]
    fields = payload.decode("ascii").split("|")
    if len(fields) != 3 or fields[0] != "PH1" or fields[1] != expected_device:
        raise ValueError("이 PC용 인증 코드가 아닙니다.")
    expiry = date.fromisoformat(fields[2])
    digest_info = SHA256_DER_PREFIX + hashlib.sha256(APP_TAG + payload).digest()
    encoded = b"\x00\x01" + b"\xff" * (SIG_BYTES - len(digest_info) - 3) + b"\x00" + digest_info
    if pow(int.from_bytes(signature, "big"), PUBLIC_E, PUBLIC_N).to_bytes(SIG_BYTES, "big") != encoded:
        raise ValueError("인증 코드가 유효하지 않습니다.")
    if expiry < date.today():
        raise ValueError(f"인증 코드가 만료되었습니다. 만료일: {expiry.isoformat()}")
    return expiry


def load_valid_license() -> tuple[str, date] | None:
    path = license_file()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        today = date.today()
        last_seen = date.fromisoformat(record.get("last_seen", ""))
        if today < last_seen:
            return None
        expiry = verify_code(record.get("code", ""), device_id())
        record["last_seen"] = today.isoformat()
        path.write_text(json.dumps(record), encoding="utf-8")
        return device_id(), expiry
    except Exception:
        return None


def save_license(code: str) -> date:
    expiry = verify_code(code, device_id())
    path = license_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"code": "".join(code.split()), "last_seen": date.today().isoformat()}), encoding="utf-8")
    return expiry
