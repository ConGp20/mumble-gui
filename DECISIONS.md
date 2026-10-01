# Entscheidungen

Warum etwas so gebaut ist, wie es gebaut ist. Jede Entscheidung nennt das
Problem, die gewaehlte Loesung und was dagegen sprach.

---

## D-001 – `docker-compose.yml`, `.env` und `intercom.yaml` wurden neu erstellt

**Problem.** Der Auftrag verweist auf drei bereits vorhandene Dateien in
`/volume1/docker/stadion-intercom/`, die den Vertrag definieren. Das
Repository `ConGp20/mumble-gui` war zum Start dieser Arbeit **vollstaendig
leer** – weder lokal noch auf dem Remote lag ein Commit, geschweige denn eine
der drei Dateien.

**Entscheidung.** Die drei Dateien wurden aus der Aufgabenbeschreibung
rekonstruiert. Sie nennt jede Umgebungsvariable (`ICE_HOST`, `ICE_PORT`,
`ICE_SECRET`, `ICE_SERVER_ID`, `POLL_INTERVAL_MS`, `ALERT_PING_MS`,
`ALERT_LOSS_PCT`, `MONITOR_BOT_NAME`, `HISTORY_RETENTION_HOURS`, `ADMIN_USER`,
`ADMIN_PASSWORD`, `SESSION_SECRET`, `LISTEN_PORT`, `PROVISION_ON_START`,
`PROVISION_PRUNE`, `MUMBLE_VERSION`), jeden Pfad (`/data/history.sqlite`,
`./server`, `./admin-data`), jeden Port (6502), das Compose-Profil `gui`, den
Servicenamen `mumble-admin` mit `network_mode: host` und das YAML-Schema.

**Konsequenz.** Existieren auf dem NAS bereits eigene Fassungen, gilt dort
deren Inhalt. Abzugleichen sind dann nur die Variablennamen aus `.env.example`
und das Schema aus `README.md`. Der Code liest **ausschliesslich** die in
`admin/intercom/config.py` aufgefuehrten Variablen – diese Datei ist der
Vertrag.

**Nachtrag.** Zwei der hier genannten Vorgaben gibt es nicht mehr: das
Compose-Profil `gui` (mit ihm beendete `docker compose down` den Admin-Container
nicht; `admin/tests/test_compose.py` haelt fest, dass kein Dienst an einem
Profil haengt) und die `intercom.yaml` als eingebundene Datei (D-034).

---

## D-002 – Beide Container laufen im Host-Netz

**Problem.** Ice-Callbacks (`Server.addCallback`) sind kein Polling: murmur
baut eine Verbindung **zum Admin-Prozess** auf. Der Admin-Prozess muss also
einen eigenen Ice-Objektadapter betreiben, den murmur erreichen kann.

**Entscheidung.** `mumble-server` **und** `mumble-admin` laufen mit
`network_mode: host`. Der Admin-Adapter bindet auf `127.0.0.1` mit Port `0`
(Ice waehlt einen freien Port).

**Verworfen.** Nur `mumble-admin` im Host-Netz und den Server in einem
Bridge-Netz zu lassen, funktioniert **nicht**: `127.0.0.1` waere aus Sicht des
Servers sein eigener Netzwerk-Namespace, der Rueckruf ginge ins Leere. Ein
Adapter auf `0.0.0.0` mit der Docker-Bridge-IP waere moeglich, macht die
Admin-Schnittstelle aber ohne Not von aussen erreichbar.

---

## D-003 – `Listen` (0x800) wird selbst definiert, nicht aus der Slice gelesen

**Problem.** `channels[].listen_for` soll auf das Recht „Mithoeren" abgebildet
werden. Die Slice-Datei von Mumble 1.5.857 deklariert dafuer **keine
Konstante** – es gibt kein `PermissionListen`.

**Befund.** Der Server kennt das Recht sehr wohl. `src/ACL.h`:

```
MakeTempChannel = 0x400,
Listen          = 0x800,
...
All = Write + Traverse + ... + Listen + ... + ResetUserContent
```

und `impl_Server_setACL` in `src/murmur/MumbleServerIce.cpp` maskiert die per
Ice uebergebenen Bits lediglich gegen `ChanACL::All`:

```cpp
acl->pDeny  = static_cast<ChanACL::Permissions>(ai.deny)  & ChanACL::All;
acl->pAllow = static_cast<ChanACL::Permissions>(ai.allow) & ChanACL::All;
```

`ChanACL::All` enthaelt `Listen`. Das Bit ueberlebt die Maskierung.

**Entscheidung.** `admin/intercom/ice/permissions.py` fuehrt die Bit-Tabelle
selbst, inklusive `Listen = 0x800`. `verify_against_slice()` vergleicht beim
Start alle Werte mit der einkompilierten Slice und meldet Abweichungen als
Banner. Der Test `test_acl_schreiben_und_lesen` belegt, dass 0x800 einen
`setACL`/`getACL`-Umlauf uebersteht.

**Konsequenz.** `listen_for` ist voll funktionsfaehig und muss **nicht**, wie
im Auftrag vermutet, auf Anzeige und Erinnerung reduziert werden.

Nebenbefund, ebenfalls in der Tabelle korrigiert: die Slice heisst
`PermissionRegisterSelf` (nicht `PermissionSelfRegister`) und
`ResetUserContent` (ohne `Permission`-Praefix).

---

## D-004 – Channel-Listener werden zur Laufzeit gesetzt, nicht provisioniert

**Problem.** `channels[].listen_to` soll dafuer sorgen, dass Mitglieder der
`speak`-Gruppen eines Kanals zusaetzlich bestimmte andere Kanaele mithoeren.
Der Auftrag vermutete, das gehe serverseitig womoeglich gar nicht.

**Befund.** Es geht – die Slice hat `startListening`, `stopListening`,
`isListening`, `getListeningChannels`, `getListeningUsers`. **Aber**: die
Slice-Dokumentation nennt den ersten Parameter „The ID of the user", und das
ist falsch. Die Implementierung nennt ihn `session` und loest ihn ueber
`NEED_PLAYER` als Sitzung auf:

```cpp
static void impl_Server_startListening(..., int session, int channelid) {
    NEED_SERVER; NEED_CHANNEL; NEED_PLAYER;
    server->startListeningToChannel(user, channel);
```

`getListeningUsers` gibt entsprechend **Session-IDs** zurueck, keine Nutzer-IDs.

**Entscheidung.** Listener sind Sitzungszustand und ueberleben keinen
Reconnect. Der Provisioner schreibt sie deshalb nicht in den Soll-Zustand,
sondern der Laufzeit-Abgleich setzt sie per Callback bei `userConnected` und
`userStateChanged` – genau wie beim Priority Speaker. Im Cockpit steht pro
Nutzer Soll (aus `listen_to`) gegen Ist (`getListeningChannels`); eine
Abweichung wird rot markiert.

`test_listener_haengen_an_der_session` haelt das Verhalten fest.

---

## D-005 – Gruppenmitgliedschaft nur ueber `setACL`, nie ueber `addUserToGroup`

**Problem.** Es gibt zwei Wege, jemanden in eine Gruppe zu bekommen.

**Befund.** `addUserToGroup` ist laut Slice ausdruecklich temporaer („This state
is not saved, and is intended for temporary memberships") und nimmt eine
**Session**. Dauerhafte Mitgliedschaft steht in der `add`-Liste einer Gruppe
und wird ueber `setACL` geschrieben; dort sind es **registrierte Nutzer-IDs**.
`impl_Server_setACL` uebernimmt `gi.add`/`gi.remove` unveraendert nach
`Group::qsAdd`/`qsRemove`.

**Entscheidung.** Der Provisioner benutzt ausschliesslich `setACL`. Ein Name
aus `users:`, der nicht registriert ist, wird nicht stillschweigend
uebersprungen, sondern landet als Eintrag im Provisioning-Report.
`addUserToGroup` bleibt fuer die eine Stelle reserviert, an der es passt: eine
befristete Berechtigung waehrend der Veranstaltung, ueber das GUI.

---

## D-006 – Geerbte ACL-Eintraege werden vor dem Schreiben entfernt

**Problem.** `getACL` liefert geerbte Eintraege der Elternkanaele mit
(`inherited = true`). `setACL` ersetzt ACLs und Gruppen des Kanals
**vollstaendig**.

**Befund.** `impl_Server_setACL` wertet `inherited` beim Schreiben nicht aus.
Wer eine gelesene ACL-Liste unveraendert zurueckschreibt, legt damit alle
geerbten Eintraege als *eigene* Eintraege des Kanals an und friert die
Vererbung ein – die Aenderung im Elternkanal wirkt danach nicht mehr.

**Entscheidung.** `IceClient.set_channel_acl` filtert `inherited`-Eintraege
und -Gruppen grundsaetzlich heraus. Jeder schreibende Pfad geht durch diese
Methode. `test_geerbte_eintraege_werden_beim_schreiben_gefiltert` sichert das ab.

---

## D-007 – Versionsabgleich ueber Slice-Pruefsummen statt nur ueber die Versionsnummer

**Problem.** Der Auftrag verlangt, `Meta.getVersion()` gegen die einkompilierte
Version zu halten. Das erkennt aber keinen Schnittstellenbruch innerhalb
derselben Versionsnummer.

**Entscheidung.** Zusaetzlich zum Versionsvergleich werden die
Slice-Pruefsummen verglichen: `Meta.getSliceChecksums()` liefert die Summen der
Serverfassung, `Ice.sliceChecksums` die unserer. `slice2py` fuellt letztere nur
mit dem Schalter `--checksum` – der steht in `admin/scripts/build_slice.sh`.
Beides zusammen landet als Banner im Cockpit und als Warnung im Log.

**Bewusst kein Abbruch.** Ein Versionsunterschied macht die meisten Aufrufe
noch nicht kaputt. Ein Container, der deswegen nicht startet, hilft waehrend
einer laufenden Veranstaltung niemandem.

---

## D-008 – `zeroc-ice` wird im Image aus dem Quelltext gebaut

**Problem.** Der Auftrag gibt Python 3.11 vor und weist darauf hin, dass es
fuer neuere Python-Versionen keine verlaesslichen Wheels gibt.

**Befund, gemessen statt vermutet.**

* PyPI liefert fuer `zeroc-ice==3.7.11` **kein** Wheel fuer CPython 3.11 – nur
  das Quell-Archiv. `pip download zeroc-ice` zieht `zeroc_ice-3.7.11.tar.gz`.
* Das Ubuntu-Paket `python3-zeroc-ice` (24.04, `3.7.10-2.1build1`) ist gegen
  **Python 3.12** gebaut (`Depends: python3 (<< 3.13), python3 (>= 3.12~)`) und
  damit unter 3.11 unbrauchbar.
* Der Bau aus dem Quelltext funktioniert: mit `build-essential`, `libssl-dev`
  und `libbz2-dev` uebersetzt `pip install --no-binary :all: zeroc-ice==3.7.11`
  unter Python 3.11 fehlerfrei. `slice2py` landet danach in `/usr/local/bin`.

**Ueberholt von D-027** -- der Quelltext-Bau ist einem fertigen
Debian-Paket gewichen. Die Begruendung unten bleibt als Beleg dafuer stehen,
warum ein Wheel nicht in Frage kam.

**Entscheidung.** Das Dockerfile baut `zeroc-ice` in einer Builder-Stufe aus
dem Quelltext und kopiert nur das Ergebnis ins Laufzeit-Image. Das kostet beim
ersten Bau einige Minuten, ist dafuer unabhaengig von Distributionspaketen und
an Python 3.11 gebunden.

---

## D-009 – Ein Prozess mit asyncio-Tasks statt `supervisord`

**Problem.** Web-Server, Ice-Callbacks, Polling, Monitor-Bot und
Verlaufs-Aufraeumen laufen nebeneinander.

**Entscheidung.** Ein einziger Prozess. `uvicorn` betreibt den asyncio-Loop,
Polling und Aufraeumen sind asyncio-Tasks. Ice und pymumble bringen eigene
Threads mit; deren Ereignisse werden per `call_soon_threadsafe` in den Loop
gehoben, Ice-Aufrufe laufen ueber einen kleinen Threadpool
(`AsyncIceClient`), damit der Loop nie blockiert.

**Verworfen.** `supervisord` haette einen zweiten Prozess fuer den Monitor-Bot
erlaubt, aber der Bot muss seine Messwerte in dieselbe SQLite schreiben und im
Cockpit sofort sichtbar sein. Zwei Prozesse haetten dafuer IPC gebraucht –
Aufwand ohne Gegenwert. Ein Absturz des Bots darf das GUI nicht mitreissen;
das loest ein Supervisor-Task im Loop mit Backoff.

---

## D-010 – Integrationstests gegen einen echten Server sind vorhanden, liefen hier aber nicht

**Problem.** Der Auftrag verlangt Tests gegen einen echten Mumble-Server im
Docker.

**Befund.** In der Umgebung, in der dieser Code entstanden ist, ist der
Docker-Daemon zwar startbar, der Zugriff auf die Docker-Hub-Layer
(`production.cloudfront.docker.com`) wird aber von der Egress-Richtlinie mit
`403` blockiert. Ein `mumblevoip/mumble-server`-Image liess sich daher nicht
laden.

**Entscheidung.** Zwei Ebenen:

1. `admin/tests/fake_murmur.py` ist ein murmur-Doppel, das **echtes Ice**
   spricht – eigener Objektadapter, echte Proxies, echte Serialisierung, echte
   Rueckrufe. Nachgebildet ist die Semantik aus `MumbleServerIce.cpp` und
   `ACL.cpp`: `setACL` ersetzt vollstaendig, `getACL` liefert geerbte Eintraege
   markiert mit, Gruppen arbeiten mit Nutzer-IDs, Listener haengen an der
   Session, `allow`/`deny` werden gegen `ChanACL::All` maskiert. Diese Tests
   laufen ueberall und sind gruen.
2. `admin/tests/test_integration_real_server.py` faehrt einen echten
   `mumble-server`-Container hoch. Er wird uebersprungen, wenn kein Docker
   erreichbar ist, und ist mit `-m integration` gezielt ausfuehrbar.

**Offen.** Ebene 2 wurde in dieser Umgebung **nicht ausgefuehrt**. Sie laeuft
vom Entwicklungsrechner gegen den Testaufbau, nicht im Laufzeit-Image -- dort
sind weder pytest noch die Testdateien enthalten, und das soll so bleiben:

```bash
docker compose -f docker-compose.test.yml up -d
cd admin && python -m pytest -m integration -v
docker compose -f docker-compose.test.yml down -v
```

**Nachtrag.** Inzwischen ausgefuehrt, regelmaessig, gegen murmur 1.5.735
(ueber den Spiegel `mirror.gcr.io`, siehe README). Was dabei herauskam, steht
in D-023 bis D-025 und D-032.


---

## D-011 - Gruppen und Richtlinien liegen am Wurzelkanal

**Problem.** Die Vorgabe lautete, Gruppen am obersten Kanal des Baums
(`Intercom`) anzulegen und die Richtlinien dort mit Apply-sub zu setzen.

**Befund.** Fuer `kick`, `ban` und `register_users` funktioniert das nicht.
`src/ACL.cpp`:

```cpp
// These permissions are only grantable from the root channel
// as they affect the users globally.
if (ch->iId == 0 && applyFromSelf) {
    if (acl->pAllow & Kick) granted |= Kick;
```

Ein solcher Eintrag an `Intercom` waere wirkungslos -- und zwar lautlos. Dazu
kommt: Gruppen vererben sich nur nach **unten**. Eine an `Intercom` definierte
Gruppe ist an der Wurzel unbekannt, ein Wurzel-ACL koennte sie also gar nicht
referenzieren.

**Entscheidung.** Gruppen **und** Richtlinien liegen am Wurzelkanal (ID 0).
Richtlinien, die im ganzen Baum gelten, bekommen `apply_here` + `apply_subs`;
die drei Wurzel-Rechte bekommen nur `apply_here`, weil `applyFromSelf` verlangt
wird. Bewahrt wird dabei der von murmur angelegte Eintrag `@admin -> Write`, den
`setACL` sonst mitloeschen wuerde.

**Konsequenz.** Gruppen gelten serverweit, nicht nur unter `Intercom`. In dieser
Installation gibt es keinen zweiten Baum; gaebe es einen, waeren die Gruppen
auch dort sichtbar.

---

## D-012 - `speak`-Gruppen bekommen Traverse und Enter mit

**Problem.** Die Abbildung `speak: [regie]` erzeugt "@all deny Speak,
@regie allow Speak". Mit einer Vorlage, die `Enter` entzieht (etwa
`geschlossen`), entstuende ein Kanal, in dem `regie` sprechen duerfte, ihn aber
nicht betreten kann -- also ein unbenutzbarer Kanal.

**Entscheidung.** Gruppen aus `speak` erhalten zusaetzlich `Traverse | Enter`,
Gruppen aus `whisper_in` und `listen_for` zusaetzlich `Traverse`. Wer sprechen
soll, muss den Kanal betreten koennen; wer mithoeren oder hineinfluestern soll,
muss ihn wenigstens sehen.

**Warum das nichts kaputt macht.** murmur gewaehrt ohne jede ACL bereits
`Traverse | Enter | Speak | Whisper | TextMessage | Listen` (`ACL.cpp`, `def`).
Bei den Vorgabewerten aendert die Zugabe also nichts -- sie wirkt nur dort, wo
eine Vorlage diese Rechte vorher entzogen hat. Dokumentiert in der
Schema-Referenz der README.

---

## D-013 - Zusaetzlicher Schluessel `channels[].acl`

**Problem.** `export` soll den Ist-Zustand als YAML im selben Schema
zurueckschreiben, ausdruecklich auch fuer "ein per Hand geklicktes Setup". Aus
rohen ACL-Bitmasken lassen sich `speak` / `whisper_in` / `listen_for` aber nicht
in jedem Fall zurueckgewinnen -- eine handgeklickte ACL folgt keinem Muster.

**Entscheidung.** Das Schema bekommt `channels[].acl`: rohe Eintraege in
derselben Form wie in `acl_templates`, angewendet nach der Vorlage und vor den
abgeleiteten Eintraegen.

Der Exporter geht in drei Stufen vor: erst raet er die lesbare Form, dann laesst
er die Vermutung durch **denselben** Generator laufen, den der Provisioner
benutzt, und vergleicht Bit fuer Bit. Passt es nicht, versucht er es mit den
Restbits von `@all` als explizitem Eintrag (das faengt den haeufigen Fall
"Vorlage hat Traverse/Enter beigesteuert"). Passt es dann immer noch nicht,
schreibt er alles roh.

**Konsequenz.** Der Export ist **immer** verlustfrei, und fuer Konfigurationen
aus dieser Datei kommt trotzdem die lesbare Form heraus. Der Name einer
`acl_template` laesst sich dabei nicht rekonstruieren -- der Server speichert nur
das Ergebnis, nicht die Herkunft.

---

## D-014 - `users[].channel` als reine Anzeige

**Problem.** Der Auftrag verlangt den Alarm "Client nicht in seinem
Soll-Kanal". Einen Soll-Kanal gab das Schema aber nicht her: `users` kannte nur
`groups`, und aus einer Gruppenzugehoerigkeit laesst sich bei mehreren
passenden Kanaelen kein eindeutiger Kanal ableiten.

**Entscheidung.** `users[name].channel` als optionaler Pfad. Er wird **nur** fuer
die Alarmleiste ausgewertet; der Provisioner fasst ihn nicht an und verschiebt
niemanden automatisch. Waehrend eines laufenden Wettkampfs jemanden ungefragt in
einen anderen Kanal zu ziehen, waere die gefaehrlichere Variante -- das GUI zeigt
die Abweichung, die Entscheidung bleibt beim Menschen.

---

## D-015 - Der Monitor-Bot braucht `Ban` am Wurzelkanal

**Problem.** In der ersten Fassung stand der Bot in der Gruppe `regie`. Damit
haette er fuer fast alle Clients keinen Paketverlust gemessen -- und die Spalte
waere leer geblieben, ohne dass irgendwo ein Fehler aufgetaucht waere.

**Befund.** `Server::msgUserStats` in `src/murmur/Messages.cpp`:

```cpp
bool extend = (uSource == pDstServerUser)
              || hasPermission(uSource, qhChannels.value(0), ChanACL::Ban);
...
bool local  = extend || (pDstServerUser->cChannel == uSource->cChannel);
if (local) {
    mpusss = msg.mutable_from_client();
    mpusss->set_good(...); mpusss->set_lost(...);
```

Die Paketzaehler haengen an `local`. Ohne `Ban` am Wurzelkanal saehe der Bot
ausschliesslich die Clients in seinem eigenen Kanal.

**Entscheidung.** Eine eigene Gruppe `monitor` mit genau einem Mitglied, und
`policies.ban: [leitung, monitor]`. Das Recht liegt bewusst **nicht** bei
`regie`: es ist eine echte Befugnis und soll nicht an einer Personengruppe
haengen, die es nie braucht. Der Bot selbst bannt niemanden -- er sendet nie
etwas ausser `UserStats`-Abfragen, und das ist strukturell sichergestellt.

**Alternative verworfen.** Den Bot in jeden Kanal zu schicken, waere die einzige
Moeglichkeit ohne `Ban` -- mit einem Client geht das aber nicht gleichzeitig, und
reihum zu wandern wuerde die Messung unbrauchbar zerhacken.

**Ueberholt von D-035.** Die zitierte Zeile stammt aus einer aelteren Fassung:
murmur 1.5.735 prueft `ChanACL::Register`, nicht `Ban` -- gemessen. Und die
Gruppe `monitor` gab es nach D-029 auf keinem Server mehr.

---

## D-016 - `.env` ist nicht versioniert

**Problem.** `setup.sh` schreibt echte Secrets in die `.env`. Eine versionierte
`.env` waere eine Falle: der naechste `git add -A` auf dem NAS wuerde sie
mitnehmen.

**Entscheidung.** Nur `.env.example` liegt im Repository, `.env` steht in der
`.gitignore`. `setup.sh` erzeugt sie beim ersten Lauf aus der Vorlage und setzt
`chmod 600`. In der Historie dieses Repositories standen zu keinem Zeitpunkt
echte Secrets -- die eingecheckte Fassung enthielt ausschliesslich
`ERSETZEN_*`-Platzhalter.

---

## D-017 - `ice_invocationTimeout` statt `Ice.Override.Timeout`

**Problem.** Ein murmur, der die Antwort schuldig bleibt, darf keinen Thread aus
dem Pool von `AsyncIceClient` festhalten. Der Pool hat vier Plaetze; vier
haengende Aufrufe legen die gesamte Oberflaeche still, `/healthz` und `/metrics`
eingeschlossen.

**Befund.** `Ice.Override.Timeout` leistet das nicht. Es ist ein
*Endpunkt*-Timeout und begrenzt einzelne Socket-Operationen, nicht die Dauer
eines Aufrufs. Nachgemessen gegen einen Servant, der absichtlich haengt:

```
Ice.Override.Timeout=3000      -> Antwort nach 12,0 s (kein Abbruch)
proxy.ice_invocationTimeout(2000) -> InvocationTimeoutException nach 2,0 s
```

**Entscheidung.** `INVOCATION_TIMEOUT_MS = 15_000` am Meta- **und** am
Server-Proxy. Der von `getServer()` zurueckgegebene Proxy erbt die Einstellung
des Aufrufers nicht -- er entsteht aus den Vorgaben des Communicators und
bekommt sie deshalb noch einmal ausdruecklich.

`Ice.InvocationTimeoutException` wird als Verbindungsverlust gewertet, obwohl
murmur den Auftrag durchaus noch ausfuehren kann: der Proxy wird verworfen und
der Reconnect setzt sauber neu auf. Ein Proxy, dessen Zustand wir nicht kennen,
ist schlechter als gar keiner.

**Nachtrag zum Abbau.** `communicator.destroy()` wartet auf ausstehende
Aufrufe -- gemessen 11 s mit gehaltenem `_lock`, in denen auch der Reconnect an
derselben Sperre stand. Der Abbau ist jetzt vom Ablegen der Felder getrennt
(Felder unter Sperre, `destroy()` ohne), und auf dem Fehlerpfad laeuft
`destroy()` in einem eigenen Daemon-Thread: es wartet dort auf genau den Aufruf,
den wir eben abgebrochen haben (gemessen 4 s Nachlauf).

---

## D-018 - Synchron oder Koroutine: wer `LiveState` liest, laeuft im Loop

**Problem.** FastAPI schiebt eine *synchrone* Pfadfunktion in einen Threadpool.
`LiveState` gehoert aber dem asyncio-Loop -- die Ice-Rueckrufe heben ihre
Ereignisse mit `call_soon_threadsafe` genau dorthin. Wer aus einem fremden
Thread ueber `live.users` laeuft, waehrend der Loop einen Client eintraegt,
faengt sich `RuntimeError: dictionary changed size during iteration`, also einen
500er -- bevorzugt dann, wenn viel los ist.

**Entscheidung.** Die Regel ist nicht "alles async", sondern:

| Endpunkt liest/schreibt | Form |
|---|---|
| `LiveState` | `async def` -- laeuft im Loop, dazwischen kommt nichts |
| SQLite (`store`) | `def` -- blockiert, gehoert in den Threadpool |
| nur Konstanten | egal, bleibt `def` |

Betroffen waren `/state`, `/metrics`, `/healthz` und `/provision/reload`. Eine
Sperre in `LiveState` waere die Alternative gewesen -- sie haette die Regel
"gehoert dem Loop" aufgeweicht und jede Leseoperation verteuert, ohne einen
Fehler zu verhindern, den die Regel ohnehin ausschliesst.

Voraussetzung ist, dass in diesen Endpunkten nichts blockiert. `/metrics` holte
die Serverlaufzeit per Ice; ausgerechnet der Endpunkt, der eine Ueberlastung
melden soll, war damit der erste, der daran haengenblieb (gemessen 30 s). Er
liest sie jetzt aus dem Polling, so wie `health()` es laengst tut.

---

## D-019 - `samples.loss_pct` ist nullbar (Schema 2)

**Problem.** "Nicht gemessen" und "kein Verlust" sind zwei verschiedene
Aussagen, und nur die zweite ist eine Entwarnung. Ohne Monitor-Bot -- oder ohne
dessen `Ban`-Recht am Wurzelkanal -- gibt es ueberhaupt keine Verlustzahlen.

**Befund.** `app.js` zeichnet Luecken in einer Sparkline seit jeher als
Unterbrechung, und `Store.sparkline` laesst sie bewusst als `None` stehen. Die
Spalte war aber `NOT NULL DEFAULT 0`: der fehlende Wert wurde beim Schreiben zu
einer 0 und in der Kurve zu einer makellosen Nulllinie -- genau die Entwarnung,
die niemand gemessen hat.

**Entscheidung.** Schema-Schritt 2 macht `loss_pct` nullbar. SQLite kann
`NOT NULL` nicht per `ALTER` entfernen, deshalb der Umbau ueber eine
Zwischentabelle. Reihenfolge: erst die alte Tabelle samt ihrer Indizes
wegwerfen, dann die Indizes neu anlegen -- ein Index behaelt beim `RENAME`
seinen Namen und haengt weiter an der alten Tabelle, `CREATE INDEX IF NOT
EXISTS` waere sonst still ein Nichtstun.

`AVG()` ueber lauter `NULL` ist `NULL`; `sparkline()` vertraegt das jetzt. Vorher
waere es ein 500er auf `/history` gewesen, sobald ein Fach nur unbemessene
Zeilen enthielt.

---

## D-020 - `UserStats` einmal voll, danach nur die Zahlen

**Problem.** Zertifikatskette, Adresse, Clientversion und Codec stehen fuer eine
Sitzung fest, sobald der Client verbunden ist. Sie kamen bei *jeder* Abfrage
mit: bei dreissig Clients alle fuenf Sekunden die vollstaendige DER-Kette je
Client -- der mit Abstand groesste Posten der ganzen Ueberwachung, fuer Daten,
die sich nicht aendern.

**Entscheidung.** Einmal voll fragen, merken, danach `stats_only=true`. Die
gemerkten Felder werden in die knappen Antworten zurueckgemischt, das Cockpit
sieht keinen Unterschied.

Selbstheilend gebaut: gemerkt wird nur, was auch angekommen ist. Fehlt dem Bot
das Recht `Ban` am Wurzelkanal, liefert murmur diese Felder gar nicht -- dann
bleibt der Speicher leer und es wird weiter voll gefragt, und ein spaeter
erteiltes Recht greift sofort. Der Speicher wird bei jedem Polling-Durchlauf
gegen die verbundenen Sessions abgeglichen und beim Reconnect geleert.

---

## D-021 - Backoff faellt erst nach einer getragenen Verbindung

**Problem.** Der Verbindungszaehler des Monitor-Bots wurde direkt nach dem
erfolgreichen Verbinden genullt. Der haeufigste Dauerfehler ist aber einer, bei
dem die Anmeldung *gelingt* und murmur den Bot gleich danach wieder loswird --
Name schon vergeben, Zertifikat abgelehnt, Ban. Der naechste Versuch wartete
damit wieder nur die Grundzeit, und der Bot haemmerte im Sekundentakt gegen den
Server.

**Entscheidung.** Zurueckgesetzt wird erst, wenn die Verbindung `_STABIL_S`
(30 s) getragen hat. Dieselbe Ueberlegung wie bei `PROVISION_ON_START`: an einen
Zeitpunkt zu binden, was an einem Ergebnis haengen muss, geht im Normalbetrieb
schief, nicht im Sonderfall.

---

## D-022 - Optionale eigene Zertifizierungsstelle beim Bau (`admin/ca/`)

**Problem.** In Netzen, in denen ein Proxy TLS aufbricht, scheitert der Bau:
`pip` meldet `CERTIFICATE_VERIFY_FAILED`, `git clone` und `curl` ebenso. Das ist
kein Sonderfall — in verwalteten Firmennetzen ist es die Regel, und es war der
Grund, warum das Image in der Entwicklungsumgebung lange nicht gebaut werden
konnte.

**Entscheidung.** `admin/ca/` ist ein Ablageort fuer `*.crt`-Dateien. Die
Builder-Stufe kopiert das Verzeichnis nach
`/usr/local/share/ca-certificates/intercom-extra/` und ruft
`update-ca-certificates`. Leeres Verzeichnis = unveraenderter Bau, also kein
Sonderpfad und keine Fallunterscheidung im Dockerfile (`COPY` eines leeren
Verzeichnisses ist erlaubt, eine bedingte `COPY` nicht).

`ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt` gehoert dazu: pip benutzt
nicht den Systemspeicher, sondern das mitgelieferte Bundle von `certifi`. Der
Wert zeigt auf den Systemspeicher und ist damit auch ohne eigene CA richtig.

**Das Laufzeit-Image bleibt unberuehrt.** Es wird nur in der Builder-Stufe
gesetzt, und die wird verworfen; die Laufzeitstufe laedt nichts mehr aus dem
Netz (`pip install --no-index` aus den fertigen Wheels). Ein Bau hinter einem
Firmenproxy erzeugt damit dasselbe Image wie ein Bau ohne — nachgeprueft an
Zertifikatsspeicher und Umgebung des fertigen Images.

Die Zertifikate selbst sind umgebungsspezifisch und stehen in der
`.gitignore`; im Repository liegt nur `admin/ca/README.md`.

---

## D-023 - Die Konfigurationsansicht zeigt Herkunft, nicht Ist/Soll

**Problem.** Die Ansicht stellte `getAllConf` als Ist-Wert neben
`getDefaultConf` als Vorgabe. Der erste Lauf gegen einen echten Server zeigte,
dass das die Verhaeltnisse verdreht.

**Befund.** `Server::getAllConf` liest nur die `config`-Tabelle, also
ausschliesslich zur Laufzeit per `setConf` geaenderte Werte — auf einem frischen
Server ausser `certificate` nichts. `Meta::getDefaultConf` liefert `qmConfig`,
das `MetaParams` aus der **ini-Datei** plus eingebauten Vorgaben baut, beim
Docker-Image also aus den `MUMBLE_CONFIG_*`-Variablen der Compose. Gemessen:
`getAllConf` → 1 Schluessel, `getDefaultConf` → 35, darunter `port=64739` aus
der Compose (murmurs eingebaute Vorgabe waere 64738).

Zwei Folgen, beide schlecht: die Ansicht behauptete fuer jede Einstellung aus
der Compose „nicht gesetzt", und die vom Auftrag geforderte Warnung
„Compose und Live laufen auseinander" konnte **strukturell nie** ausloesen —
`env_mismatch` verlangte einen nichtleeren Ist-Wert, und der war fuer genau
diese Schluessel immer leer.

**Entscheidung.** Spalten `Wirksam` / `Herkunft` (Datenbank oder Datei) /
`Faellt zurueck auf`. Der wirksame Wert ist der Datenbankeintrag, sonst der
Dateiwert. Herkunft *Datenbank* ist die Zeile, auf die es ankommt: live
geaendert, folgt der Compose nicht mehr, ueberlebt jeden `up -d`. Der Abgleich
mit der Compose normalisiert die Namen so wie der Einstiegspunkt des Images
(`${1^^}`, Unterstriche weg) — murmur schreibt dieselbe Einstellung an drei
Stellen unterschiedlich: `registername`, `registerName`,
`MUMBLE_CONFIG_REGISTERNAME`.

---

## D-024 - Die Laufzeitstufe uebernimmt Installationen, keine Wheels

**Problem.** Die Laufzeitstufe hatte `COPY --from=builder /wheels /wheels` und
raeumte danach mit `rm -rf /wheels` auf. Das gibt den Platz nicht zurueck: die
Kopierschicht bleibt im Image, das `rm` legt nur eine Tilgung darueber. Gemessen
am fertigen Image: 554 MB, davon rund 50 MB fuer eine Schicht, deren Inhalt es
gar nicht mehr gibt.

**Entscheidung.** Die Builder-Stufe installiert nach
`--root=/install --prefix=/usr/local`, die Laufzeitstufe uebernimmt den Baum mit
einem `COPY --from=builder /install/ /`. Es entsteht keine Wheel-Schicht mehr.
Beide Stufen benutzen dasselbe Basisimage, die Python-Version stimmt also
zwangsweise. Gemessen: **445 MB statt 554 MB.**

Bewusst *nicht* `RUN --mount=type=bind,from=builder`: das braucht BuildKit,
und wo das nicht aktiv ist, scheitert der Bau hart statt langsamer zu werden.
Betrifft aeltere Docker-Fassungen ebenso wie NAS-Oberflaechen mit eigenem
Bau-Weg. Der Weg ueber `--root` laeuft mit jedem Docker.

`--ignore-installed` gehoert dazu: zeroc-ice ist in der Builder-Stufe bereits
installiert, weil `slice2py` es braucht. Ohne das Kennzeichen meldet pip
"Requirement already satisfied" und legt es **nicht** unter `/install` ab. Das
Laufzeit-Image waere ohne Ice gewesen, und zwar lautlos, denn alle anderen
Pakete waren da. Aufgefallen ist es nur, weil die Importpruefung im Dockerfile
tatsaechlich importiert -- Grund genug, sie dort zu behalten.

---

## D-025 - Der Export liest die wirksame Konfiguration, nicht die Datenbank

**Problem.** Zwei Fehler im Exporter, beide erst beim Lauf gegen einen echten
Server aufgefallen, beide gegen das Abnahmekriterium "Export ist wieder
einlesbar".

**1. `get_all_conf` verliert die Konfiguration aus der Datei.** Der Exporter las
`welcometext` und `defaultchannel` aus `Server::getAllConf` -- und das sind nur
die Datenbank-Uebersteuerungen (siehe D-023). Auf einem Server, der ueber die
`MUMBLE_CONFIG_*`-Variablen der Compose eingerichtet wurde, stehen beide Werte
in der ini-Datei; der Export liess sie stillschweigend weg. Ein Ruecksichern
haette den Begruessungstext und den Vorgabekanal auf die Werkseinstellung
zurueckgesetzt. Neu: `IceClient.get_effective_conf()` -- Datei, darueber die
Datenbank.

**2. `roots=` erzeugte eine nicht einlesbare YAML.** `server.defaultchannel`
wurde ungeprueft mitgeschrieben. Zeigte er auf einen Kanal ausserhalb des
exportierten Teilbaums, verwies die YAML auf einen Kanal, den sie selbst nicht
anlegt, und `parse_config` lehnte sie mit "Kanal gibt es nicht" ab. Das
Kriterium war also genau dann verletzt, wenn ein Vorgabekanal gesetzt war.

**Entscheidung.** Der Vorgabekanal wird nur uebernommen, wenn er im Export
vorkommt. Andernfalls erscheint im Kommentarkopf des Dokuments eine Zeile
"Nicht uebernommen: server.defaultchannel verweist auf ... -- der Kanal liegt
ausserhalb des exportierten Teilbaums". Weglassen ja, stilles Weglassen nein.

---

## D-026 - Zielsystem ist ein beliebiger Linux-Rechner, und es gibt kein HTTPS

**Problem.** Die urspruengliche Aufgabe nannte eine Synology mit DSM, und die
Unterlagen waren entsprechend durchsetzt: `/volume1/docker/...` als Pfad,
`synopkg start ContainerManager` als Hilfe, der DSM-Reverse-Proxy als der Weg
zu TLS. Als Zielsystem wurde daraus ein gewoehnlicher Linux-Rechner -- ein
Raspberry Pi, ein Mini-PC, eine virtuelle Maschine.

**Entscheidung.** Alles DSM-Eigene ist raus, aus README, `setup.sh`,
`.env.example` und den Kommentaren im Code. Was bleibt, ist bewusst
verallgemeinert und nicht ersatzlos gestrichen:

* `setup.sh` blieb schon vorher ohne `mapfile`, assoziative Arrays und
  GNU-eigene `sed`-Schalter. Das war fuer die DSM-Bash gedacht und schadet
  anderswo nicht -- es laeuft damit auf Debian, Ubuntu und Raspberry Pi OS
  genauso.
* Die Rechtevergabe auf `./server` und `./admin-data` (UID/GID 10000) bleibt
  wie sie ist: sie faellt auf eine Warnung mit `sudo`-Hinweis zurueck, wenn der
  aufrufende Benutzer nicht darf. Genau der Fall auf einem Pi, wo man nicht als
  root arbeitet.
* Der Hinweis auf gepufferte Datenstroeme bleibt als Fehlersuche-Abschnitt --
  nur nicht mehr auf einen bestimmten Proxy gemuenzt. `X-Accel-Buffering: no`
  wird weiter gesetzt; es kostet nichts und hilft, sobald doch einer davorsteht.

**Kein HTTPS, ausdruecklich.** Das Cockpit laeuft unverschluesselt. Passwort und
Sitzungscookie gehen im Klartext ueber das Netz. Fuer ein abgeschlossenes
Stadionnetz ist das vertretbar -- aber nur dann, und das steht jetzt an drei
Stellen so da: im Schnellstart, in der `.env.example` neben `LISTEN_HOST` und in
der Schlussmeldung von `setup.sh`. Als Absicherung ohne Zusatzsoftware ist
`LISTEN_HOST` gedacht: die IP der Netzkarte eintragen, die im Stadionnetz
haengt, statt `0.0.0.0` stehen zu lassen. Dann ist der Port auf allen anderen
Schnittstellen gar nicht erst offen.

Die Anwendung selbst aendert sich dadurch nicht. Sie lieferte von Anfang an nur
HTTP und tut das weiter; wer spaeter TLS will, stellt einen Reverse Proxy davor,
ohne im Code etwas anzufassen.

---

## D-027 - zeroc-ice kommt aus Debian, nicht aus dem Quelltext

**Problem.** D-008 entschied, `zeroc-ice` im Image aus dem Quelltext zu bauen.
Das stimmte fuer die damalige Annahme (Ubuntu als Basis, dort Python 3.12 und
ein dagegen gebautes Ice) -- aber es wurde nie gegen **Debian** geprueft, und
das ist die Basis, auf der das Image ohnehin laeuft.

Der Preis war hoch und faellt erst auf schwacher Hardware auf: `pip install
zeroc-ice` uebersetzt den kompletten C++-Quelltext, hier gemessen **gut zehn
Minuten auf x86-64**. Auf einem Raspberry Pi entsprechend laenger, und mit 2 GB
Arbeitsspeicher kann der Compiler dabei aussteigen. Fuer ein Geraet, auf dem das
Ding laufen soll, ist das die falsche Richtung.

**Befund.** Debian 12 liefert das Paket fertig, und zwar gegen genau die
Python-Version gebaut, die dieses Projekt benutzt:

| Basis | `python3` | `python3-zeroc-ice` | gebaut fuer |
|---|---|---|---|
| Ubuntu 24.04 | 3.12.3 | 3.7.10 | 3.12 |
| Ubuntu 22.04 | 3.10.6 | 3.7.6 | 3.10 |
| **Debian 12** | **3.11.2** | **3.7.8** | **3.11** |

`slice2py` kommt aus `zeroc-ice-compilers`, die von `MumbleServer.ice`
eingebundenen Ice-Slices aus `libzeroc-ice-dev` (nur in der Builder-Stufe).

**Ice 3.7.8 statt 3.7.11 ist geprueft, nicht angenommen.** Gegen murmur 1.5.735:

* `slice2py` 3.7.8 uebersetzt die 1.5.735-Slice zu **133 Pruefsummen** --
  dieselbe Zahl wie mit 3.7.11.
* `Meta.getSliceChecksums()` meldet 71 Pruefsummen, davon **0 abweichend und 0
  fehlend**.
* `getChannels`, `getACL`, `getAllConf`, Rueckrufe, Provisionierung und der
  Monitor-Bot laufen unveraendert durch.

**Entscheidung.** Basis ist `debian:bookworm-slim`, Ice kommt per `apt`.
Ergebnis: sauberer Bau ohne Cache in **50 Sekunden statt ueber zehn Minuten**,
Image **329 MB statt 445 MB**, kein Compiler im Bau.

Zwei Dinge, die dazugehoeren:

* **`zeroc-ice` ist ein Extra, keine feste Abhaengigkeit** (`pyproject.toml`).
  Debian legt die Module ab, aber keine pip-Metadaten -- pip haelt das Paket
  also fuer nicht installiert und wuerde es trotzdem aus dem Quelltext bauen.
  Im Image kommt Ice aus Debian, auf einem Arbeitsplatz ohne das
  Distributionspaket ueber `pip install -e ".[dev,ice]"`.
* **Eine venv mit `--system-site-packages`.** Unsere Abhaengigkeiten kommen aus
  pip, Ice aus `/usr/lib/python3/dist-packages`. Ohne den Schalter saehe die
  Umgebung es nicht. Zugleich umgeht das PEP 668, ohne mit
  `--break-system-packages` an Debians Paketverwaltung vorbeizuschreiben.

**Auf arm64 nachgewiesen.** Das Image wurde fuer `linux/arm64` gebaut (338 s
unter QEMU-Emulation), gestartet und gegen denselben echten mumble-server
gefahren: `uname -m` meldet `aarch64`, Ice 3.7.8 mit 133 Pruefsummen, alle
Seiten und API-Endpunkte antworten, der Monitor-Bot haengt stumm und taub im
Zielkanal. Die Emulation belegt Bau und Lauf, nicht die Geschwindigkeit auf
echter Hardware -- die Antwortzeiten (0,22 s statt 0,03 s fuer die
Cockpit-Seite) sind Emulationskosten.

**Was das nicht ist.** Eine Aussage ueber das Betriebssystem des Hosts. Ubuntu,
Debian oder Raspberry Pi OS auf dem Geraet sind gleichermassen in Ordnung -- der
Container bringt sein eigenes Userland mit. Die Tabelle oben betrifft
ausschliesslich das Basisimage.

---

## D-028 - Das Image kommt aus der Registry, der Bau ist Rueckfallebene

**Problem.** `setup.sh` rief `docker compose up -d --build` und baute das
Admin-Image auf dem Zielgeraet. Auf einem Raspberry Pi ist das der langsamste
Teil der ganzen Einrichtung -- fuer ein Image, das auf jedem Geraet identisch
ist. Gebaut werden muss es nur, weil es niemand veroeffentlicht hat.

**Entscheidung.** `.github/workflows/image.yml` baut es und legt es in die
Container-Registry von GitHub (`ghcr.io`). Gebaut wird auf **zwei Laeufern,
jeder nativ**: `ubuntu-latest` fuer x86-64, `ubuntu-24.04-arm` fuer arm64.
Kein QEMU -- der emulierte arm64-Bau dauerte hier 338 s, nativ ist er in
derselben Groessenordnung wie x86-64. Beide Laeufer sind fuer oeffentliche
Repositories kostenlos. Ein dritter Schritt fasst die beiden Digests zu einer
Multi-Arch-Liste zusammen, aus der `docker pull` von selbst das Richtige
waehlt.

In der Compose stehen jetzt `image:` **und** `build:`. Das ist kein Versehen:
nachgemessen zieht Compose in dieser Kombination das Image und baut nur, wenn
das Ziehen scheitert oder man `--build` mitgibt. Damit deckt dieselbe Datei
drei Faelle ab -- fertiges Image, eigene Abspaltung ohne Registry, Rechner ohne
Internet.

`setup.sh` versucht entsprechend erst zu ziehen und baut nur im Fehlerfall.
Beide Wege sind durchgespielt: mit erreichbarem Image laeuft die komplette
Einrichtung in **14 Sekunden** durch, ohne faellt sie auf den lokalen Bau
zurueck und meldet das ausdruecklich.

**Was von Hand bleibt.** Ein neu angelegtes Paket in der GitHub-Registry ist
**privat**, auch bei oeffentlichem Repository: es erbt die Zugriffsrechte des
verknuepften Repositories, aber ausdruecklich nicht dessen Sichtbarkeit. Damit
ein Geraet ohne Anmeldung ziehen kann, muss die Sichtbarkeit einmal in der
Oberflaeche umgestellt werden. Die Verknuepfung selbst stellt das Etikett
`org.opencontainers.image.source` im Dockerfile her.

**Geprueft.** Zunaechst nur mittelbar -- GitHub Actions laesst sich in dieser
Umgebung nicht ausfuehren -- naemlich ueber die YAML-Gueltigkeit, das Verhalten
von Compose bei `image` plus `build`, beide Zweige in `setup.sh` gegen eine
lokale Registry und die Etiketten im fertigen Image. Inzwischen ist der
Arbeitsablauf selbst mehrfach durchgelaufen und hat das Multi-Arch-Image
veroeffentlicht; der erste Lauf brauchte 2 min 20 s.

---

## D-029 - Der Server ist die Quelle der Wahrheit, nicht die `intercom.yaml`

**Problem.** `PROVISION_ON_START` stand ueberall auf `true`. Bei jedem Start des
Containers wurde die YAML wieder auf den Server geschrieben -- was jemand in der
Oberflaeche an einem verwalteten Kanal geaendert hatte, war danach weg. Die
Datei gewann, immer. Damit war jede Aenderung in der Oberflaeche bestenfalls
vorlaeufig, und die Oberflaeche fuehlte sich an wie ein Betrachter mit Knoepfen.

**Entscheidung.** Umgekehrt: angelegt und geaendert wird in der Oberflaeche, und
was dort steht, bleibt dort.

* `PROVISION_ON_START` ist per Vorgabe **aus** (`config.py`,
  `docker-compose.yml`, `.env.example`).
* Eine **fehlende** `intercom.yaml` ist der Normalfall, kein Fehler: kein
  Banner, kein Log-Eintrag. Eine Datei, die da ist und sich nicht lesen laesst,
  bleibt ein Fehler -- dann wollte jemand etwas und es ging schief.
* `setup.sh` legt nichts mehr an. Eine Einrichtung, die ungefragt elf Kanaele
  mit fremden Namen hinstellt, nimmt die Entscheidung vorweg.

**Was an die Stelle tritt.** Baukaesten in der Oberflaeche
(`admin/intercom/provision/vorlagen.py`) als einmaliger Startschuss, keine
laufende Bindung: wer danach einen Kanal umbenennt, hat einen umbenannten Kanal.
Dazu eine Sicherung zum Herunterladen und Einspielen -- beim Einspielen ist
Aufraeumen per Vorgabe aus, denn eine Sicherung einzuspielen soll nichts
wegwerfen, was jemand seither angelegt hat.

Jede Vorlage ist der Text einer `intercom.yaml` und laeuft durch dieselbe
Pruefung wie eine eingespielte Sicherung. Sie kann also nichts, was eine
Sicherung nicht auch koennte, und ein Fehler faellt im Test auf statt beim
Anwenden.

**Die Provisionierung bleibt.** Sie ist jetzt ein Werkzeug (Testlauf, Anwenden,
Export) statt ein Herr, der bei jedem Start durchgreift.

---

## D-030 - Die Rechte-Auswertung ist nachgebaut, nicht erfragt

**Problem.** Die Oberflaeche soll zeigen, was eine Rolle an einem Platz darf --
auch dann, wenn niemand verbunden ist. Beim Aufbauen einer Veranstaltung sitzt
noch keiner im Kanal, und genau dann will man die Rechte sehen.

`Server::effectivePermissions` beantwortet die Frage, aber nur fuer eine
**verbundene Sitzung**. Es gibt in der Slice nichts, was "was duerfte jemand aus
Gruppe X in Kanal Y" ohne Sitzung beantwortet.

**Entscheidung.** `ChanACL::effectivePermissions` (src/ACL.cpp) und
`Group::appliesToUser` (src/Group.cpp) sind in `admin/intercom/ice/wirkung.py`
nachgebaut, Stand v1.5.735.

**Warum nicht naeherungsweise.** Weil eine Naeherung hier genau das erzeugt, was
nicht passieren darf: eine Anzeige, die etwas behauptet, das der Server anders
sieht. Drei Details kommen aus dem Gedaechtnis falsch heraus, und jedes einzelne
haette die Anzeige zum Luegen gebracht:

1. **Innerhalb eines Eintrags gilt erst `allow`, dann `deny`.** Ein Eintrag, der
   dasselbe Recht erlaubt und verbietet, verbietet es. Die umgekehrte Annahme
   zeigt ein erteiltes Recht, das der Server verweigert.
2. **`bInheritACL` bricht die Kette nicht ab.** Sie laeuft *immer* bis zur
   Wurzel; ein nicht erbender Kanal setzt nur die gesammelten Rechte auf die
   Grundausstattung zurueck (`if (!ch->bInheritACL) granted = def;`). `Traverse`
   und `Write` laufen daran vorbei weiter -- ein fehlendes `Traverse` von oben
   nimmt auch einem nicht erbenden Unterkanal alles.
3. **`Write` impliziert fast alles, aber weder `Speak` noch `Whisper`.** Ein
   Admin darf alles verwalten und trotzdem nicht ueberall reinreden.

Ein vierter Punkt kam aus dem Zufallstest: **`getACL` liefert geerbte Gruppen
mit leerer `add`-Liste** (`impl_Server_getACL` fuellt bei ihnen nur `members`).
Wer sie zur Aufloesung heranzieht, haelt jede vererbte Rolle fuer unbesetzt und
zeigt zu wenig Rechte an. Die Aufloesung laeuft deshalb ueber die *eigenen*
Gruppen jedes Kanals -- so wie `Group::appliesToUser` ueber `qhGroups` laeuft.

**Was nicht nachgebaut wird.** Alles, was an einer echten Verbindung haengt:
`strong` (geprueftes Zertifikat), `#zugangswort`, `$zertifikatshash`. Das wird
**nicht geraten**, sondern eingeklammert: jede Kombination der offenen Fragen
wird durchgerechnet, und nur was in allen Durchlaeufen gleich herauskommt, gilt
als sicher. Der Rest kommt als "kommt drauf an" in die Oberflaeche.

Zwei Extremlaeufe ("alle treffen zu" / "keine trifft zu") genuegen dafuer
**nicht** -- auch das hat der Zufallstest gefunden: zwei Angaben koennen sich
gegenseitig aufheben, und ein Bit, das in beiden Extremen gleich ist, kann in
einer Mischung abweichen. Gezaehlt wird deshalb ueber alle 2^n Kombinationen,
mit einer Reissleine bei zehn verschiedenen Angaben (in der Praxis sind es null
oder eine).

`sub` wird ausgerechnet statt eingeklammert -- es haengt nur am Aufenthaltsort,
und der ist bekannt.

**Belegt.** 642 gewuerfelte ACL-Konstellationen gegen murmur v1.5.735, jeweils
mit einem wirklich verbundenen pymumble-Client und `effectivePermissions` als
Massstab. Geprueft wird die Eigenschaft, auf die sich die Oberflaeche verlaesst:
**kein Bit, das als sicher ausgegeben wird, weicht ab.** Ergebnis: keines. Der
Kern davon steht als Integrationstest in
`test_gerechnete_rechte_stimmen_mit_dem_server_ueberein`.

---

## D-031 - Der Wunschzustand fuer das, was murmur nicht behaelt, steht in SQLite

**Problem.** Drei Dinge ueberleben in Mumble keine Verbindung:

| Was | Warum nicht |
|-----|-------------|
| Fester Platz je Person | `enum UserInfo` hat kein Kanalfeld; `defaultchannel` gilt fuer alle gleich |
| Dauerhaftes Mithoeren | `startListening` nimmt eine **Sitzung**, kein Konto |
| Vorrang beim Sprechen | Priority Speaker ist ein Flag am verbundenen Client |

Frueher stand das in der `intercom.yaml`. Die ist als Quelle der Wahrheit weg
(D-029), also brauchte es einen neuen Ort.

**Entscheidung.** Tabelle `wunsch` in `/data/history.sqlite`, Migrationsschritt
3. Der `Enforcer` zieht sie nach dem Verbinden nach.

**Als Pfad, nicht als Kanal-ID.** murmur vergibt IDs neu, sobald ein Kanal
geloescht und wieder angelegt wird. Nach dem Einspielen einer Sicherung zeigte
eine gespeicherte ID auf den falschen Platz oder ins Leere. Beim Umbenennen und
Verschieben werden die Pfade mitgezogen (`wunsch_umschreiben`), samt Unterpfaden
und ohne Namensverwandte zu treffen.

**Beim Abmelden wird geloescht.** murmur vergibt Nutzer-IDs weiter; ein
stehengebliebener Wunsch erbte irgendwann die naechste Person mit derselben ID.

**Der Platz wird einmal je Sitzung hergestellt, nicht dauernd.** Wer danach
bewusst woanders hingeht, soll dort bleiben duerfen. Ein Automatismus, der
jemanden mitten im Wettkampf zurueckzieht, ist schlimmer als gar keiner. Die
Merkliste haengt an der Sitzung und wird beim Trennen geleert.

**In der Sicherung, nach Namen.** Ein eigener Abschnitt `wunsch:` im Export --
`parse_config` laesst unbekannte Schluessel auf oberster Ebene stehen, der
Provisioning-Weg bleibt also unberuehrt. Geschluesselt nach Nutzernamen, aus
demselben Grund wie oben bei den Pfaden. Wen es beim Einspielen nicht gibt,
meldet die Antwort als fehlend, statt ihn zu verschlucken.

**Sichtbar getrennt.** Das Pult sagt in einer eigenen Tafel, was der Server
haelt und was nicht: Rollen, Rechte und der Kanalbaum ueberleben einen Neustart,
ein Zug in einen Kanal gilt nur fuer diese Verbindung, und ein fester Platz
kommt gar nicht von Mumble, sondern von uns.

**Nachtrag (Pfadvergleich).** "Ohne Namensverwandte zu treffen" stimmte bis zur
Einfuehrung der Ruftasten nur halb: verglichen wurde mit `LIKE pfad || '/%'`,
und dort sind `_` und `%` Platzhalter. Ein Umbenennen von `KG_1` zog `KGA1/...`
mit. Alle Pfad-Umschreibungen (Wuensche, Verbindungen, Ruftasten) vergleichen
jetzt mit `substr(spalte, 1, n) = pfad || '/'`; ein Test mit `_` und `%` im
Namen haelt das fest.

---

## D-032 - Ruftasten: zentral belegte Tasten ueber `redirectWhisperGroup`

**Problem.** Profi-Intercoms (GreenGo, Riedel, Clear-Com) belegen die Tasten
eines Beltpacks zentral: in der Konfigurationssoftware steht, dass Taste 1 am
Kampfgericht die Zeitmessung ruft. In Mumble stehen Fluestertasten im Client.
Die Slice hat keine Methode, um sie von aussen zu setzen -- `setState`,
`updateRegistration` und `setACL` beruehren sie nicht.

**Der Umweg.** `MumbleServer.ice` hat
`redirectWhisperGroup(int session, string source, string target)`. Im Quelltext
von murmur v1.5.735 nachgelesen -- gesetzt in `Ice.cpp`
(`impl_Server_redirectWhisperGroup`, Tabelle `ServerUser::qmWhisperRedirect`),
ausgewertet in `Server.cpp` (`createWhisperTargetCacheFor`):

- greift nur bei einem Fluesterziel vom Typ **Kanal** mit Gruppenbeschraenkung;
- der Schluessel ist der Gruppenname, den **der Client** in sein Ziel
  geschrieben hat; murmur setzt dafuer `target` ein;
- haengt an der **Sitzung** (`ServerUser`) und stirbt beim Trennen;
- leeres `target` hebt die Umleitung auf; es gibt keinen Getter;
- `redirectWhisperGroup` ruft `clearACLCache(user)`, das den Fluesterziel-Cache
  leert -- eine Aenderung wirkt also sofort, nicht erst beim naechsten
  Tastendruck nach Neuaufbau des Caches.

Daraus: jeder Client richtet **einmal** Taste n ein als "Rufen an den obersten
Platz samt Unterplaetzen, beschraenkt auf Gruppe `rufn`". Eine Gruppe `rufn`
gibt es nicht; ohne Umleitung hoert niemand etwas. Der Server leitet `rufn` je
Sitzung auf die Rolle um, die an diesem Platz gerufen werden soll.

**Gemessen, nicht vermutet.** Echter murmur 1.5.735, drei pymumble-Clients mit
Opus, ein Sender mit festem Fluesterziel (`ruf1`, Kanal 0, Unterkanaele):

| Fall | Erwartet | Ergebnis |
|------|----------|----------|
| keine Umleitung | niemand hoert | ✔ |
| `ruf1` → Technik | nur Technik | ✔ |
| mitten in der Sitzung auf Zeitmessung umgestellt | nur Zeitmessung | ✔ |
| Umleitung entfernt | niemand | ✔ |
| Zielplatz ohne Fluesterrecht | niemand | ✔ |
| nach Neuverbinden, nicht erneut gesetzt | niemand | ✔ |
| nach Neuverbinden erneut gesetzt | Ziel hoert | ✔ |

Der Ende-zu-Ende-Test `test_ruftaste_wird_ueber_den_enforcer_wirklich_gehoert`
(`tests/test_integration_real_server.py`) laesst das ueber den echten
`Enforcer` laufen, samt Platzwechsel, bei dem am Client nichts passiert.

**Umsetzung.**

- Tabelle `ruftaste (platz, taste, rolle)`, Migration 6. Platz als Pfad wie
  bei D-031; der oberste Platz ist der leere Pfad (in der Datei: `/`).
- Vier Tasten. Belegung **erbt nach unten**: am Ordner "Kampfgerichte"
  belegt, gilt sie fuer alle acht Kampfgerichte, solange eines nicht selbst
  etwas anderes festlegt. Der naechstgelegene Eintrag gewinnt.
- Der `Enforcer` setzt die Umleitung bei jedem Verbinden und jedem
  Platzwechsel nach -- nur wenn sie sich aendert, weil es keinen Getter gibt
  und er sich deshalb merkt, was er zuletzt geschrieben hat.
- Beim Belegen schaltet die Oberflaeche den Rufenden das Fluesterrecht
  ("Reinschalten") an den Plaetzen der Gerufenen frei, wo es fehlt, und sagt
  das in der Antwort.

**Was die Oberflaeche anzeigen muss, weil sonst eine Taste belegt aussieht und
stumm bleibt:**

1. *Fluesterrecht je Zielplatz.* `createWhisperTargetCacheFor` prueft
   `ChanACL::Whisper` an jedem Kanal, in dem ein Empfaenger sitzt. Fehlt es an
   einem, kommt der Ruf dort nicht an. Das Platzblatt rechnet das mit
   `wirkung.py` nach und zeigt "nicht ueberall – fehlt an n Stellen".
2. *Der Rufende muss auf seinem Platz sprechen duerfen.* Wer unterdrueckt ist,
   dessen gesamte Sprache verwirft murmur, Fluestern eingeschlossen
   (`Server.cpp`, Abbruch bei `bSuppress`). Als Rufende gelten deshalb nur
   Rollen, die dort sprechen duerfen.

**Grenzen, ehrlich benannt.**

- Die Taste im Client muss jede Person **einmal** selbst einrichten. Das
  Platzblatt fuehrt mit den deutschen Bezeichnungen des Mumble-Programms 1.5
  durch ("Flüstern/Rufen", "Beschränke auf Gruppe" ...), entnommen aus
  `mumble_de.ts` und `GlobalShortcut.cpp` derselben Version. Als Zielkanal
  empfiehlt es den Eintrag **"Hauptkanal"** (`SHORTCUT_TARGET_ROOT`): er wird
  erst beim Druecken auf Kanal 0 aufgeloest (`MainWindow::mapChannel`), laesst
  sich also ohne Verbindung einstellen und gilt auf jedem Server. Was der
  Client dann sendet -- Kanalziel 0, Unterkanaele, Gruppe `rufn` -- ist genau
  das, was die Messung oben mit pymumble gesendet hat.
- **Nicht** durchgeklickt ist es am Desktop-Programm selbst, und mit Mumla
  oder anderen Apps ist nicht geprueft, ob sie im Fluesterziel eine Gruppe
  beschraenken koennen. Die Oberflaeche sagt das so.
- Eine Taste ruft eine Rolle, nicht einen Platz. Wer eine Person auf einem
  bestimmten Platz rufen will, ruft die Rolle, die dort sitzt.
- Das Fluesterrecht laesst sich in Mumble nicht auf eine Gruppe beschraenken:
  `ChanACL::Whisper` am Zielplatz gilt fuer jedes Fluestern dorthin, die
  Gruppenbeschraenkung waehlt der Client. Wer eine Ruftaste auf eine
  wandernde Rolle (Technik, Leitung) legt, bekommt deshalb Reinschalten an
  allen Plaetzen, an denen diese Rolle sprechen darf -- und koennte mit einer
  selbst eingerichteten Fluestertaste ohne Gruppe dort auch alle anderen
  erreichen. Das Pult setzt das Recht nicht still: die Antwort nennt Rolle
  und Zahl der Plaetze, das Platzblatt sagt es dauerhaft, und im Raster steht
  es in der Spalte "Reinschalten".
- Keine Rueckmeldung am Gerufenen, wer gerufen hat, ausser der Stimme selbst --
  Mumble zeigt Fluestern als solches an, aber keinen Tastennamen.

---

## D-033 - Shows: benannte Aufbauten statt einer einzigen Sicherung

**Problem.** Derselbe Server wird fuer verschiedene Veranstaltungen umgebaut --
Landesfinale mit acht Kampfgerichten, Training mit zwei, Probe ohne Presse.
Die Sicherung war eine Datei: herunterladen, irgendwo ablegen, beim naechsten
Mal suchen und hochladen. Und sie war unvollstaendig im Einspielen: Ruftasten
fehlten, und die Netzsegmente kamen aus der geladenen `intercom.yaml` statt aus
der hochgeladenen Datei -- ein Test war nur gruen, weil beide zufaellig
dasselbe Segment enthielten.

**Entscheidung.** Tabelle `show (name, notiz, yaml_text, ...)`, Migration 7.
Eine Show ist **woertlich dieselbe Datei** wie eine Sicherung. Gespeichert wird
der Text, nicht eine zerlegte Form:

- Laden geht exakt den Weg einer hochgeladenen Datei (`web/shows.py`,
  `einspielen()`), es gibt nur einen.
- Was man herunterlaedt, ist Byte fuer Byte das, was geladen wuerde.
- Eine Show laesst sich als Datei mitnehmen und anderswo wieder ablegen.

**Ein Weg, drei Schritte, jeder mit Testlauf.**

1. Plaetze, Rollen, Regeln ueber den Planner (mit Aufraeumen auch loeschen).
2. Was nur diese Oberflaeche kennt: Wunsch je Person (nach Namen), Verbindungen,
   Ruftasten, Netzsegmente. Der Testlauf zaehlt, was uebernommen, geaendert
   und entfernt wuerde, und sagt es in Saetzen.
3. `wunschzustand_neu_laden()`: Enforcer neu laden und alle verbundenen
   Sitzungen sofort nachziehen -- sonst stuenden Ruftasten und feste Plaetze
   bis zum naechsten Abgleich auf dem alten Stand.

**Aufraeumen ist bei Shows die Vorgabe, bei Dateien nicht.** Wer eine Show
laedt, will danach genau diese Show. Wer eine alte Sicherung einspielt, will
meist nur Verlorenes zurueck. Beide Male gilt: erst Testlauf, und der
Laden-Knopf bleibt gesperrt, bis ein Testlauf **mit demselben Haken** gelaufen
ist. Die Rueckfrage nennt die Zahl der Loeschungen.

**Was ein Laden nie tut:** Personen registrieren oder deren Registrierung
loeschen. murmur registriert an einem Zertifikat, das nur das Geraet der Person
mitbringt. Namen aus der Show, die der Server nicht kennt, nennt das Ergebnis,
samt dem Weg dahin.

**"Zuletzt geladen" ist eine Auskunft, kein Zustand.** Der Server bleibt die
Wahrheit (D-029). Wer nach dem Laden etwas aendert, hat etwas geaendert; ob der
Server noch der Show entspricht, sagt nur ein Testlauf -- und genau darauf
verweist die Oberflaeche.

**Namen.** Leerraum wird zusammengezogen, damit "Halle 1" und "Halle  1" nicht
zwei Shows sind. Ein vorhandener Name wird nur nach ausdruecklicher Rueckfrage
ueberschrieben (HTTP 409 → Rueckfrage → `ueberschreiben: true`). Namen gehen als
Query- bzw. Body-Parameter, nicht im Pfad -- ein `/` im Namen ("Halle 1 /
Probe") bleibt so erlaubt.

---

## D-034 - Keine aktive `intercom.yaml` im Repository

**Problem.** Im Wurzelverzeichnis lag seit dem ersten Tag eine `intercom.yaml`
mit dem Kopf "Diese Datei ist die Wahrheit", und `docker-compose.yml` band sie
nach `/config/intercom.yaml` ein. Nach D-029 sollte eine fehlende Datei der
Normalfall sein -- auf jeder Installation per `git clone` war sie aber da und
wurde gelesen (`/healthz` meldete `config.loaded: true`). Folgen:

* Ihre sechs **Netzsegmente** galten, solange im Editor keines gepflegt war.
  Die Netzsicht zeigte dann Namen wie "Richtfunk Nord", die niemand angelegt
  hatte und die im Editor nicht standen -- dieselbe Inkonsistenz, die zur
  Verlagerung der Netze in die Oberflaeche gefuehrt hatte.
* Ihre **Personen** (`regie-1`, `kam-1`, `monitor` ...) lieferten Soll-Plaetze
  fuer die Alarmleiste und ihre **Geraete** Angaben im Detailpanel -- fuer
  jeden, der zufaellig so hiess.
* Hinzu kam eine Falle: fehlt eine per Bind eingebundene **Datei** auf dem
  Host, legt Docker an ihrer Stelle ein **Verzeichnis** an. Die Datei einfach
  zu loeschen haette also einen Lesefehler und ein rotes Banner gebracht.

**Entscheidung.**

* Die Datei liegt jetzt als `beispiele/stadion-intercom.yaml`, mit einem Kopf,
  der sagt, dass sie nicht aktiv ist und wie man sie verwendet (Shows → Datei
  waehlen, oder fuer die Kommandozeile nach `./config/` kopieren).
* Compose bindet ein **Verzeichnis** ein: `./config:/config:ro`. Im Normalfall
  leer; `setup.sh` legt es an und meldet eine alte `intercom.yaml` im
  Projektverzeichnis samt den zwei Moeglichkeiten.
* `intercom export` auf der Kommandozeile schreibt jetzt dasselbe wie der Knopf
  in der Oberflaeche, samt festen Plaetzen, Verbindungen, Ruftasten und Netzen.
  Vorher war eine Sicherung per SSH stillschweigend unvollstaendig.

`admin/tests/test_compose.py` haelt fest, dass keine Datei aus dem Repository
als Vorgabe eingebunden wird und dass das Beispiel gueltig ist.

---

## D-035 - Die Anwendung berechtigt ihren Monitor-Bot selbst -- mit `Register`, nicht `Ban`

**Problem.** Auf einer frischen Installation blieb die Verlustspalte im Cockpit
fuer jeden Client auf einem Platz leer. Zwei Fehler lagen uebereinander:

1. D-015 setzte auf eine Gruppe `monitor` mit `Ban` am obersten Platz -- aus
   der `intercom.yaml`. Seit D-029 entsteht ein Aufbau aus Baukaesten und der
   Oberflaeche; die Gruppe gab es auf keinem Server mehr, und in der
   Oberflaeche gab es keinen Weg, dem Bot ein Recht zu geben.
2. Das Recht war ohnehin das falsche. `Server::msgUserStats` in murmur 1.5.735
   (`src/murmur/Messages.cpp`):

   ```cpp
   bool extend = (uSource == pDstServerUser)
                 || hasPermission(uSource, qhChannels.value(0), ChanACL::Register);
   bool local  = extend || (pDstServerUser->cChannel == uSource->cChannel);
   ```

   Die Paketzaehler gibt es nur bei `local`. Fehlt das Recht, kommt **keine**
   Fehlermeldung -- die Zaehler fehlen einfach.

**Gemessen** (`test_monitor_braucht_register_am_obersten_platz_nicht_ban`, echter
murmur, Beobachter mit Zertifikat am obersten Platz, Ziel auf einem anderen):

| Regel am obersten Platz | Zaehler fuer das Ziel |
|---|---|
| keine | nein |
| `$<hash>` erlaubt Ban | **nein** |
| `$<hash>` erlaubt Register | ja |
| Regel entfernt, `sicherstellen()` der Anwendung | ja |

**Entscheidung.** Die Anwendung setzt beim Verbinden, nach jedem Laden einer
Show, nach jedem Speichern in der Fachsicht am obersten Platz und alle zehn
Minuten eine Regel fuer die Gruppe `$<Zertifikats-Hash des Bots>`: erlaubt
`Register`, nur am obersten Platz selbst (`intercom/monitor/berechtigung.py`).

* `$hash` vergleicht murmur mit dem Zertifikat der Sitzung
  (`Group::appliesToUser`, `user.qsHash`) -- keine Registrierung, keine Rolle,
  und das Recht haengt an genau diesem einen Zertifikat.
* Gelesen wird immer, geschrieben nur, wenn die Regel fehlt. Ein Eintrag im
  Protokoll sagt, wann es passiert ist.
* Die Regel **gehoert der Anwendung**: Planer und Export lassen sie in Ruhe
  (`geschuetzt` bzw. `ohne_gruppen`). Ohne das haette jede Show mit Aufraeumen
  sie geloescht, sie waere neu gesetzt worden, und jeder Testlauf danach haette
  eine Aenderung gemeldet -- und der Hash dieser Installation stuende in jeder
  Show.
* Sichtbar statt versteckt: die Fachsicht beschriftet die Regel als
  "Monitor-Bot", und die Kachel "Messung" im Cockpit zeigt "eingeschraenkt",
  wenn die Regel nicht gesetzt werden konnte.

**Der Bot ist Messtechnik, kein Teilnehmer.** Bei der Abnahme an einer frischen
Installation meldete das Cockpit "1 verbunden" -- den Bot, mit den Abzeichen
"unterdrueckt", "selbst stumm", "selbst taub". Er zaehlt jetzt weder bei den
Verbundenen noch im Ping-Median, bei belegten Plaetzen, in der Netzsicht, in
`/metrics` oder in Alarmen; sichtbar bleibt er, als "Monitor-Bot"
gekennzeichnet. Erkannt wird er an seiner Sitzungsnummer, die er selbst meldet
-- nicht am Namen, den auch ein Mensch tragen koennte.

**Was das Recht sonst erlaubt.** `Register` heisst: Personen registrieren. Der
Bot sendet nie etwas ausser `UserStats`-Abfragen, und das ist strukturell
sichergestellt (kein Audio-Ausgang, `send_message` weist Audio ab). Die
Alternative -- `Write` ueber die Gruppe `admin` -- waere viel mehr gewesen.

