# =============================================================================
#  Admin-GUI des Stadion-Intercoms
#
#  Zwei Stufen, beide auf debian:bookworm-slim. Die erste holt und uebersetzt
#  die Slice und baut die Python-Abhaengigkeiten, die zweite nimmt nur das
#  Ergebnis und bleibt ohne Werkzeugkette.
#
#  Warum Debian und nicht python:3.11-slim:
#
#  zeroc-ice hat auf PyPI fuer Linux ueberhaupt kein Wheel -- nur ein sdist.
#  `pip install zeroc-ice` uebersetzt also den kompletten C++-Quelltext: hier
#  gemessen gut zehn Minuten auf x86-64, auf einem Raspberry Pi entsprechend
#  laenger, und mit 2 GB Arbeitsspeicher kann der Compiler dabei aussteigen.
#
#  Debian 12 liefert das Paket fertig -- und zwar gegen genau die Python-Version
#  gebaut, die dieses Projekt benutzt:
#
#      python3-zeroc-ice 3.7.8   Depends: python3 (>= 3.11~), python3 (<< 3.12)
#      python3           3.11.2
#
#  Ubuntu taugt dafuer nicht: 24.04 hat Python 3.12 (und ein dagegen gebautes
#  Ice 3.7.10), 22.04 hat 3.10. Nur Debian 12 trifft 3.11. Das betrifft
#  ausschliesslich das Basisimage -- auf welchem System der Container spaeter
#  laeuft, ist davon unberuehrt.
#
#  Ice 3.7.8 statt 3.7.11 ist geprueft, nicht angenommen: gegen murmur 1.5.735
#  meldet Meta.getSliceChecksums() 71 Pruefsummen, davon 0 abweichend und 0
#  fehlend. Siehe DECISIONS.md (D-008, D-027).
# =============================================================================

ARG BASE_IMAGE=debian:bookworm-slim

# --- Stufe 1: bauen -----------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ARG MUMBLE_VERSION=v1.5.735

# python3-zeroc-ice  = die Ice-Laufzeit fuer Python
# zeroc-ice-compilers = slice2py
# libzeroc-ice-dev   = die Ice-eigenen .ice-Dateien unter /usr/share/ice/slice,
#                      die MumbleServer.ice einbindet. Nur hier noetig; die
#                      Stufe wird verworfen.
# build-essential/python3-dev braucht pip nur noch, falls fuer eine
# Abhaengigkeit ausnahmsweise kein Rad vorliegt.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python3-dev \
      python3-zeroc-ice zeroc-ice-compilers libzeroc-ice-dev \
      build-essential ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Optionale eigene Zertifizierungsstellen -- fuer Netze, in denen ein Proxy TLS
# aufbricht. Das Verzeichnis ist im Regelfall leer, dann tut das hier nichts;
# `update-ca-certificates` nimmt nur `*.crt`. Siehe admin/ca/README.md.
#
# Das wirkt AUSSCHLIESSLICH in dieser Stufe. Die Laufzeitstufe laedt nichts mehr
# aus dem Netz und bekommt die Zertifikate nicht -- ein Bau hinter einem
# Firmenproxy erzeugt dasselbe Image wie ein Bau ohne.
#
# PIP_CERT ist noetig, weil pip nicht den Systemspeicher benutzt, sondern das
# mitgelieferte Bundle von certifi. Der Wert ist der Systemspeicher selbst und
# damit auch ohne eigene CA richtig.
COPY ca/ /usr/local/share/ca-certificates/intercom-extra/
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Eigene Umgebung mit --system-site-packages: unsere Abhaengigkeiten kommen aus
# pip, Ice dagegen aus dem Debian-Paket und liegt in
# /usr/lib/python3/dist-packages. Ohne den Schalter saehe die Umgebung es nicht.
# Zugleich umgeht das PEP 668 ("externally-managed-environment"), ohne pip mit
# --break-system-packages an Debians Paketverwaltung vorbeizuschreiben.
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Slice-Datei laden und uebersetzen. Das Skript prueft, dass wirklich eine
# Slice und keine 404-Seite ankam, und erzeugt die Pruefsummen, mit denen die
# Anwendung beim Start gegen Meta.getSliceChecksums() vergleicht.
COPY scripts/build_slice.sh /build/scripts/build_slice.sh
RUN chmod +x /build/scripts/build_slice.sh \
 && /build/scripts/build_slice.sh "${MUMBLE_VERSION}" /opt/intercom/slice

# Frontend-Bibliotheken jetzt holen, nicht zur Laufzeit: das Stadion hat nicht
# immer Internet.
COPY scripts/fetch_vendor.sh /build/scripts/fetch_vendor.sh
RUN chmod +x /build/scripts/fetch_vendor.sh \
 && /build/scripts/fetch_vendor.sh /opt/intercom/static/vendor

# Jetzt erst der Quelltext: alles davor haengt nicht daran und bleibt bei einer
# Code-Aenderung im Schichten-Cache.
#
# Das Wheel wird aus dem VOLLSTAENDIGEN Paket gebaut, nicht aus einem Rumpf --
# sonst laege in site-packages eine leere Huelle, waehrend der echte Code
# daneben im Dateisystem liegt, und `import intercom` haette zwei Kandidaten.
# git wird fuer pymumble gebraucht, siehe pyproject.toml.
COPY pyproject.toml /build/pyproject.toml
COPY intercom/ /build/intercom/
RUN pip install --no-cache-dir /build

# --- Stufe 2: Laufzeit --------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

ARG MUMBLE_VERSION=v1.5.735

# Nur die Laufzeit: python3 und die Ice-Bindung, dazu curl fuer den HEALTHCHECK
# und tini als PID 1. Kein Compiler, kein slice2py, keine Ice-Entwicklerdateien.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-zeroc-ice ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

# UID 10000 wie beim mumble-server-Image. setup.sh setzt ./server UND
# ./admin-data auf 10000:10000; damit passt der Besitzer auf beiden Seiten und
# niemand muss raten, wem /data gehoeren soll.
RUN groupadd --gid 10000 intercom \
 && useradd --uid 10000 --gid 10000 --no-create-home --shell /usr/sbin/nologin intercom

# Die von slice2py erzeugten Module liegen ausserhalb der Umgebung. Statt
# PYTHONPATH zu setzen -- das jede Umgebung ueberschreiben kann -- wird ein
# .pth abgelegt: Python liest es bei jedem Start, egal wie der Prozess
# aufgerufen wurde.
COPY --from=builder /opt/intercom/slice /opt/intercom/slice
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Die Pruefungen bleiben: ein Image, in dem der Import scheitert, soll beim Bau
# auffallen und nicht beim ersten Start im Stadion.
RUN printf '/opt/intercom/slice\n' \
      > "$(python3 -c 'import site; print(site.getsitepackages()[0])')/intercom-slice.pth" \
 && python3 -c "import MumbleServer, Ice; print('Ice', Ice.stringVersion(), '--', len(Ice.sliceChecksums), 'Pruefsummen')" \
 && python3 -c "import intercom.cli, intercom.web.app; print('Paket installiert')" \
 && command -v intercom > /dev/null

# Statische Dateien und Vorlagen liegen neben dem Paket, nicht darin: sie
# werden zur Laufzeit ausgeliefert und muessen sich ohne Neubau austauschen
# lassen.
COPY static/    /opt/intercom/static/
COPY templates/ /opt/intercom/templates/
COPY --from=builder /opt/intercom/static/vendor /opt/intercom/static/vendor
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SLICE_DIR=/opt/intercom/slice \
    DATA_DIR=/data \
    LISTEN_PORT=8080 \
    MUMBLE_VERSION=${MUMBLE_VERSION}

WORKDIR /opt/intercom
RUN mkdir -p /data && chown intercom:intercom /data
USER intercom

# Der Healthcheck prueft nur, dass der Prozess antwortet -- NICHT, ob murmur
# erreichbar ist. Sonst wuerde ein kurz nicht erreichbarer Mumble-Server das
# Cockpit neu starten lassen, und beide kreisen umeinander.
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${LISTEN_PORT}/healthz" > /dev/null || exit 1

# tini als PID 1: uvicorn und die Ice-Threads sollen ein sauberes SIGTERM
# bekommen, damit removeCallback noch durchgeht.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "intercom.web.app"]
