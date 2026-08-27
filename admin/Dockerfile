# =============================================================================
#  Admin-GUI des Stadion-Intercoms
#
#  Zwei Stufen. Die erste baut zeroc-ice aus dem Quelltext -- das dauert einige
#  Minuten und braucht einen Compiler. Die zweite nimmt nur das fertige Wheel
#  und bleibt damit ohne Werkzeugkette.
#
#  Warum aus dem Quelltext: zeroc-ice 3.7.11 hat auf PyPI kein Wheel fuer
#  CPython 3.11, und das Distributionspaket python3-zeroc-ice ist gegen genau
#  eine Python-Version gebaut (in Ubuntu 24.04 gegen 3.12, damit hier
#  unbrauchbar). Siehe DECISIONS.md (D-008).
# =============================================================================

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# --- Stufe 1: bauen -----------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG MUMBLE_VERSION=v1.5.735

# libssl-dev und libbz2-dev sind die tatsaechlich benoetigten Bibliotheken --
# Ice verschluesselt selbst und komprimiert seine Nachrichten.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libssl-dev libbz2-dev ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# zeroc-ice zuerst und in einer eigenen Schicht: es ist mit Abstand das
# teuerste Stueck und darf nicht bei jeder Code-Aenderung neu uebersetzt werden.
RUN pip wheel --no-binary :all: --wheel-dir /wheels "zeroc-ice==3.7.11"
RUN pip install --no-index --find-links=/wheels "zeroc-ice==3.7.11"

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
RUN pip wheel --wheel-dir /wheels /build

# --- Stufe 2: Laufzeit --------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG MUMBLE_VERSION=v1.5.735

# curl fuer den HEALTHCHECK, libssl/libbz2 als Laufzeitbibliotheken von Ice.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates libssl3 libbz2-1.0 tini \
 && rm -rf /var/lib/apt/lists/*

# UID 10000 wie beim mumble-server-Image. setup.sh setzt ./server UND
# ./admin-data auf 10000:10000; damit passt der Besitzer auf beiden Seiten und
# niemand muss auf dem NAS raten, wem /data gehoeren soll.
RUN groupadd --gid 10000 intercom \
 && useradd --uid 10000 --gid 10000 --no-create-home --shell /usr/sbin/nologin intercom

# Die von slice2py erzeugten Module liegen ausserhalb von site-packages. Statt
# PYTHONPATH zu setzen -- das jede Umgebung ueberschreiben kann -- wird ein
# .pth in site-packages abgelegt: Python liest es bei jedem Start, egal wie der
# Prozess aufgerufen wurde. Das muss VOR der Pruefung unten stehen, sonst
# findet `import intercom.web.app` das Modul MumbleServer nicht.
COPY --from=builder /opt/intercom/slice /opt/intercom/slice

COPY --from=builder /wheels /wheels
# Die Wheels werden direkt ueber ihren Dateinamen installiert, nicht ueber den
# Paketnamen. Grund: pymumble haengt als direkte Git-URL in den Metadaten, und
# bei einer Aufloesung ueber den Paketnamen wuerde pip diese URL erneut
# anfassen -- dann braeuchte auch das Laufzeit-Image git und Netz. `pip wheel`
# hat in der Builder-Stufe bereits den vollstaendigen Abhaengigkeitsbaum
# gebaut; hier wird er nur noch ausgepackt.
RUN pip install --no-cache-dir --no-deps /wheels/*.whl \
 && rm -rf /wheels \
 && printf '/opt/intercom/slice\n' \
      > "$(python -c 'import site; print(site.getsitepackages()[0])')/intercom-slice.pth" \
 && python -c "import MumbleServer, Ice; print('Slice:', len(Ice.sliceChecksums), 'Pruefsummen')" \
 && python -c "import intercom.cli, intercom.web.app; print('Paket installiert')" \
 && command -v intercom > /dev/null

# Statische Dateien und Vorlagen liegen neben dem Paket, nicht darin: sie
# werden zur Laufzeit ausgeliefert und muessen sich ohne Neubau des Wheels
# austauschen lassen.
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
CMD ["python", "-m", "intercom.web.app"]
