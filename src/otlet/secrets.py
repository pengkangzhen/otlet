"""OS keychain storage for API keys.

The yaml config keeps a "keychain" placeholder; the actual secrets live
in the OS credential store (macOS Keychain / Windows Credential Manager
/ Linux Secret Service). On headless systems without a secret service
the calls degrade gracefully and otlet falls back to plaintext yaml —
availability is probed, never assumed.
"""

from __future__ import annotations

SERVICE = "otlet"
SECRET_FIELDS = ("api_key", "s2_api_key")
_PLACEHOLDER = "keychain"
_PROBE = "__probe__"


def available() -> bool:
    """True when a usable keyring backend is present."""
    try:
        import keyring

        keyring.get_password(SERVICE, _PROBE)  # raises on fail backends
        return True
    except Exception:
        return False


def get_secret(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    """Store a secret; False when no keyring backend is available."""
    try:
        import keyring

        keyring.set_password(SERVICE, name, value)
        return True
    except Exception:
        return False


def delete_secret(name: str) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, name)
    except Exception:
        pass


def is_placeholder(value: str | None) -> bool:
    return value == _PLACEHOLDER


def placeholder() -> str:
    return _PLACEHOLDER
