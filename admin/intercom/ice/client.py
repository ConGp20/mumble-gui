"""Ice-Anbindung an murmur.

Aufbau
------
``IceClient`` ist **synchron**. Die von ``slice2py`` erzeugten Proxies blockieren,
und Ice bringt eigene Threads mit. Der Web-Layer benutzt daher
``AsyncIceClient``, das jeden Aufruf in einen kleinen Threadpool schiebt und so
den asyncio-Loop frei haelt.

Verbindung
----------
* ``Ice.ImplicitContext=Shared`` plus ``getImplicitContext().put("secret", ...)``
  haengt das Secret an *jeden* Aufruf. murmur prueft es pro Aufruf, nicht pro
  Verbindung.
* Fuer Callbacks oeffnen wir einen eigenen Objektadapter auf ``127.0.0.1`` mit
  Port 0 (Ice sucht sich einen freien). Das geht nur, weil beide Container im
  Host-Netz laufen -- murmur muss uns aktiv erreichen koennen.

Versionspruefung
----------------
Statt nur ``Meta.getVersion()`` gegen den Compose-Tag zu halten, vergleichen wir
zusaetzlich die **Slice-Pruefsummen**: ``Meta.getSliceChecksums()`` liefert die
Summen der Slice, mit der der Server gebaut wurde, ``Ice.sliceChecksums`` die
unserer einkompilierten Fassung. Das erkennt auch den Fall, in dem sich die
Schnittstelle geaendert hat, ohne dass die Versionsnummer es verraet.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import Ice
import MumbleServer

from ..config import Settings
from .errors import (
    IceAuthError,
    IceCallFailed,
    IceConnectionLost,
    IceNotConnected,
)
from .permissions import verify_against_slice
from .types import (
    ACLEntry,
    BanEntry,
    ChannelACL,
    ChannelGroup,
    LogEntry,
    MumbleChannel,
    MumbleUser,
    RegisteredUser,
    ServerVersion,
    encode_address,
)

log = logging.getLogger(__name__)

__all__ = ["AsyncIceClient", "CallbackAdapter", "IceClient"]


#: Name des Adapters fuer Rueckrufe. Muss zum Property-Praefix passen.
_CALLBACK_ADAPTER = "IntercomCallback"

#: Obergrenze fuer einen einzelnen Ice-Aufruf (Millisekunden).
#:
#: Wichtig: ``Ice.Override.Timeout`` taugt dafuer **nicht**. Das ist ein
#: Endpunkt-Timeout und begrenzt nur einzelne Socket-Operationen; ein murmur,
#: der die Antwort schuldig bleibt, laesst den Aufruf trotzdem beliebig lange
#: haengen (gemessen: 12 s Aufruf bei ``Ice.Override.Timeout=3000``). Nur
#: ``proxy.ice_invocationTimeout()`` bricht den Aufruf wirklich ab -- derselbe
#: Test lieferte damit nach exakt 2 s eine ``InvocationTimeoutException``.
#:
#: Das ist keine Feinheit: der Threadpool von :class:`AsyncIceClient` hat vier
#: Plaetze. Vier haengende Aufrufe legen sonst die gesamte Oberflaeche lahm,
#: einschliesslich ``/healthz`` und ``/metrics``.
INVOCATION_TIMEOUT_MS = 15_000

#: Abbildung UserInfo-Enum -> Feldname in :class:`RegisteredUser`.
_USERINFO_FIELDS: dict[Any, str] = {}


def _init_userinfo_fields() -> None:
    ui = MumbleServer.UserInfo
    _USERINFO_FIELDS.update(
        {
            ui.UserName: "name",
            ui.UserEmail: "email",
            ui.UserComment: "comment",
            ui.UserHash: "hash",
            ui.UserLastActive: "last_active",
            ui.UserKDFIterations: "kdf_iterations",
        }
    )


_init_userinfo_fields()


def _translate(exc: BaseException) -> Exception:
    """Ice-Ausnahme -> unsere Fehlerklasse, mit brauchbarem deutschen Text."""
    name = type(exc).__name__
    if isinstance(exc, MumbleServer.InvalidSecretException):
        return IceAuthError(
            "murmur hat das Ice-Secret abgelehnt. ICE_SECRET in der .env stimmt "
            "nicht mit icesecretwrite in der Serverkonfiguration ueberein."
        )
    if isinstance(exc, MumbleServer.InvalidChannelException):
        return IceCallFailed("Der Kanal existiert nicht (mehr).", name)
    if isinstance(exc, MumbleServer.InvalidSessionException):
        return IceCallFailed(
            "Die Sitzung existiert nicht mehr -- der Client hat sich getrennt.", name
        )
    if isinstance(exc, MumbleServer.InvalidUserException):
        return IceCallFailed("Diesen registrierten Nutzer gibt es nicht.", name)
    if isinstance(exc, MumbleServer.InvalidServerException):
        return IceCallFailed(
            "Der virtuelle Server existiert nicht. ICE_SERVER_ID pruefen.", name
        )
    if isinstance(exc, MumbleServer.ServerBootedException):
        return IceCallFailed("Der virtuelle Server laeuft nicht.", name)
    if isinstance(exc, MumbleServer.NestingLimitException):
        return IceCallFailed(
            "Die maximale Kanal-Verschachtelung ist erreicht "
            "(channelnestinglimit in der Serverkonfiguration).",
            name,
        )
    if isinstance(exc, MumbleServer.InvalidInputDataException):
        return IceCallFailed("Ungueltige Eingabedaten.", name)
    if isinstance(exc, MumbleServer.WriteOnlyException):
        return IceCallFailed(
            "Dieser Wert ist nicht lesbar (write-only, z. B. ein Secret).", name
        )
    if isinstance(exc, (Ice.ConnectionRefusedException, Ice.ConnectTimeoutException)):
        return IceConnectionLost(
            "Keine Verbindung zur Ice-Schnittstelle. Laeuft mumble-server, und "
            "ist 'ice' in dessen Konfiguration gesetzt?"
        )
    if isinstance(exc, (Ice.ConnectionLostException, Ice.CloseConnectionException)):
        return IceConnectionLost("Die Verbindung zu murmur wurde unterbrochen.")
    if isinstance(exc, Ice.InvocationTimeoutException):
        # Der Aufruf lief in INVOCATION_TIMEOUT_MS. Ob die Verbindung noch
        # steht, wissen wir nicht -- murmur kann den Auftrag durchaus noch
        # ausfuehren. Wir werten es trotzdem als Verbindungsverlust, damit
        # _call() den Proxy verwirft und der Reconnect sauber neu aufsetzt.
        sekunden = f"{INVOCATION_TIMEOUT_MS / 1000:g}".replace(".", ",")
        return IceConnectionLost(
            f"murmur hat den Aufruf nicht innerhalb von {sekunden} s beantwortet."
        )
    if isinstance(exc, Ice.TimeoutException):
        return IceConnectionLost("murmur antwortet nicht (Zeitueberschreitung).")
    if isinstance(exc, Ice.ObjectNotExistException):
        return IceConnectionLost(
            "murmur kennt das Meta-Objekt nicht mehr -- vermutlich neu gestartet."
        )
    if isinstance(exc, Ice.LocalException):
        return IceConnectionLost(f"Ice-Fehler: {name}: {exc}")
    if isinstance(exc, Ice.UserException):
        return IceCallFailed(f"Der Server meldet {name}.", name)
    return exc if isinstance(exc, Exception) else RuntimeError(str(exc))


class CallbackAdapter:
    """Nimmt die Rueckrufe von murmur entgegen und reicht sie weiter.

    murmur ruft aus eigenen Threads auf. Alle Handler muessen deshalb
    thread-sicher sein; ``AsyncIceClient`` uebergibt Handler, die das Ereignis
    per ``call_soon_threadsafe`` in den asyncio-Loop heben.

    Wichtig: wirft ein Handler eine Ausnahme, entfernt murmur den Callback
    kommentarlos ("If an added callback ever throws an exception or goes away,
    it will be automatically removed"). Wir fangen deshalb alles ab.
    """

    # slice2py erzeugt die Basisklasse zur Laufzeit; fuer mypy ist sie Any.
    class _Servant(MumbleServer.ServerCallback):  # type: ignore[misc]
        def __init__(self, outer: CallbackAdapter) -> None:
            self._outer = outer

        # -- Nutzer ----------------------------------------------------------
        def userConnected(self, state: Any, current: Any = None) -> None:
            self._outer._emit("user_connected", MumbleUser.from_ice(state))

        def userDisconnected(self, state: Any, current: Any = None) -> None:
            self._outer._emit("user_disconnected", MumbleUser.from_ice(state))

        def userStateChanged(self, state: Any, current: Any = None) -> None:
            self._outer._emit("user_state_changed", MumbleUser.from_ice(state))

        def userTextMessage(self, state: Any, message: Any, current: Any = None) -> None:
            self._outer._emit(
                "user_text_message",
                (
                    MumbleUser.from_ice(state),
                    {
                        "text": message.text,
                        "sessions": list(message.sessions),
                        "channels": list(message.channels),
                        "trees": list(message.trees),
                    },
                ),
            )

        # -- Kanaele ---------------------------------------------------------
        def channelCreated(self, state: Any, current: Any = None) -> None:
            self._outer._emit("channel_created", MumbleChannel.from_ice(state))

        def channelRemoved(self, state: Any, current: Any = None) -> None:
            self._outer._emit("channel_removed", MumbleChannel.from_ice(state))

        def channelStateChanged(self, state: Any, current: Any = None) -> None:
            self._outer._emit("channel_state_changed", MumbleChannel.from_ice(state))

    def __init__(self) -> None:
        self._handlers: list[Callable[[str, Any], None]] = []
        self._lock = threading.Lock()
        self.servant = CallbackAdapter._Servant(self)

    def subscribe(self, handler: Callable[[str, Any], None]) -> None:
        with self._lock:
            self._handlers.append(handler)

    def _emit(self, event: str, payload: Any) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event, payload)
            except Exception:
                log.exception(
                    "Callback-Handler fuer %s ist gescheitert. murmur wuerde den "
                    "Callback sonst abmelden, deshalb wird der Fehler geschluckt.",
                    event,
                )


class IceClient:
    """Synchroner Zugriff auf ``Meta`` und ``Server``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._communicator: Any = None
        self._meta: Any = None
        self._server: Any = None
        self._adapter: Any = None
        self._callback_proxy: Any = None
        self.callbacks = CallbackAdapter()
        #: Schuetzt die Felder oben. Wird nur kurz gehalten -- insbesondere
        #: nie ueber ``communicator.destroy()`` hinweg.
        self._lock = threading.RLock()
        #: Serialisiert Auf- und Abbau gegeneinander, damit nicht zwei
        #: Threads gleichzeitig einen Communicator bauen. Getrennt von
        #: ``_lock``, weil der Aufbau Sekunden dauern darf, das Lesen der
        #: Felder aber nicht warten soll.
        self._setup_lock = threading.Lock()
        #: Klartext-Warnungen zur Slice-/Versionslage, fuer das Cockpit-Banner.
        self.version_warnings: list[str] = []
        self.server_version: ServerVersion | None = None

    # ------------------------------------------------------------------ #
    #  Verbindung
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._server is not None

    def connect(self) -> None:
        """Baut die Verbindung auf und registriert den Callback.

        Idempotent: ein zweiter Aufruf auf einer stehenden Verbindung tut nichts.
        """
        with self._setup_lock:
            with self._lock:
                if self.connected:
                    return
                alt = self._detach_communicator()
            # Reste eines gescheiterten Versuchs abbauen -- nebenher, damit
            # der Reconnect nicht darauf wartet, dass ein alter, haengender
            # Aufruf endlich zurueckkommt.
            self._destroy_communicator_bg(alt)

            props = Ice.createProperties()
            # Secret an jeden Aufruf haengen.
            props.setProperty("Ice.ImplicitContext", "Shared")
            # Ein toter murmur soll nach Sekunden auffallen, nicht nach Minuten.
            props.setProperty("Ice.Override.ConnectTimeout", "5000")
            # Kein Ice.Override.Timeout -- siehe INVOCATION_TIMEOUT_MS oben.
            # Verbindung offen halten, sonst raeumt Ice sie ab und die
            # Rueckrufe versiegen still.
            props.setProperty("Ice.ACM.Client.Timeout", "30")
            props.setProperty("Ice.ACM.Client.Heartbeat", "3")  # HeartbeatAlways
            props.setProperty("Ice.ACM.Server.Timeout", "0")
            # Zertifikatsketten und grosse Kanalbaeume sprengen die 1-MB-Vorgabe.
            props.setProperty("Ice.MessageSizeMax", "8192")
            # Ice soll nicht in unser Log schreiben; wir uebersetzen selbst.
            props.setProperty("Ice.Warn.Connections", "0")
            # Adapter fuer Rueckrufe: Loopback, freier Port.
            props.setProperty(f"{_CALLBACK_ADAPTER}.Endpoints", "tcp -h 127.0.0.1 -p 0")

            init_data = Ice.InitializationData()
            init_data.properties = props

            communicator = Ice.initialize(init_data)
            try:
                communicator.getImplicitContext().put("secret", self._settings.ice_secret)

                proxy = communicator.stringToProxy(self._settings.ice_proxy)
                # Ab hier begrenzt jeder Aufruf sich selbst. checkedCast ist
                # bereits ein entfernter Aufruf, die Grenze muss also vorher
                # am Proxy haengen; die ice_*-Methoden liefern einen Proxy
                # desselben Typs zurueck.
                proxy = proxy.ice_invocationTimeout(INVOCATION_TIMEOUT_MS)
                meta = MumbleServer.MetaPrx.checkedCast(proxy)
                if meta is None:
                    raise IceConnectionLost(
                        f"Unter {self._settings.ice_proxy} antwortet kein "
                        "Mumble-Meta-Objekt."
                    )

                server = meta.getServer(self._settings.ice_server_id)
                if server is None:
                    raise IceCallFailed(
                        f"Virtueller Server {self._settings.ice_server_id} existiert "
                        "nicht. ICE_SERVER_ID pruefen (murmur zaehlt ab 1)."
                    )
                # Ein per Aufruf zurueckgegebener Proxy erbt die Einstellungen
                # des Aufrufers nicht -- er entsteht aus den Vorgaben des
                # Communicators. Also erneut setzen.
                server = server.ice_invocationTimeout(INVOCATION_TIMEOUT_MS)
            except Exception as exc:
                self._destroy_communicator(communicator)
                raise _translate(exc) from exc

            with self._lock:
                self._communicator = communicator
                self._meta = meta
                self._server = server

            try:
                self._check_versions(meta)
                self._install_callback(communicator, server)
            except Exception:
                # Versions- oder Callback-Probleme duerfen die Verbindung nicht
                # verhindern -- lesend funktioniert das Cockpit trotzdem.
                log.exception("Verbindung steht, aber die Nacharbeit ist gescheitert.")

    def _install_callback(self, communicator: Any, server: Any) -> None:
        # communicator und server kommen als Parameter, nicht aus self:
        # zwischen dem Veroeffentlichen und hier kann ein paralleler Aufruf
        # die Verbindung schon wieder verworfen haben.
        adapter = communicator.createObjectAdapter(_CALLBACK_ADAPTER)
        adapter.activate()
        proxy = adapter.addWithUUID(self.callbacks.servant)
        callback_proxy = MumbleServer.ServerCallbackPrx.uncheckedCast(proxy)
        server.addCallback(callback_proxy)
        with self._lock:
            self._adapter = adapter
            self._callback_proxy = callback_proxy
        endpoints = ", ".join(str(e) for e in proxy.ice_getEndpoints())
        log.info("Ice-Callback registriert, eigener Adapter auf %s", endpoints)

    def _check_versions(self, meta: Any) -> None:
        """Vergleicht Serverversion und Slice-Pruefsummen mit unserem Stand."""
        warnings: list[str] = []

        major, minor, patch, text = meta.getVersion()
        self.server_version = ServerVersion(major, minor, patch, text)
        expected = self._settings.expected_mumble_version
        if not self.server_version.matches_tag(expected):
            warnings.append(
                f"Der Server laeuft als {self.server_version.short} "
                f"({text}), die einkompilierte Slice stammt aber aus {expected}. "
                "Aufrufe koennen fehlschlagen. MUMBLE_VERSION in der .env "
                "angleichen und das Admin-Image neu bauen."
            )

        # Pruefsummenvergleich: praeziser als die Versionsnummer.
        try:
            remote = dict(meta.getSliceChecksums())
        except Exception:
            remote = {}
            log.debug("getSliceChecksums nicht verfuegbar", exc_info=True)
        if remote:
            local = dict(Ice.sliceChecksums)
            differing = [
                key
                for key, value in remote.items()
                if key.startswith("::MumbleServer::") and local.get(key) != value
            ]
            missing = [
                key
                for key in remote
                if key.startswith("::MumbleServer::") and key not in local
            ]
            if differing or missing:
                detail = ", ".join(sorted(differing + missing)[:6])
                warnings.append(
                    f"{len(differing) + len(missing)} Slice-Typen weichen von der "
                    f"Serverfassung ab (z. B. {detail}). Das Admin-Image mit "
                    "passendem MUMBLE_VERSION neu bauen."
                )

        warnings.extend(verify_against_slice(MumbleServer))
        self.version_warnings = warnings
        for warning in warnings:
            log.warning("%s", warning)

    def close(self) -> None:
        with self._setup_lock:
            with self._lock:
                server = self._server
                callback_proxy = self._callback_proxy
                alt = self._detach_communicator()
            # Ab hier ist der Client fuer alle anderen Threads "nicht
            # verbunden"; die folgenden Aufrufe duerfen also dauern.
            if server is not None and callback_proxy is not None:
                try:
                    server.removeCallback(callback_proxy)
                except Exception:
                    log.debug("removeCallback fehlgeschlagen", exc_info=True)
            self._destroy_communicator(alt)

    def _detach_communicator(self) -> Any:
        """Loest den Communicator aus dem Objekt und gibt ihn zurueck.

        Nur unter ``self._lock`` aufrufen. Der teure Teil -- ``destroy()`` --
        gehoert **ausserhalb** der Sperre in :meth:`_destroy_communicator`:
        ``destroy()`` wartet auf laufende Aufrufe und den Abbau des
        Threadpools und blockiert dabei so lange, wie der haengende Aufruf
        braucht (gemessen: 11 s). Mit gehaltener Sperre steht in genau dieser
        Zeit auch der Reconnect, der die Lage retten soll.
        """
        self._server = None
        self._meta = None
        self._adapter = None
        self._callback_proxy = None
        alt = self._communicator
        self._communicator = None
        return alt

    @staticmethod
    def _destroy_communicator(communicator: Any) -> None:
        """Baut einen abgeloesten Communicator ab. Ohne gehaltene Sperre."""
        if communicator is None:
            return
        try:
            communicator.destroy()
        except Exception:
            log.debug("Communicator-Abbau fehlgeschlagen", exc_info=True)

    @classmethod
    def _destroy_communicator_bg(cls, communicator: Any) -> None:
        """Baut den Communicator in einem eigenen Thread ab.

        ``destroy()`` wartet auf ausstehende Aufrufe. Gemessen: 4 s, nachdem
        der Aufruf bereits in den Invocation-Timeout gelaufen war. Auf dem
        Fehlerpfad von :meth:`_call` haenge sonst ein Thread des Pools genau
        so lange fest wie der Aufruf, den wir gerade abgebrochen haben --
        der Abbruch waere umsonst gewesen.

        Der Communicator ist zu diesem Zeitpunkt bereits abgekoppelt; niemand
        greift mehr darauf zu. Der Thread ist ein Daemon: haengt Ice beim
        Abbau, blockiert das den Prozessende nicht.
        """
        if communicator is None:
            return
        threading.Thread(
            target=cls._destroy_communicator,
            args=(communicator,),
            name="ice-abbau",
            daemon=True,
        ).start()

    def _drop_connection(self) -> None:
        """Verwirft die Verbindung: Felder unter Sperre, Abbau nebenher."""
        with self._lock:
            alt = self._detach_communicator()
        self._destroy_communicator_bg(alt)

    def _srv(self) -> Any:
        server = self._server
        if server is None:
            raise IceNotConnected("Keine Verbindung zu murmur.")
        return server

    def _call(self, func: Callable[[], Any]) -> Any:
        """Fuehrt einen Ice-Aufruf aus und uebersetzt Fehler.

        Bei einem Verbindungsfehler wird die Verbindung sofort verworfen, damit
        der Reconnect-Task sie neu aufbaut statt auf einem toten Proxy zu haengen.
        """
        try:
            return func()
        except Exception as exc:
            translated = _translate(exc)
            if isinstance(translated, IceConnectionLost):
                log.warning("Verbindung zu murmur verloren: %s", translated)
                self._drop_connection()
            raise translated from exc

    # ------------------------------------------------------------------ #
    #  Meta
    # ------------------------------------------------------------------ #

    def get_version(self) -> ServerVersion:
        def run() -> ServerVersion:
            major, minor, patch, text = self._meta.getVersion()
            return ServerVersion(major, minor, patch, text)

        if self._meta is None:
            raise IceNotConnected("Keine Verbindung zu murmur.")
        return self._call(run)

    def get_meta_uptime(self) -> int:
        if self._meta is None:
            raise IceNotConnected("Keine Verbindung zu murmur.")
        return self._call(lambda: self._meta.getUptime())

    def get_default_conf(self) -> dict[str, str]:
        if self._meta is None:
            raise IceNotConnected("Keine Verbindung zu murmur.")
        return self._call(lambda: dict(self._meta.getDefaultConf()))

    # ------------------------------------------------------------------ #
    #  Server -- lesend
    # ------------------------------------------------------------------ #

    def get_uptime(self) -> int:
        return self._call(lambda: self._srv().getUptime())

    def get_users(self) -> dict[int, MumbleUser]:
        raw = self._call(lambda: self._srv().getUsers())
        return {session: MumbleUser.from_ice(user) for session, user in raw.items()}

    def get_channels(self) -> dict[int, MumbleChannel]:
        raw = self._call(lambda: self._srv().getChannels())
        return {cid: MumbleChannel.from_ice(channel) for cid, channel in raw.items()}

    def get_state(self, session: int) -> MumbleUser:
        return MumbleUser.from_ice(self._call(lambda: self._srv().getState(session)))

    def get_channel_state(self, channel_id: int) -> MumbleChannel:
        return MumbleChannel.from_ice(
            self._call(lambda: self._srv().getChannelState(channel_id))
        )

    def get_certificate_list(self, session: int) -> list[bytes]:
        """Vollstaendige Zertifikatskette eines verbundenen Clients (DER)."""
        raw = self._call(lambda: self._srv().getCertificateList(session))
        return [bytes(bytearray(cert)) for cert in raw]

    def get_acl(self, channel_id: int) -> ChannelACL:
        """``getACL`` liefert drei out-Parameter; Python gibt sie als Tupel."""

        def run() -> ChannelACL:
            acls, groups, inherit = self._srv().getACL(channel_id)
            return ChannelACL(
                channel_id=channel_id,
                acls=[ACLEntry.from_ice(a) for a in acls],
                groups=[ChannelGroup.from_ice(g) for g in groups],
                inherit=inherit,
            )

        return self._call(run)

    def get_bans(self) -> list[BanEntry]:
        return [BanEntry.from_ice(b) for b in self._call(lambda: self._srv().getBans())]

    def get_log_len(self) -> int:
        return self._call(lambda: self._srv().getLogLen())

    def get_log(self, first: int = 0, last: int = 100) -> list[LogEntry]:
        """``first`` = 0 ist der *neueste* Eintrag."""
        raw = self._call(lambda: self._srv().getLog(first, last))
        return [LogEntry.from_ice(entry) for entry in raw]

    def get_effective_conf(self) -> dict[str, str]:
        """Die Werte, mit denen der Server tatsaechlich laeuft.

        Keiner der beiden Ice-Aufrufe liefert das allein, und ihre Namen fuehren
        in die Irre:

        * ``Server::getAllConf`` liest ``SELECT key, value FROM config WHERE
          server_id = ?`` -- **nur** was jemand zur Laufzeit per ``setConf``
          geaendert hat. Auf einem frischen Server steht dort ausser dem selbst
          erzeugten ``certificate`` nichts.
        * ``Meta::getDefaultConf`` liefert ``qmConfig``, gebaut von
          ``MetaParams`` aus der **ini-Datei** plus den eingebauten Vorgaben.
          Beim Docker-Image also genau das, was die ``MUMBLE_CONFIG_*``-
          Variablen der Compose geschrieben haben. "Default" heisst hier
          nicht "Werkseinstellung".

        Wirksam ist damit: Datenbankeintrag, wenn vorhanden, sonst Dateiwert.
        Wer nur ``getAllConf`` nimmt, verliert auf einem ueber die Compose
        eingerichteten Server praktisch die gesamte Konfiguration.
        """
        aus_datei = self.get_default_conf()
        aus_datei.update(self.get_all_conf())
        return aus_datei

    def get_all_conf(self) -> dict[str, str]:
        return self._call(lambda: dict(self._srv().getAllConf()))

    def get_conf(self, key: str) -> str:
        return self._call(lambda: self._srv().getConf(key))

    def get_registered_users(self, filter_text: str = "") -> dict[int, str]:
        return self._call(lambda: dict(self._srv().getRegisteredUsers(filter_text)))

    def get_registration(self, userid: int) -> RegisteredUser:
        raw = self._call(lambda: self._srv().getRegistration(userid))
        user = RegisteredUser(userid=userid, name="")
        for key, value in raw.items():
            field_name = _USERINFO_FIELDS.get(key)
            if field_name:
                setattr(user, field_name, value)
        return user

    def get_user_ids(self, names: Sequence[str]) -> dict[str, int]:
        """Namen -> IDs. Unbekannte Namen kommen mit -1 zurueck."""
        if not names:
            return {}
        return self._call(lambda: dict(self._srv().getUserIds(list(names))))

    def get_user_names(self, ids: Sequence[int]) -> dict[int, str]:
        if not ids:
            return {}
        return self._call(lambda: dict(self._srv().getUserNames(list(ids))))

    def effective_permissions(self, session: int, channel_id: int) -> int:
        """Tatsaechliche Rechte eines Clients in einem Kanal.

        Das Cached-Bit wird ausmaskiert -- es ist ein interner Marker des
        Servers und kein Recht.
        """
        from .permissions import CACHED_BIT

        raw = self._call(
            lambda: self._srv().effectivePermissions(session, channel_id)
        )
        return int(raw) & ~CACHED_BIT

    # -- Channel Listener ---------------------------------------------------
    #
    # ACHTUNG: Die Slice dokumentiert den ersten Parameter als "The ID of the
    # user". Das ist falsch. Die Implementierung in MumbleServerIce.cpp nennt
    # ihn `session` und loest ihn ueber NEED_PLAYER als *Session* auf:
    #
    #     static void impl_Server_startListening(..., int session, int channelid) {
    #         NEED_SERVER; NEED_CHANNEL; NEED_PLAYER;
    #         server->startListeningToChannel(user, channel);
    #
    # Folge: Listener sind an die Sitzung gebunden und ueberleben einen
    # Reconnect des Clients nicht. Der Provisioner setzt sie deshalb zur
    # Laufzeit per Callback nach. Siehe README, Abschnitt "Bekannte Grenzen".

    def get_listening_channels(self, session: int) -> list[int]:
        return list(self._call(lambda: self._srv().getListeningChannels(session)))

    def get_listening_users(self, channel_id: int) -> list[int]:
        """Gibt **Session-IDs** zurueck, nicht Nutzer-IDs."""
        return list(self._call(lambda: self._srv().getListeningUsers(channel_id)))

    def start_listening(self, session: int, channel_id: int) -> None:
        self._call(lambda: self._srv().startListening(session, channel_id))

    def stop_listening(self, session: int, channel_id: int) -> None:
        self._call(lambda: self._srv().stopListening(session, channel_id))

    def is_listening(self, session: int, channel_id: int) -> bool:
        return bool(self._call(lambda: self._srv().isListening(session, channel_id)))

    def redirect_whisper_group(self, session: int, source: str, target: str) -> None:
        """Leitet einen Gruppenruf dieser Sitzung auf eine andere Gruppe um.

        Ruft der Client auf einen Platz mit Gruppenbeschraenkung ``source``,
        geht der Ruf stattdessen an ``target``. Leeres ``target`` hebt die
        Umleitung auf. Gilt nur fuer diese Sitzung und laesst sich nicht
        zuruecklesen -- die Slice hat keinen Getter dafuer.

        Ob der Ruf ankommt, entscheidet weiterhin das Fluesterrecht des Rufenden
        am Platz jedes Empfaengers (``createWhisperTargetCacheFor`` in
        Server.cpp prueft es je Zielplatz).
        """
        self._call(lambda: self._srv().redirectWhisperGroup(session, source, target))

    # ------------------------------------------------------------------ #
    #  Server -- schreibend
    # ------------------------------------------------------------------ #

    def set_conf(self, key: str, value: str) -> None:
        self._call(lambda: self._srv().setConf(key, value))

    def set_superuser_password(self, password: str) -> None:
        self._call(lambda: self._srv().setSuperuserPassword(password))

    def add_channel(self, name: str, parent: int) -> int:
        return int(self._call(lambda: self._srv().addChannel(name, parent)))

    def remove_channel(self, channel_id: int) -> None:
        self._call(lambda: self._srv().removeChannel(channel_id))

    def set_channel_state(self, channel: MumbleChannel) -> None:
        """Aendert Name, Beschreibung, Position, Eltern oder Verlinkungen.

        Der Elternwechsel funktioniert, indem ``parent`` gesetzt und der ganze
        Zustand geschrieben wird -- murmur verschiebt den Kanal dann samt
        Unterbaum.
        """

        def run() -> None:
            state = MumbleServer.Channel()
            state.id = channel.id
            state.name = channel.name
            state.parent = channel.parent
            state.description = channel.description
            state.temporary = channel.temporary
            state.position = channel.position
            state.links = list(channel.links)
            self._srv().setChannelState(state)

        self._call(run)

    def set_channel_acl(self, acl: ChannelACL) -> None:
        """Schreibt ACLs und Gruppen eines Kanals.

        ``setACL`` ersetzt **beides vollstaendig**. Geerbte Eintraege werden
        vorher entfernt: murmur liefert sie bei ``getACL`` mit, wuerde sie beim
        Zurueckschreiben aber als *eigene* Eintraege des Kanals anlegen und die
        Vererbung damit einfrieren.
        """

        def run() -> None:
            acl_list = []
            for entry in acl.acls:
                if entry.inherited:
                    continue
                item = MumbleServer.ACL()
                item.applyHere = entry.apply_here
                item.applySubs = entry.apply_subs
                item.inherited = False
                item.userid = entry.userid
                item.group = entry.group
                item.allow = entry.allow
                item.deny = entry.deny
                acl_list.append(item)

            group_list = []
            for group in acl.groups:
                if group.inherited:
                    continue
                item = MumbleServer.Group()
                item.name = group.name
                item.inherited = False
                item.inherit = group.inherit
                item.inheritable = group.inheritable
                item.add = list(group.add)
                item.remove = list(group.remove)
                # `members` ist read-only und wird von murmur ignoriert.
                item.members = []
                group_list.append(item)

            self._srv().setACL(acl.channel_id, acl_list, group_list, acl.inherit)

        self._call(run)

    def set_user_state(
        self,
        session: int,
        *,
        channel: int | None = None,
        mute: bool | None = None,
        deaf: bool | None = None,
        suppress: bool | None = None,
        priority_speaker: bool | None = None,
        comment: str | None = None,
        name: str | None = None,
    ) -> None:
        """Aendert den Zustand eines verbundenen Clients.

        ``setState`` erwartet ein vollstaendiges ``User``-Struct. Wir holen den
        aktuellen Zustand, aendern nur die uebergebenen Felder und schreiben
        zurueck -- sonst wuerden ungesetzte Felder auf ihre Vorgabe fallen.

        ``recording`` ist laut Slice read-only und wird nie geschrieben.
        """

        def run() -> None:
            state = self._srv().getState(session)
            if channel is not None:
                state.channel = channel
            if mute is not None:
                state.mute = mute
            if deaf is not None:
                state.deaf = deaf
            if suppress is not None:
                state.suppress = suppress
            if priority_speaker is not None:
                state.prioritySpeaker = priority_speaker
            if comment is not None:
                state.comment = comment
            if name is not None:
                state.name = name
            self._srv().setState(state)

        self._call(run)

    def kick_user(self, session: int, reason: str) -> None:
        self._call(lambda: self._srv().kickUser(session, reason))

    def send_message(self, session: int, text: str) -> None:
        self._call(lambda: self._srv().sendMessage(session, text))

    def send_message_channel(self, channel_id: int, tree: bool, text: str) -> None:
        self._call(lambda: self._srv().sendMessageChannel(channel_id, tree, text))

    def set_bans(self, bans: Sequence[BanEntry]) -> None:
        """Ersetzt die gesamte Bannliste -- vorher ``get_bans`` und ergaenzen."""

        def run() -> None:
            items = []
            for ban in bans:
                item = MumbleServer.Ban()
                item.address = encode_address(ban.address)
                item.bits = ban.bits
                item.name = ban.name
                item.hash = ban.hash
                item.reason = ban.reason
                item.start = ban.start
                item.duration = ban.duration
                items.append(item)
            self._srv().setBans(items)

        self._call(run)

    def register_user(
        self,
        name: str,
        *,
        password: str | None = None,
        cert_hash: str | None = None,
        email: str | None = None,
        comment: str | None = None,
    ) -> int:
        """Legt einen registrierten Nutzer an und gibt dessen ID zurueck.

        Minimal noetig ist ``UserName``. Fuer die Anmeldung per Zertifikat wird
        zusaetzlich ``UserHash`` gesetzt -- das ist derselbe Hash, den murmur im
        Client als "Zertifikatshash" anzeigt.
        """

        def run() -> int:
            info = {MumbleServer.UserInfo.UserName: name}
            if password:
                info[MumbleServer.UserInfo.UserPassword] = password
            if cert_hash:
                info[MumbleServer.UserInfo.UserHash] = cert_hash
            if email:
                info[MumbleServer.UserInfo.UserEmail] = email
            if comment:
                info[MumbleServer.UserInfo.UserComment] = comment
            return int(self._srv().registerUser(info))

        return self._call(run)

    def update_registration(
        self,
        userid: int,
        *,
        name: str | None = None,
        password: str | None = None,
        cert_hash: str | None = None,
        email: str | None = None,
        comment: str | None = None,
    ) -> None:
        def run() -> None:
            info: dict[Any, str] = {}
            if name is not None:
                info[MumbleServer.UserInfo.UserName] = name
            if password is not None:
                info[MumbleServer.UserInfo.UserPassword] = password
            if cert_hash is not None:
                info[MumbleServer.UserInfo.UserHash] = cert_hash
            if email is not None:
                info[MumbleServer.UserInfo.UserEmail] = email
            if comment is not None:
                info[MumbleServer.UserInfo.UserComment] = comment
            if not info:
                return
            self._srv().updateRegistration(userid, info)

        self._call(run)

    def unregister_user(self, userid: int) -> None:
        self._call(lambda: self._srv().unregisterUser(userid))

    def add_user_to_group(self, channel_id: int, session: int, group: str) -> None:
        """Temporaere Mitgliedschaft -- ueberlebt keinen Serverneustart.

        Fuer dauerhafte Mitgliedschaft gehoert die Nutzer-ID in die ``add``-Liste
        der Gruppe und muss per ``setACL`` geschrieben werden. Der Provisioner
        macht ausschliesslich das.
        """
        self._call(lambda: self._srv().addUserToGroup(channel_id, session, group))

    def remove_user_from_group(self, channel_id: int, session: int, group: str) -> None:
        self._call(lambda: self._srv().removeUserFromGroup(channel_id, session, group))


class AsyncIceClient:
    """asyncio-Fassade um :class:`IceClient`.

    Ice ist synchron und bringt eigene Threads mit. Jeder Aufruf wandert in
    einen kleinen Threadpool, damit der Web-Loop nicht blockiert. Der Pool ist
    absichtlich klein: murmur serialisiert die meisten Aufrufe ohnehin ueber
    seinen Voice-Thread-Lock, mehr Parallelitaet bringt nur mehr Wartezeit.
    """

    def __init__(self, settings: Settings, max_workers: int = 4) -> None:
        self.sync = IceClient(settings)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ice"
        )

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Fuehrt eine Methode von :class:`IceClient` im Threadpool aus."""
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool, lambda: func(*args, **kwargs)
        )

    def __getattr__(self, item: str) -> Any:
        """Erlaubt ``await client.get_users()`` fuer jede IceClient-Methode."""
        attr = getattr(self.sync, item)
        if not callable(attr):
            return attr

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.run(attr, *args, **kwargs)

        return wrapper

    def shutdown(self) -> None:
        try:
            self.sync.close()
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
