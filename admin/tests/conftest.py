"""Gemeinsame Test-Vorbereitung.

Die Slice v1.5.735 und ihre Uebersetzung liegen unter ``admin/slice/`` im
Repository; das Image erzeugt seine eigene aus dem Tag in ``MUMBLE_VERSION``.
Hier wird das Verzeichnis in den Suchpfad gehaengt -- und fehlt die
Uebersetzung (etwa nach einem Versionswechsel), einmalig neu erzeugt.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ADMIN_DIR = Path(__file__).resolve().parent.parent
SLICE_DIR = Path(os.environ.get("SLICE_DIR", ADMIN_DIR / "slice"))
MUMBLE_VERSION = os.environ.get("MUMBLE_VERSION", "v1.5.735")


def _ensure_slice() -> bool:
    """Sorgt dafuer, dass ``MumbleServer`` importierbar ist."""
    if str(SLICE_DIR) not in sys.path:
        sys.path.insert(0, str(SLICE_DIR))
    if (SLICE_DIR / "MumbleServer_ice.py").exists():
        return True
    script = ADMIN_DIR / "scripts" / "build_slice.sh"
    if not script.exists():
        return False
    try:
        subprocess.run(
            [str(script), MUMBLE_VERSION, str(SLICE_DIR)],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return (SLICE_DIR / "MumbleServer_ice.py").exists()


if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

SLICE_AVAILABLE = _ensure_slice()

try:
    import Ice  # type: ignore[import-not-found]  # noqa: F401

    ICE_AVAILABLE = True
except ImportError:  # pragma: no cover
    ICE_AVAILABLE = False


#: Tests, die eine echte Ice-Laufzeit brauchen.
needs_ice = pytest.mark.skipif(
    not (ICE_AVAILABLE and SLICE_AVAILABLE),
    reason=(
        "zeroc-ice oder die uebersetzte Slice fehlt. "
        "admin/scripts/build_slice.sh ausfuehren."
    ),
)


@pytest.fixture()
def fake_murmur():
    """Ein laufendes murmur-Doppel auf einem freien Loopback-Port."""
    from tests.fake_murmur import FakeMurmur

    fake = FakeMurmur()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture()
def ice_client(fake_murmur):
    """Ein verbundener :class:`~intercom.ice.client.IceClient`."""
    from intercom.ice.client import IceClient

    client = IceClient(fake_murmur.settings())
    client.connect()
    try:
        yield client
    finally:
        client.close()
