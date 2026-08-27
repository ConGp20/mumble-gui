"""Ein murmur-Doppel, das echtes Ice spricht.

Warum
-----
Der Provisioner und der ACL-Editor haengen an Feinheiten, die man mit Attrappen
nicht trifft: dass ``setACL`` Gruppen *und* ACLs komplett ersetzt, dass
``getACL`` geerbte Eintraege mitliefert, dass Gruppenmitgliedschaft ueber
Nutzer-IDs laeuft, dass Listener an der Session haengen. Dieses Doppel bildet
genau diese Semantik nach und wird ueber einen echten Ice-Objektadapter
angeboten -- die Tests reden also mit demselben ``IceClient``-Code wie die
Produktion, inklusive Serialisierung und Proxy-Dispatch.

Es ersetzt **nicht** den Integrationstest gegen einen echten Server (siehe
``tests/test_integration_real_server.py``), es macht ihn nur seltener noetig.

Nachgebildet ist das Verhalten aus:
  * ``src/murmur/MumbleServerIce.cpp``  (impl_Server_*)
  * ``src/ACL.cpp`` / ``src/Group.cpp``  (Vererbung)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import Ice  # type: ignore[import-not-found]
import MumbleServer  # type: ignore[import-not-found]

#: Entspricht ``ChanACL::All`` in src/ACL.h -- setACL maskiert dagegen.
ALL_PERMISSIONS = 0x1F0FFF


@dataclass
class _Channel:
    id: int
    name: str
    parent: int
    description: str = ""
    temporary: bool = False
    position: int = 0
    links: list[int] = field(default_factory=list)
    inherit_acl: bool = True
    acls: list[Any] = field(default_factory=list)
    groups: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Registered:
    userid: int
    name: str
    info: dict[Any, str] = field(default_factory=dict)


class FakeServer(MumbleServer.Server):  # type: ignore[misc, name-defined]
    """Der virtuelle Server. Thread-sicher ueber ein grobes Lock, wie murmur."""

    def __init__(self, server_id: int = 1) -> None:
        self._id = server_id
        self._lock = threading.RLock()
        self._started = time.time()

        root = _Channel(id=0, name="Root", parent=-1)
        self.channels: dict[int, _Channel] = {0: root}
        self._next_channel_id = 1

        self.users: dict[int, Any] = {}
        self._next_session = 1

        self.registered: dict[int, _Registered] = {}
        self._next_userid = 1

        self.conf: dict[str, str] = {}
        self.bans: list[Any] = []
        self.log: list[Any] = []
        self.callbacks: list[Any] = []
        #: session -> Menge von Kanal-IDs. Bewusst an der Session, nicht am
        #: Nutzer -- genau wie m_channelListenerManager in murmur.
        self.listening: dict[int, set[int]] = {}
        self.superuser_password: str | None = None

    # -- Testhilfen (kein Teil der Ice-Schnittstelle) -----------------------

    def _log(self, text: str) -> None:
        entry = MumbleServer.LogEntry()
        entry.timestamp = int(time.time())
        entry.txt = text
        self.log.insert(0, entry)

    def connect_user(
        self,
        name: str,
        *,
        userid: int = -1,
        channel: int = 0,
        address: str = "10.20.10.5",
        release: str = "1.5.735",
        os_name: str = "Linux",
        tcp_only: bool = False,
        udp_ping: float = 12.5,
    ) -> int:
        """Simuliert einen Client-Login und feuert ``userConnected``."""
        import ipaddress

        with self._lock:
            session = self._next_session
            self._next_session += 1
            user = MumbleServer.User()
            user.session = session
            user.userid = userid
            user.name = name
            user.channel = channel
            user.mute = user.deaf = user.suppress = False
            user.selfMute = user.selfDeaf = False
            user.prioritySpeaker = user.recording = False
            user.onlinesecs = 0
            user.idlesecs = 0
            user.bytespersec = 0
            user.version = 0x010500
            user.version2 = (1 << 32) | (5 << 16) | 735
            user.release = release
            user.os = os_name
            user.osversion = "6.1"
            user.identity = ""
            user.context = ""
            user.comment = ""
            parsed = ipaddress.ip_address(address)
            if isinstance(parsed, ipaddress.IPv4Address):
                parsed = ipaddress.IPv6Address("::ffff:" + str(parsed))
            user.address = tuple(parsed.packed)
            user.tcponly = tcp_only
            user.udpPing = udp_ping
            user.tcpPing = udp_ping + 3.0
            self.users[session] = user
            self.listening[session] = set()
            self._log(f"<{session}:{name}> Authenticated")
        self._fire("userConnected", user)
        return session

    def disconnect_user(self, session: int) -> None:
        with self._lock:
            user = self.users.pop(session, None)
            self.listening.pop(session, None)
        if user is not None:
            self._fire("userDisconnected", user)

    def _fire(self, event: str, *args: Any) -> None:
        for callback in list(self.callbacks):
            try:
                getattr(callback, event)(*args)
            except Exception:  # noqa: BLE001 - murmur meldet den Callback ab
                try:
                    self.callbacks.remove(callback)
                except ValueError:
                    pass

    def _ancestors(self, channel_id: int) -> list[_Channel]:
        """Wurzel zuerst, der Kanal selbst nicht enthalten."""
        chain: list[_Channel] = []
        current = self.channels.get(channel_id)
        while current is not None and current.parent >= 0:
            parent = self.channels.get(current.parent)
            if parent is None:
                break
            chain.append(parent)
            current = parent
        chain.reverse()
        return chain

    # -- Ice: Meta-nahe ------------------------------------------------------

    def id(self, current: Any = None) -> int:
        return self._id

    def isRunning(self, current: Any = None) -> bool:
        return True

    def start(self, current: Any = None) -> None:
        return None

    def stop(self, current: Any = None) -> None:
        return None

    def delete(self, current: Any = None) -> None:
        return None

    def getUptime(self, current: Any = None) -> int:
        return int(time.time() - self._started)

    # -- Ice: Callbacks ------------------------------------------------------

    def addCallback(self, cb: Any, current: Any = None) -> None:
        self.callbacks.append(cb)

    def removeCallback(self, cb: Any, current: Any = None) -> None:
        try:
            self.callbacks.remove(cb)
        except ValueError:
            pass

    def setAuthenticator(self, auth: Any, current: Any = None) -> None:
        return None

    def addContextCallback(
        self, session: int, action: str, text: str, cb: Any, ctx: int, current: Any = None
    ) -> None:
        return None

    def removeContextCallback(self, cb: Any, current: Any = None) -> None:
        return None

    # -- Ice: Konfiguration --------------------------------------------------

    def getConf(self, key: str, current: Any = None) -> str:
        if key in {"icesecretwrite", "icesecretread"}:
            raise MumbleServer.WriteOnlyException()
        return self.conf.get(key, "")

    def getAllConf(self, current: Any = None) -> dict[str, str]:
        return dict(self.conf)

    def setConf(self, key: str, value: str, current: Any = None) -> None:
        with self._lock:
            self.conf[key] = value
        self._log(f"Configuration {key} changed")

    def setSuperuserPassword(self, pw: str, current: Any = None) -> None:
        self.superuser_password = pw

    # -- Ice: Log ------------------------------------------------------------

    def getLog(self, first: int, last: int, current: Any = None) -> list[Any]:
        return self.log[first:last]

    def getLogLen(self, current: Any = None) -> int:
        return len(self.log)

    # -- Ice: Nutzer ---------------------------------------------------------

    def getUsers(self, current: Any = None) -> dict[int, Any]:
        return dict(self.users)

    def getState(self, session: int, current: Any = None) -> Any:
        try:
            return self.users[session]
        except KeyError:
            raise MumbleServer.InvalidSessionException() from None

    def setState(self, state: Any, current: Any = None) -> None:
        with self._lock:
            if state.session not in self.users:
                raise MumbleServer.InvalidSessionException()
            if state.channel not in self.channels:
                raise MumbleServer.InvalidChannelException()
            stored = self.users[state.session]
            # recording ist laut Slice read-only.
            recording = stored.recording
            self.users[state.session] = state
            state.recording = recording
        self._fire("userStateChanged", state)

    def kickUser(self, session: int, reason: str, current: Any = None) -> None:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        self._log(f"Kicked <{session}>: {reason}")
        self.disconnect_user(session)

    def sendMessage(self, session: int, text: str, current: Any = None) -> None:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()

    def sendMessageChannel(
        self, channelid: int, tree: bool, text: str, current: Any = None
    ) -> None:
        if channelid not in self.channels:
            raise MumbleServer.InvalidChannelException()

    def getCertificateList(self, session: int, current: Any = None) -> list[Any]:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        return []

    def hasPermission(
        self, session: int, channelid: int, perm: int, current: Any = None
    ) -> bool:
        return bool(self.effectivePermissions(session, channelid) & perm)

    def effectivePermissions(
        self, session: int, channelid: int, current: Any = None
    ) -> int:
        """Stark vereinfacht: Rechte aus den ACLs der Kette, @all und Gruppen.

        Reicht, um den Simulator im ACL-Editor zu testen; die echte Auswertung
        in ACL.cpp kennt zusaetzlich Token und Kontextgruppen.
        """
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        if channelid not in self.channels:
            raise MumbleServer.InvalidChannelException()
        user = self.users[session]
        acls, groups, _ = self.getACL(channelid)
        member_of = {g.name for g in groups if user.userid in g.members}
        member_of.add("all")
        if user.userid >= 0:
            member_of.add("auth")
        allowed = 0
        for acl in acls:
            if not acl.applyHere:
                continue
            applies = acl.userid == user.userid or (
                acl.userid < 0 and acl.group in member_of
            )
            if not applies:
                continue
            allowed |= acl.allow
            allowed &= ~acl.deny
        if allowed & 0x01:  # Write impliziert alles ausser Speak
            allowed |= ALL_PERMISSIONS & ~0x08
        return allowed

    # -- Ice: Kanaele --------------------------------------------------------

    def getChannels(self, current: Any = None) -> dict[int, Any]:
        return {cid: self._to_ice_channel(c) for cid, c in self.channels.items()}

    def _to_ice_channel(self, channel: _Channel) -> Any:
        item = MumbleServer.Channel()
        item.id = channel.id
        item.name = channel.name
        item.parent = channel.parent
        item.description = channel.description
        item.temporary = channel.temporary
        item.position = channel.position
        item.links = list(channel.links)
        return item

    def getChannelState(self, channelid: int, current: Any = None) -> Any:
        try:
            return self._to_ice_channel(self.channels[channelid])
        except KeyError:
            raise MumbleServer.InvalidChannelException() from None

    def setChannelState(self, state: Any, current: Any = None) -> None:
        with self._lock:
            if state.id not in self.channels:
                raise MumbleServer.InvalidChannelException()
            if state.parent not in self.channels and state.id != 0:
                raise MumbleServer.InvalidChannelException()
            # Ein Kanal darf nicht unter sich selbst haengen.
            walker = state.parent
            while walker >= 0:
                if walker == state.id:
                    raise MumbleServer.InvalidChannelException()
                walker = self.channels[walker].parent
            channel = self.channels[state.id]
            channel.name = state.name
            channel.parent = state.parent
            channel.description = state.description
            channel.position = state.position
            channel.links = list(state.links)
        self._fire("channelStateChanged", self._to_ice_channel(channel))

    def addChannel(self, name: str, parent: int, current: Any = None) -> int:
        with self._lock:
            if parent not in self.channels:
                raise MumbleServer.InvalidChannelException()
            for existing in self.channels.values():
                if existing.parent == parent and existing.name == name:
                    # murmur laesst keine zwei gleichnamigen Geschwister zu.
                    raise MumbleServer.InvalidChannelException()
            channel_id = self._next_channel_id
            self._next_channel_id += 1
            self.channels[channel_id] = _Channel(id=channel_id, name=name, parent=parent)
        self._fire("channelCreated", self._to_ice_channel(self.channels[channel_id]))
        return channel_id

    def removeChannel(self, channelid: int, current: Any = None) -> None:
        with self._lock:
            if channelid not in self.channels or channelid == 0:
                raise MumbleServer.InvalidChannelException()
            doomed = [channelid]
            index = 0
            while index < len(doomed):
                current_id = doomed[index]
                doomed.extend(
                    c.id for c in self.channels.values() if c.parent == current_id
                )
                index += 1
            removed = [self._to_ice_channel(self.channels[c]) for c in doomed]
            for cid in doomed:
                self.channels.pop(cid, None)
        for channel in removed:
            self._fire("channelRemoved", channel)

    def getTree(self, current: Any = None) -> Any:
        def build(channel_id: int) -> Any:
            node = MumbleServer.Tree()
            node.c = self._to_ice_channel(self.channels[channel_id])
            node.users = [u for u in self.users.values() if u.channel == channel_id]
            node.children = [
                build(c.id)
                for c in sorted(
                    (c for c in self.channels.values() if c.parent == channel_id),
                    key=lambda c: (c.position, c.name),
                )
            ]
            return node

        return build(0)

    # -- Ice: ACL ------------------------------------------------------------

    def getACL(self, channelid: int, current: Any = None) -> tuple[Any, Any, bool]:
        """Eigene und geerbte Eintraege, geerbte zuerst und markiert."""
        with self._lock:
            if channelid not in self.channels:
                raise MumbleServer.InvalidChannelException()
            channel = self.channels[channelid]

            acls: list[Any] = []
            groups: dict[str, Any] = {}

            if channel.inherit_acl:
                for ancestor in self._ancestors(channelid):
                    for acl in ancestor.acls:
                        if not acl.applySubs:
                            continue
                        copy = MumbleServer.ACL()
                        copy.applyHere = True
                        copy.applySubs = acl.applySubs
                        copy.inherited = True
                        copy.userid = acl.userid
                        copy.group = acl.group
                        copy.allow = acl.allow
                        copy.deny = acl.deny
                        acls.append(copy)
                    for name, group in ancestor.groups.items():
                        if not group.inheritable:
                            continue
                        copy = MumbleServer.Group()
                        copy.name = name
                        copy.inherited = True
                        copy.inherit = group.inherit
                        copy.inheritable = group.inheritable
                        copy.add = list(group.add)
                        copy.remove = list(group.remove)
                        copy.members = list(group.add)
                        groups[name] = copy

            for acl in channel.acls:
                copy = MumbleServer.ACL()
                copy.applyHere = acl.applyHere
                copy.applySubs = acl.applySubs
                copy.inherited = False
                copy.userid = acl.userid
                copy.group = acl.group
                copy.allow = acl.allow
                copy.deny = acl.deny
                acls.append(copy)

            for name, group in channel.groups.items():
                inherited_members: list[int] = []
                if group.inherit and name in groups:
                    inherited_members = list(groups[name].members)
                copy = MumbleServer.Group()
                copy.name = name
                copy.inherited = False
                copy.inherit = group.inherit
                copy.inheritable = group.inheritable
                copy.add = list(group.add)
                copy.remove = list(group.remove)
                members = set(inherited_members) | set(group.add)
                members -= set(group.remove)
                copy.members = sorted(members)
                groups[name] = copy

            return acls, list(groups.values()), channel.inherit_acl

    def setACL(
        self,
        channelid: int,
        acls: list[Any],
        groups: list[Any],
        inherit: bool,
        current: Any = None,
    ) -> None:
        """Ersetzt ACLs und Gruppen vollstaendig, wie impl_Server_setACL."""
        with self._lock:
            if channelid not in self.channels:
                raise MumbleServer.InvalidChannelException()
            channel = self.channels[channelid]
            channel.inherit_acl = inherit
            channel.acls = []
            channel.groups = {}
            for group in groups:
                copy = MumbleServer.Group()
                copy.name = group.name
                copy.inherited = False
                copy.inherit = group.inherit
                copy.inheritable = group.inheritable
                copy.add = sorted(set(group.add))
                copy.remove = sorted(set(group.remove))
                copy.members = []
                channel.groups[group.name] = copy
            for acl in acls:
                copy = MumbleServer.ACL()
                copy.applyHere = acl.applyHere
                copy.applySubs = acl.applySubs
                copy.inherited = False
                copy.userid = acl.userid
                copy.group = acl.group
                # Genau wie murmur: gegen ChanACL::All maskieren.
                copy.allow = acl.allow & ALL_PERMISSIONS
                copy.deny = acl.deny & ALL_PERMISSIONS
                channel.acls.append(copy)
        self._log(f"ACL for channel {channelid} updated")

    def addUserToGroup(
        self, channelid: int, session: int, group: str, current: Any = None
    ) -> None:
        # Temporaer und nicht persistent -- hier bewusst ohne Wirkung auf setACL.
        if channelid not in self.channels:
            raise MumbleServer.InvalidChannelException()
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()

    def removeUserFromGroup(
        self, channelid: int, session: int, group: str, current: Any = None
    ) -> None:
        if channelid not in self.channels:
            raise MumbleServer.InvalidChannelException()
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()

    def redirectWhisperGroup(
        self, session: int, source: str, target: str, current: Any = None
    ) -> None:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()

    # -- Ice: Registrierung --------------------------------------------------

    def registerUser(self, info: dict[Any, str], current: Any = None) -> int:
        name = info.get(MumbleServer.UserInfo.UserName, "")
        if not name:
            raise MumbleServer.InvalidUserException()
        with self._lock:
            for entry in self.registered.values():
                if entry.name.lower() == name.lower():
                    raise MumbleServer.InvalidUserException()
            userid = self._next_userid
            self._next_userid += 1
            self.registered[userid] = _Registered(userid=userid, name=name, info=dict(info))
        return userid

    def unregisterUser(self, userid: int, current: Any = None) -> None:
        with self._lock:
            if userid not in self.registered:
                raise MumbleServer.InvalidUserException()
            del self.registered[userid]

    def updateRegistration(
        self, userid: int, info: dict[Any, str], current: Any = None
    ) -> None:
        with self._lock:
            if userid not in self.registered:
                raise MumbleServer.InvalidUserException()
            entry = self.registered[userid]
            entry.info.update(info)
            if MumbleServer.UserInfo.UserName in info:
                entry.name = info[MumbleServer.UserInfo.UserName]

    def getRegistration(self, userid: int, current: Any = None) -> dict[Any, str]:
        try:
            entry = self.registered[userid]
        except KeyError:
            raise MumbleServer.InvalidUserException() from None
        info = dict(entry.info)
        info[MumbleServer.UserInfo.UserName] = entry.name
        info.pop(MumbleServer.UserInfo.UserPassword, None)
        return info

    def getRegisteredUsers(self, filter: str, current: Any = None) -> dict[int, str]:
        needle = filter.lower()
        return {
            uid: entry.name
            for uid, entry in self.registered.items()
            if not needle or needle in entry.name.lower()
        }

    def verifyPassword(self, name: str, pw: str, current: Any = None) -> int:
        for entry in self.registered.values():
            if entry.name == name:
                stored = entry.info.get(MumbleServer.UserInfo.UserPassword)
                return entry.userid if stored == pw else -1
        return -2

    def getUserNames(self, ids: list[int], current: Any = None) -> dict[int, str]:
        return {uid: self.registered[uid].name if uid in self.registered else "" for uid in ids}

    def getUserIds(self, names: list[str], current: Any = None) -> dict[str, int]:
        lookup = {entry.name.lower(): entry.userid for entry in self.registered.values()}
        return {name: lookup.get(name.lower(), -1) for name in names}

    def getTexture(self, userid: int, current: Any = None) -> list[int]:
        return []

    def setTexture(self, userid: int, tex: list[int], current: Any = None) -> None:
        return None

    def updateCertificate(
        self, certificate: str, privateKey: str, passphrase: str, current: Any = None
    ) -> None:
        return None

    # -- Ice: Bans -----------------------------------------------------------

    def getBans(self, current: Any = None) -> list[Any]:
        return list(self.bans)

    def setBans(self, bans: list[Any], current: Any = None) -> None:
        self.bans = list(bans)

    # -- Ice: Channel Listener ----------------------------------------------
    # Der erste Parameter ist eine SESSION, nicht die Nutzer-ID -- siehe
    # impl_Server_startListening in MumbleServerIce.cpp (NEED_PLAYER).

    def startListening(self, session: int, channelid: int, current: Any = None) -> None:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        if channelid not in self.channels:
            raise MumbleServer.InvalidChannelException()
        self.listening.setdefault(session, set()).add(channelid)

    def stopListening(self, session: int, channelid: int, current: Any = None) -> None:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        self.listening.setdefault(session, set()).discard(channelid)

    def isListening(self, session: int, channelid: int, current: Any = None) -> bool:
        return channelid in self.listening.get(session, set())

    def getListeningChannels(self, session: int, current: Any = None) -> list[int]:
        if session not in self.users:
            raise MumbleServer.InvalidSessionException()
        return sorted(self.listening.get(session, set()))

    def getListeningUsers(self, channelid: int, current: Any = None) -> list[int]:
        return sorted(s for s, chans in self.listening.items() if channelid in chans)

    def getListenerVolumeAdjustment(
        self, channelid: int, session: int, current: Any = None
    ) -> float:
        return 1.0

    def setListenerVolumeAdjustment(
        self, channelid: int, session: int, volumeAdjustment: float, current: Any = None
    ) -> None:
        return None

    def sendWelcomeMessage(self, receiverUserIDs: list[int], current: Any = None) -> None:
        return None


class FakeMeta(MumbleServer.Meta):  # type: ignore[misc, name-defined]
    """``Meta`` mit genau einem virtuellen Server."""

    def __init__(self, server_proxy: Any, server: FakeServer, version: tuple[int, int, int, str]) -> None:
        self._proxy = server_proxy
        self._server = server
        self._version = version
        self._started = time.time()

    def getServer(self, id: int, current: Any = None) -> Any:
        return self._proxy if id == self._server.id() else None

    def newServer(self, current: Any = None) -> Any:
        raise MumbleServer.InvalidSecretException()

    def getBootedServers(self, current: Any = None) -> list[Any]:
        return [self._proxy]

    def getAllServers(self, current: Any = None) -> list[Any]:
        return [self._proxy]

    def getDefaultConf(self, current: Any = None) -> dict[str, str]:
        return {
            "welcometext": "Welcome to Mumble.",
            "port": "64738",
            "users": "100",
            "bandwidth": "72000",
            "timeout": "30",
            "textmessagelength": "5000",
            "allowhtml": "true",
            "defaultchannel": "0",
        }

    def getVersion(self, current: Any = None) -> tuple[int, int, int, str]:
        return self._version

    def addCallback(self, cb: Any, current: Any = None) -> None:
        return None

    def removeCallback(self, cb: Any, current: Any = None) -> None:
        return None

    def getUptime(self, current: Any = None) -> int:
        return int(time.time() - self._started)

    def getSlice(self, current: Any = None) -> str:
        return ""

    def getSliceChecksums(self, current: Any = None) -> dict[str, str]:
        # Die eigenen Pruefsummen: gleiche Slice -> keine Warnung.
        return dict(Ice.sliceChecksums)


class FakeMurmur:
    """Startet das Doppel auf einem freien Loopback-Port.

    Benutzung::

        with FakeMurmur() as fake:
            settings = fake.settings()
            client = IceClient(settings)
            client.connect()
    """

    def __init__(
        self,
        secret: str = "test-secret",
        version: tuple[int, int, int, str] = (1, 5, 735, "1.5.735"),
    ) -> None:
        self.secret = secret
        self.version = version
        self.server = FakeServer()
        self._communicator: Any = None
        self._adapter: Any = None
        self.port = 0

    def __enter__(self) -> "FakeMurmur":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    def start(self) -> "FakeMurmur":
        props = Ice.createProperties()
        props.setProperty("FakeMurmur.Endpoints", "tcp -h 127.0.0.1 -p 0")
        props.setProperty("Ice.MessageSizeMax", "8192")
        props.setProperty("Ice.Warn.Connections", "0")
        init_data = Ice.InitializationData()
        init_data.properties = props
        self._communicator = Ice.initialize(init_data)

        adapter = self._communicator.createObjectAdapter("FakeMurmur")
        server_proxy = MumbleServer.ServerPrx.uncheckedCast(
            adapter.add(self.server, Ice.stringToIdentity("s/1"))
        )
        meta = FakeMeta(server_proxy, self.server, self.version)
        meta_proxy = adapter.add(meta, Ice.stringToIdentity("Meta"))
        adapter.activate()
        self._adapter = adapter

        # Den tatsaechlich vergebenen Port aus dem Endpunkt herausziehen.
        endpoint = str(meta_proxy.ice_getEndpoints()[0])
        for token in endpoint.split():
            if token.startswith("-p"):
                continue
        parts = endpoint.split("-p ")
        self.port = int(parts[1].split()[0])
        return self

    def stop(self) -> None:
        if self._communicator is not None:
            try:
                self._communicator.destroy()
            finally:
                self._communicator = None
                self._adapter = None

    def settings(self, **overrides: Any) -> Any:
        """Passende :class:`~intercom.config.Settings` fuer dieses Doppel."""
        from pathlib import Path

        from intercom.config import Settings

        defaults: dict[str, Any] = dict(
            ice_host="127.0.0.1",
            ice_port=self.port,
            ice_secret=self.secret,
            ice_server_id=1,
            listen_host="127.0.0.1",
            listen_port=8080,
            admin_user="admin",
            admin_password="admin",
            readonly_user=None,
            readonly_password=None,
            session_secret="test" * 8,
            intercom_config=Path("intercom.yaml"),
            provision_on_start=False,
            provision_prune=False,
            monitor_enabled=False,
            monitor_name="monitor",
            monitor_channel="Intercom/Regie",
            monitor_password=None,
            monitor_cert=Path("/data/monitor-cert.pem"),
            monitor_stats_interval_ms=5000,
            mumble_port=64738,
            poll_interval_ms=2000,
            history_retention_hours=48,
            alert_ping_ms=80.0,
            alert_loss_pct=2.0,
            log_level="INFO",
            data_dir=Path("/tmp"),
            slice_dir=Path("/tmp"),
            expected_mumble_version=f"v{self.version[0]}.{self.version[1]}.{self.version[2]}",
            warnings=(),
        )
        defaults.update(overrides)
        return Settings(**defaults)
