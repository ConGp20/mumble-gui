"""Vertrag mit der Compose: alle Umgebungsvariablen an genau einer Stelle.

Jede Variable, die ``docker-compose.yml`` an den Service ``mumble-admin``
uebergibt, ist hier abgebildet. Wer eine neue Variable einfuehrt, aendert diese
Datei und die Compose -- sonst nichts.

``Settings.load()`` liest die Umgebung, validiert sie und wirft bei einem harten
Fehler (fehlendes Secret) ``ConfigError``. Weiche Probleme landen in
``Settings.warnings`` und werden im Cockpit als Banner angezeigt.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = ["Settings", "ConfigError", "PLACEHOLDER_PREFIX"]

#: setup.sh ersetzt alle Werte, die so beginnen, durch echte Zufallswerte.
PLACEHOLDER_PREFIX: Final[str] = "ERSETZEN"


class ConfigError(RuntimeError):
    """Die Umgebung ist so kaputt, dass ein Start sinnlos waere."""


def _str(key: str, default: str | None = None) -> str:
    value = os.environ.get(key)
    if value is None or value == "":
        if default is None:
            raise ConfigError(
                f"Umgebungsvariable {key} fehlt. Sie wird von docker-compose.yml "
                "aus der .env gesetzt -- steht sie dort?"
            )
        return default
    return value


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{key}={raw!r} ist keine ganze Zahl.") from None


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        raise ConfigError(f"{key}={raw!r} ist keine Zahl.") from None


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    normalised = raw.strip().lower()
    if normalised in {"1", "true", "yes", "on", "ja"}:
        return True
    if normalised in {"0", "false", "no", "off", "nein"}:
        return False
    raise ConfigError(f"{key}={raw!r} ist kein Wahrheitswert (true/false).")


@dataclass(frozen=True, slots=True)
class Settings:
    """Auswertung der Umgebung. Unveraenderlich, einmal beim Start gebaut."""

    # --- Ice ---------------------------------------------------------------
    ice_host: str
    ice_port: int
    ice_secret: str
    ice_server_id: int

    # --- Web ---------------------------------------------------------------
    listen_host: str
    listen_port: int
    admin_user: str
    admin_password: str
    readonly_user: str | None
    readonly_password: str | None
    session_secret: str

    # --- Provisioning ------------------------------------------------------
    intercom_config: Path
    provision_on_start: bool
    provision_prune: bool

    # --- Monitor-Bot -------------------------------------------------------
    monitor_enabled: bool
    monitor_name: str
    monitor_channel: str
    monitor_password: str | None
    monitor_cert: Path
    monitor_stats_interval_ms: int
    mumble_port: int

    # --- Betrieb -----------------------------------------------------------
    poll_interval_ms: int
    history_retention_hours: int
    alert_ping_ms: float
    alert_loss_pct: float
    log_level: str
    data_dir: Path
    slice_dir: Path
    expected_mumble_version: str

    #: Nicht toedliche Probleme, die das Cockpit als Banner zeigt.
    warnings: tuple[str, ...] = field(default=())

    # ------------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        """SQLite fuer Verlauf, Audit-Log und Notizen."""
        return self.data_dir / "history.sqlite"

    @property
    def ice_proxy(self) -> str:
        """Proxy-String fuer den Meta-Endpunkt."""
        return f"Meta:tcp -h {self.ice_host} -p {self.ice_port}"

    @property
    def has_readonly_account(self) -> bool:
        return bool(self.readonly_user and self.readonly_password)

    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls) -> "Settings":
        warnings: list[str] = []

        ice_secret = _str("ICE_SECRET", "")
        if not ice_secret:
            raise ConfigError(
                "ICE_SECRET ist leer. Ohne Secret laesst murmur keine "
                "schreibenden Ice-Aufrufe zu. setup.sh erzeugt einen Wert."
            )
        if ice_secret.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(
                "ICE_SECRET steht noch auf dem Platzhalter aus .env.example. "
                "./setup.sh ausfuehren oder den Wert von Hand setzen."
            )

        session_secret = _str("SESSION_SECRET", "")
        if not session_secret or session_secret.startswith(PLACEHOLDER_PREFIX):
            # Kein harter Abbruch: das GUI laeuft, aber alle Sessions sind nach
            # einem Neustart ungueltig. Das ist besser als ein toter Container.
            session_secret = secrets.token_urlsafe(32)
            warnings.append(
                "SESSION_SECRET war nicht gesetzt. Es wurde ein Zufallswert "
                "erzeugt -- alle Anmeldungen gehen bei jedem Neustart verloren. "
                "./setup.sh ausfuehren."
            )

        admin_password = _str("ADMIN_PASSWORD", "")
        if not admin_password:
            raise ConfigError("ADMIN_PASSWORD ist leer. Kein Login moeglich.")
        if admin_password.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(
                "ADMIN_PASSWORD steht noch auf dem Platzhalter. ./setup.sh ausfuehren."
            )

        readonly_user = os.environ.get("ADMIN_READONLY_USER", "").strip() or None
        readonly_password = os.environ.get("ADMIN_READONLY_PASSWORD", "").strip() or None
        if readonly_user and not readonly_password:
            warnings.append(
                f"ADMIN_READONLY_USER={readonly_user!r} ist gesetzt, aber "
                "ADMIN_READONLY_PASSWORD fehlt. Der Nur-Lese-Zugang bleibt aus."
            )
            readonly_user = None
        if readonly_password and readonly_password.startswith(PLACEHOLDER_PREFIX):
            warnings.append(
                "ADMIN_READONLY_PASSWORD steht noch auf dem Platzhalter. "
                "Der Nur-Lese-Zugang bleibt aus."
            )
            readonly_user = None
            readonly_password = None

        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        slice_dir = Path(os.environ.get("SLICE_DIR", "/opt/intercom/slice"))
        config_path = Path(_str("INTERCOM_CONFIG", "/config/intercom.yaml"))
        if not config_path.exists():
            warnings.append(
                f"{config_path} nicht gefunden. Provisioning ist deaktiviert, "
                "das Cockpit funktioniert trotzdem."
            )

        monitor_password = os.environ.get("MONITOR_BOT_PASSWORD", "").strip() or None

        poll_interval_ms = _int("POLL_INTERVAL_MS", 2000)
        if poll_interval_ms < 250:
            warnings.append(
                f"POLL_INTERVAL_MS={poll_interval_ms} ist sehr klein. Unter 250 ms "
                "erzeugt das Polling mehr Last als Nutzen; es wird auf 250 gehoben."
            )
            poll_interval_ms = 250

        retention = _int("HISTORY_RETENTION_HOURS", 48)
        if retention < 1:
            warnings.append(
                f"HISTORY_RETENTION_HOURS={retention} ist < 1. Es wird 1 verwendet."
            )
            retention = 1

        log_level = _str("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            warnings.append(f"LOG_LEVEL={log_level!r} unbekannt, INFO wird verwendet.")
            log_level = "INFO"

        return cls(
            ice_host=_str("ICE_HOST", "127.0.0.1"),
            ice_port=_int("ICE_PORT", 6502),
            ice_secret=ice_secret,
            ice_server_id=_int("ICE_SERVER_ID", 1),
            listen_host=_str("LISTEN_HOST", "0.0.0.0"),
            listen_port=_int("LISTEN_PORT", 8080),
            admin_user=_str("ADMIN_USER", "admin"),
            admin_password=admin_password,
            readonly_user=readonly_user,
            readonly_password=readonly_password,
            session_secret=session_secret,
            intercom_config=config_path,
            provision_on_start=_bool("PROVISION_ON_START", True),
            provision_prune=_bool("PROVISION_PRUNE", False),
            monitor_enabled=_bool("MONITOR_BOT_ENABLED", True),
            monitor_name=_str("MONITOR_BOT_NAME", "monitor"),
            monitor_channel=_str("MONITOR_BOT_CHANNEL", "Intercom/Regie"),
            monitor_password=monitor_password,
            monitor_cert=Path(_str("MONITOR_BOT_CERT", "/data/monitor-cert.pem")),
            monitor_stats_interval_ms=_int("MONITOR_STATS_INTERVAL_MS", 5000),
            mumble_port=_int("MUMBLE_PORT", 64738),
            poll_interval_ms=poll_interval_ms,
            history_retention_hours=retention,
            alert_ping_ms=_float("ALERT_PING_MS", 80.0),
            alert_loss_pct=_float("ALERT_LOSS_PCT", 2.0),
            log_level=log_level,
            data_dir=data_dir,
            slice_dir=slice_dir,
            expected_mumble_version=_str("MUMBLE_VERSION", "unbekannt"),
            warnings=tuple(warnings),
        )

    def redacted(self) -> dict[str, object]:
        """Fassung fuer Log und ``/healthz`` -- ohne Secrets.

        ICE_SECRET, Passwoerter und SESSION_SECRET tauchen hier bewusst nur als
        Laengenangabe auf. Sie duerfen niemals ins Log oder ins Frontend.
        """
        return {
            "ice": f"{self.ice_host}:{self.ice_port} (server {self.ice_server_id})",
            "ice_secret": f"<{len(self.ice_secret)} Zeichen>",
            "listen": f"{self.listen_host}:{self.listen_port}",
            "admin_user": self.admin_user,
            "readonly_user": self.readonly_user or "(aus)",
            "intercom_config": str(self.intercom_config),
            "provision_on_start": self.provision_on_start,
            "provision_prune": self.provision_prune,
            "monitor": (
                f"{self.monitor_name} -> {self.monitor_channel}"
                if self.monitor_enabled
                else "(aus)"
            ),
            "poll_interval_ms": self.poll_interval_ms,
            "history_retention_hours": self.history_retention_hours,
            "alert_ping_ms": self.alert_ping_ms,
            "alert_loss_pct": self.alert_loss_pct,
            "expected_mumble_version": self.expected_mumble_version,
            "db_path": str(self.db_path),
        }
