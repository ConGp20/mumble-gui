"""Kommandozeile: ``intercom {plan,apply,export,validate,status}``.

Im Container::

    docker compose exec mumble-admin intercom plan
    docker compose exec mumble-admin intercom apply --prune
    docker compose exec mumble-admin intercom export > intercom-neu.yaml
    docker compose exec mumble-admin intercom validate

Dieselbe Logik haengt im GUI hinter den Knoepfen -- beides ruft
``provision.planner.reconcile`` auf, es gibt keinen zweiten Codepfad.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ConfigError, Settings

if TYPE_CHECKING:
    from .provision.schema import IntercomConfig

__all__ = ["main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
#: Wie `terraform plan -detailed-exitcode`: es gaebe etwas zu tun.
EXIT_CHANGES = 3


def _colour(stream: Any) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _connect(settings: Settings) -> Any:
    """Verbindet sich mit murmur und meldet Fehler in Klartext."""
    from .ice.client import IceClient

    client = IceClient(settings)
    client.connect()
    for warning in client.version_warnings:
        print(f"  WARNUNG  {warning}", file=sys.stderr)
    return client


def _load(settings: Settings, path: str | None) -> IntercomConfig:
    from .provision.schema import load_config

    return load_config(path or settings.intercom_config)


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    """Prueft nur die YAML -- ohne Server, damit es auch offline geht."""
    from .provision.schema import ConfigInvalid

    try:
        config = _load(settings, args.config)
    except ConfigInvalid as exc:
        for issue in exc.issues:
            print(f"  {issue}", file=sys.stderr)
        print(f"\n  {len(exc.issues)} Befund(e). Die Datei ist nicht brauchbar.", file=sys.stderr)
        return EXIT_CONFIG

    for issue in config.issues:
        print(f"  {issue}")
    channels = list(config.all_channels())
    print(
        f"\n  In Ordnung: {len(channels)} Kanaele, {len(config.groups)} Gruppen, "
        f"{len(config.users)} Nutzer, {len(config.acl_templates)} ACL-Vorlagen."
    )
    if config.warnings():
        print(f"  {len(config.warnings())} Warnung(en) -- siehe oben.")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace, settings: Settings) -> int:
    from .provision.planner import reconcile
    from .provision.schema import ConfigInvalid

    try:
        config = _load(settings, args.config)
    except ConfigInvalid as exc:
        for issue in exc.issues:
            print(f"  {issue}", file=sys.stderr)
        return EXIT_CONFIG

    client = _connect(settings)
    try:
        plan = reconcile(
            client,
            config,
            prune=args.prune if args.prune is not None else settings.provision_prune,
            dry_run=True,
            geschuetzt=_geschuetzt(settings),
        )
    finally:
        client.close()

    print(plan.to_text(colour=_colour(sys.stdout)))
    return EXIT_CHANGES if (not plan.empty and args.detailed_exitcode) else EXIT_OK


def cmd_apply(args: argparse.Namespace, settings: Settings) -> int:
    from .provision.planner import reconcile
    from .provision.schema import ConfigInvalid

    try:
        config = _load(settings, args.config)
    except ConfigInvalid as exc:
        for issue in exc.issues:
            print(f"  {issue}", file=sys.stderr)
        return EXIT_CONFIG

    prune = args.prune if args.prune is not None else settings.provision_prune
    client = _connect(settings)
    try:
        if not args.yes:
            preview = reconcile(
                client, config, prune=prune, dry_run=True, geschuetzt=_geschuetzt(settings)
            )
            print(preview.to_text(colour=_colour(sys.stdout)))
            if preview.empty:
                return EXIT_OK
            if not sys.stdin.isatty():
                print(
                    "\n  Abgebrochen: keine Rueckfrage moeglich. Mit --yes erneut aufrufen.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            answer = input("\n  Anwenden? [j/N] ").strip().lower()
            if answer not in {"j", "ja", "y", "yes"}:
                print("  Abgebrochen.")
                return EXIT_OK

        plan = reconcile(
            client, config, prune=prune, dry_run=False, geschuetzt=_geschuetzt(settings)
        )
    finally:
        client.close()

    print(plan.to_text(colour=_colour(sys.stdout)))
    if plan.failed:
        print(f"\n  {len(plan.failed)} Aenderung(en) fehlgeschlagen.", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _geschuetzt(settings: Settings) -> frozenset[str]:
    """Die Regel des Monitor-Bots gehoert der Anwendung (D-035).

    Die Kommandozeile laesst sie so in Ruhe wie die Oberflaeche: ein
    ``apply --prune`` nahme dem Bot sonst die Verlustmessung, und ein Export
    truege den Zertifikats-Hash dieser Installation in jede Datei.
    """
    if not settings.monitor_enabled or not settings.monitor_cert.exists():
        return frozenset()
    try:
        from .monitor.berechtigung import gruppe
        from .monitor.bot import certificate_fingerprint

        return frozenset({gruppe(certificate_fingerprint(settings.monitor_cert))})
    except Exception:  # noqa: BLE001 - ohne Bot-Zertifikat gibt es nichts zu schuetzen
        return frozenset()


def _oberflaechenstand(settings: Settings) -> dict[str, Any]:
    """Was nur die Oberflaeche kennt, aus dem Store -- fuer einen vollstaendigen Export.

    Ohne das waere ``intercom export`` eine stillschweigend unvollstaendige
    Sicherung: feste Plaetze, Verbindungen, Ruftasten und Netze fehlten, und
    das fiele erst beim Einspielen auf. Gibt es noch keinen Store (frische
    Installation, nie gestartet), gibt es auch nichts davon.
    """
    from .store.db import Store

    if not settings.db_path.exists():
        return {}
    store = Store(settings.db_path)
    try:
        store.connect()
        return {
            "wunsch": store.alle_wuensche(),
            "verbindungen": store.verbindungen(),
            "netze": store.netze(),
            "ruftasten": store.ruftasten(),
        }
    except Exception as exc:  # noqa: BLE001 - Export soll trotzdem gehen
        print(
            f"  Hinweis: Oberflaechenstand nicht lesbar ({exc}) -- exportiert wird "
            "nur, was am Server steht.",
            file=sys.stderr,
        )
        return {}
    finally:
        store.close()


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    from .provision.exporter import export_yaml

    zusatz = _oberflaechenstand(settings)
    client = _connect(settings)
    try:
        text = export_yaml(client, ohne_gruppen=_geschuetzt(settings), **zusatz)
    finally:
        client.close()

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"  Nach {args.output} geschrieben.", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return EXIT_OK


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    """Kurzer Zustandsbericht -- gut fuer setup.sh und zum Fehlersuchen."""
    client = _connect(settings)
    try:
        version = client.get_version()
        users = client.get_users()
        channels = client.get_channels()
        uptime = client.get_uptime()
        print(f"  Server      {version.short} ({version.text})")
        print(f"  Laufzeit    {uptime // 3600} h {uptime % 3600 // 60} min")
        print(f"  Kanaele     {len(channels)}")
        print(f"  Clients     {len(users)}")
        if users:
            print("\n  Session  Name                 Kanal  Ping    IP")
            for user in sorted(users.values(), key=lambda u: u.name.lower()):
                channel_name = channels[user.channel].name if user.channel in channels else "?"
                print(
                    f"  {user.session:>7}  {user.name[:20]:<20} "
                    f"{channel_name[:5]:<5}  {user.ping:>5.1f}  {user.address}"
                )
        for warning in client.version_warnings:
            print(f"\n  WARNUNG  {warning}")
    finally:
        client.close()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intercom",
        description=(
            "Werkzeuge fuer das Stadion-Intercom auf der Kommandozeile. "
            "Angelegt wird im Normalfall in der Oberflaeche -- diese Befehle "
            "sind fuer Selbsttest, Sicherung und den Fall, dass die "
            "Oberflaeche nicht erreichbar ist."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Pfad zu einer Vorgabedatei bzw. Sicherung (Vorgabe: INTERCOM_CONFIG).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Ausfuehrliches Log."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_prune(sub: argparse.ArgumentParser) -> None:
        group = sub.add_mutually_exclusive_group()
        group.add_argument(
            "--prune",
            dest="prune",
            action="store_true",
            default=None,
            help="Plaetze und Rollen loeschen, die nicht in der Datei stehen.",
        )
        group.add_argument(
            "--no-prune", dest="prune", action="store_false", help="Nichts loeschen."
        )

    validate = subparsers.add_parser("validate", help="Nur die YAML pruefen, ohne Server.")
    validate.set_defaults(func=cmd_validate)

    plan = subparsers.add_parser("plan", help="Zeigen, was sich aendern wuerde.")
    add_prune(plan)
    plan.add_argument(
        "--detailed-exitcode",
        action="store_true",
        help="Rueckgabewert 3, wenn es Aenderungen gaebe (fuer Skripte).",
    )
    plan.set_defaults(func=cmd_plan)

    apply_cmd = subparsers.add_parser("apply", help="Aenderungen schreiben.")
    add_prune(apply_cmd)
    apply_cmd.add_argument(
        "-y", "--yes", action="store_true", help="Ohne Rueckfrage anwenden."
    )
    apply_cmd.set_defaults(func=cmd_apply)

    export = subparsers.add_parser("export", help="Ist-Zustand als YAML ausgeben.")
    export.add_argument("-o", "--output", help="Zieldatei statt der Standardausgabe.")
    export.set_defaults(func=cmd_export)

    status = subparsers.add_parser("status", help="Kurzer Zustandsbericht.")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"  Konfigurationsfehler: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return int(args.func(args, settings))
    except KeyboardInterrupt:
        print("\n  Abgebrochen.", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"  Fehler: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        print("  Mit -v gibt es den vollstaendigen Traceback.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
