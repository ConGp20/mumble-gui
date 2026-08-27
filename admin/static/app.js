/* ==========================================================================
   Stadion-Intercom – Hilfsfunktionen fuer das Cockpit
   Klassisches Skript ohne Modulsyntax, ohne Abhaengigkeiten. Laeuft NEBEN
   Alpine und HTMX. Alles haengt an window.Intercom.
   ========================================================================== */
(function (global) {
  "use strict";

  /* --- Formatierung ----------------------------------------------------- */

  function fmtZahl(wert, stellen) {
    if (wert === null || wert === undefined || Number.isNaN(wert)) return "–";
    return Number(wert).toFixed(stellen === undefined ? 1 : stellen);
  }

  function fmtPing(ms) {
    if (ms === null || ms === undefined || ms <= 0) return "–";
    return ms < 10 ? ms.toFixed(1) : Math.round(ms).toString();
  }

  function fmtPct(wert) {
    if (wert === null || wert === undefined) return "–";
    return Number(wert).toFixed(wert < 10 ? 1 : 0) + " %";
  }

  function fmtBytes(bytes) {
    if (!bytes) return "0 B";
    var einheiten = ["B", "kB", "MB", "GB"];
    var i = 0;
    var wert = Math.abs(bytes);
    while (wert >= 1024 && i < einheiten.length - 1) { wert /= 1024; i += 1; }
    return (bytes < 0 ? "-" : "") + wert.toFixed(i === 0 ? 0 : 1) + " " + einheiten[i];
  }

  function fmtBits(bps) {
    if (!bps) return "0 bit/s";
    var einheiten = ["bit/s", "kbit/s", "Mbit/s"];
    var i = 0;
    var wert = bps;
    while (wert >= 1000 && i < einheiten.length - 1) { wert /= 1000; i += 1; }
    return wert.toFixed(i === 0 ? 0 : 1) + " " + einheiten[i];
  }

  function fmtDauer(sekunden) {
    if (sekunden === null || sekunden === undefined) return "–";
    var s = Math.max(0, Math.floor(sekunden));
    var t = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (t > 0) return t + " d " + h + " h";
    if (h > 0) return h + " h " + m + " min";
    if (m > 0) return m + " min";
    return s + " s";
  }

  function fmtZeit(unix) {
    if (!unix) return "–";
    var d = new Date(unix * 1000);
    var p = function (n) { return n < 10 ? "0" + n : String(n); };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function fmtDatum(unix) {
    if (!unix) return "–";
    var d = new Date(unix * 1000);
    var p = function (n) { return n < 10 ? "0" + n : String(n); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /* --- Statusfarben ------------------------------------------------------ */

  /* Farbe fuer eine Heatmap-Zelle der Netzsicht. Der Verlauf geht ueber
     Gruen -> Gelb -> Rot; die Grenzen kommen aus ALERT_PING_MS bzw.
     ALERT_LOSS_PCT, damit die Anzeige zu den Alarmen passt. */
  function heatColor(wert, warn, krit) {
    if (wert === null || wert === undefined) return null;
    if (wert <= 0) return null;
    var anteil;
    if (wert <= warn) {
      anteil = (wert / warn) * 0.5;
    } else if (wert >= krit) {
      anteil = 1;
    } else {
      anteil = 0.5 + ((wert - warn) / (krit - warn)) * 0.5;
    }
    /* Farbton von 130 (gruen) nach 0 (rot). Helligkeit bleibt hoch, damit die
       dunkle Schrift auf der Zelle lesbar bleibt. */
    var farbton = Math.round(130 * (1 - anteil));
    return "hsl(" + farbton + ", 62%, 62%)";
  }

  function stufe(wert, warn, krit) {
    if (wert === null || wert === undefined) return "inaktiv";
    if (wert >= krit) return "kritisch";
    if (wert >= warn) return "warnung";
    return "ok";
  }

  /* --- Sparkline --------------------------------------------------------- */

  /* Zeichnet eine Reihe als SVG. `werte` darf null enthalten (Luecken im
     Verlauf, etwa waehrend ein Client getrennt war); solche Punkte
     unterbrechen die Linie, statt sie auf 0 zu ziehen -- eine Linie, die auf
     null faellt, sieht aus wie ein perfekter Wert und ist damit das
     gefaehrlichste Diagramm ueberhaupt. */
  function sparkline(werte, opts) {
    opts = opts || {};
    var breite = opts.breite || 200;
    var hoehe = opts.hoehe || 34;
    var polster = 2;
    var klasse = opts.klasse || "";

    var zahlen = (werte || []).filter(function (w) {
      return w !== null && w !== undefined && !Number.isNaN(w);
    });
    if (zahlen.length < 2) {
      return '<svg class="sparkline ' + klasse + '" viewBox="0 0 ' + breite + ' ' + hoehe +
             '" preserveAspectRatio="none" role="img" aria-label="zu wenig Messwerte">' +
             '<text x="4" y="' + (hoehe / 2 + 4) +
             '" fill="currentColor" opacity=".4" font-size="10">zu wenig Daten</text></svg>';
    }

    var max = opts.max !== undefined ? opts.max : Math.max.apply(null, zahlen);
    var min = opts.min !== undefined ? opts.min : Math.min.apply(null, zahlen);
    if (max === min) { max = min + 1; }

    var n = werte.length;
    var x = function (i) {
      return polster + (i / (n - 1)) * (breite - 2 * polster);
    };
    var y = function (w) {
      return hoehe - polster - ((w - min) / (max - min)) * (hoehe - 2 * polster);
    };

    var linie = "";
    var neu = true;
    for (var i = 0; i < n; i += 1) {
      var w = werte[i];
      if (w === null || w === undefined || Number.isNaN(w)) { neu = true; continue; }
      linie += (neu ? "M" : "L") + x(i).toFixed(1) + " " + y(w).toFixed(1) + " ";
      neu = false;
    }

    var svg = '<svg class="sparkline ' + klasse + '" viewBox="0 0 ' + breite + ' ' + hoehe +
              '" preserveAspectRatio="none" role="img" aria-label="Verlauf, ' +
              'zuletzt ' + fmtZahl(zahlen[zahlen.length - 1]) + '">';

    if (opts.schwelle !== undefined && opts.schwelle >= min && opts.schwelle <= max) {
      var ys = y(opts.schwelle).toFixed(1);
      svg += '<path class="schwelle" d="M' + polster + ' ' + ys + ' L' +
             (breite - polster) + ' ' + ys + '"/>';
    }
    svg += '<path class="linie" d="' + linie.trim() + '"/>';
    svg += "</svg>";
    return svg;
  }

  /* --- Gespeicherte Einstellungen ---------------------------------------- */

  /* localStorage wirft in manchen Umgebungen (privates Fenster, gesperrte
     Website-Daten). Eine geblockte Spaltenauswahl darf das Cockpit nicht
     mitnehmen. */
  function lade(schluessel, vorgabe) {
    try {
      var roh = global.localStorage.getItem(schluessel);
      return roh === null ? vorgabe : JSON.parse(roh);
    } catch (e) {
      return vorgabe;
    }
  }

  function sichere(schluessel, wert) {
    try {
      global.localStorage.setItem(schluessel, JSON.stringify(wert));
    } catch (e) {
      /* bewusst still: eine nicht gespeicherte Ansichtseinstellung ist kein Fehler */
    }
  }

  /* --- Sortier- und Filtertabelle (Alpine) -------------------------------- */

  /* Wird als x-data="Intercom.dataTable(spalten, {schluessel: 'clients'})"
     eingehaengt. Die Zeilen kommen von aussen (aus dem SSE-Strom), damit sie
     bei jedem Ereignis ersetzt werden koennen, ohne Sortierung, Filter oder
     Spaltenauswahl zu verlieren. */
  function dataTable(spalten, opts) {
    opts = opts || {};
    var speicher = opts.schluessel ? "intercom." + opts.schluessel : null;

    return {
      spalten: spalten,
      zeilen: [],
      filter: "",
      sortKey: opts.sortKey || spalten[0].key,
      sortAb: !!opts.sortAb,
      sichtbar: {},
      spaltenOffen: false,

      init: function () {
        var gespeichert = speicher ? lade(speicher + ".spalten", null) : null;
        var self = this;
        this.spalten.forEach(function (s) {
          if (gespeichert && Object.prototype.hasOwnProperty.call(gespeichert, s.key)) {
            self.sichtbar[s.key] = !!gespeichert[s.key];
          } else {
            self.sichtbar[s.key] = s.aus !== true;
          }
        });
        if (speicher) {
          var s = lade(speicher + ".sort", null);
          if (s && s.key) { this.sortKey = s.key; this.sortAb = !!s.ab; }
        }
      },

      sortiereNach: function (key) {
        if (this.sortKey === key) {
          this.sortAb = !this.sortAb;
        } else {
          this.sortKey = key;
          this.sortAb = false;
        }
        if (speicher) { sichere(speicher + ".sort", { key: this.sortKey, ab: this.sortAb }); }
      },

      schalteSpalte: function (key) {
        this.sichtbar[key] = !this.sichtbar[key];
        if (speicher) { sichere(speicher + ".spalten", this.sichtbar); }
      },

      kopfKlasse: function (key) {
        if (this.sortKey !== key) return "sortierbar";
        return "sortierbar " + (this.sortAb ? "sort-ab" : "sort-auf");
      },

      get sichtbareSpalten() {
        var self = this;
        return this.spalten.filter(function (s) { return self.sichtbar[s.key]; });
      },

      /* Sortierung und Filter in einem Durchgang. Der Filter durchsucht nur
         die SICHTBAREN Spalten -- wer eine Spalte ausblendet, will auch nicht
         danach filtern. */
      get gefiltert() {
        var self = this;
        var suche = this.filter.trim().toLowerCase();
        var felder = this.sichtbareSpalten.map(function (s) { return s.key; });

        var ergebnis = this.zeilen.filter(function (zeile) {
          if (!suche) return true;
          for (var i = 0; i < felder.length; i += 1) {
            var wert = zeile[felder[i]];
            if (wert !== null && wert !== undefined &&
                String(wert).toLowerCase().indexOf(suche) !== -1) {
              return true;
            }
          }
          return false;
        });

        var key = this.sortKey;
        var richtung = this.sortAb ? -1 : 1;
        return ergebnis.slice().sort(function (a, b) {
          var x = a[key];
          var y = b[key];
          /* Fehlende Werte immer ans Ende, in BEIDEN Richtungen: ein Client
             ohne Messwert ist keine "beste" und keine "schlechteste" Zeile. */
          var xLeer = x === null || x === undefined || x === "";
          var yLeer = y === null || y === undefined || y === "";
          if (xLeer && yLeer) return 0;
          if (xLeer) return 1;
          if (yLeer) return -1;
          if (typeof x === "number" && typeof y === "number") return (x - y) * richtung;
          if (typeof x === "boolean" && typeof y === "boolean") {
            return ((x ? 1 : 0) - (y ? 1 : 0)) * richtung;
          }
          return String(x).localeCompare(String(y), "de", { numeric: true }) * richtung;
        });
      }
    };
  }

  /* --- Schreibende Aufrufe ------------------------------------------------ */

  /* Jeder schreibende Aufruf braucht das CSRF-Token. Es steht als
     data-csrf am <body> und wird hier automatisch angehaengt, damit es keine
     Route gibt, die es versehentlich vergisst. */
  function csrf() {
    var body = global.document && global.document.body;
    return (body && body.dataset && body.dataset.csrf) || "";
  }

  function anfrage(methode, pfad, koerper) {
    var optionen = {
      method: methode,
      headers: { "X-CSRF-Token": csrf() },
      credentials: "same-origin"
    };
    if (koerper !== undefined) {
      optionen.headers["Content-Type"] = "application/json";
      optionen.body = JSON.stringify(koerper);
    }
    return fetch(pfad, optionen).then(function (antwort) {
      var typ = antwort.headers.get("content-type") || "";
      var daten = typ.indexOf("application/json") !== -1 ? antwort.json() : antwort.text();
      return daten.then(function (inhalt) {
        if (!antwort.ok) {
          var meldung = (inhalt && inhalt.detail) || inhalt ||
                        ("HTTP " + antwort.status);
          throw new Error(meldung);
        }
        return inhalt;
      });
    });
  }

  global.Intercom = {
    fmtZahl: fmtZahl,
    fmtPing: fmtPing,
    fmtPct: fmtPct,
    fmtBytes: fmtBytes,
    fmtBits: fmtBits,
    fmtDauer: fmtDauer,
    fmtZeit: fmtZeit,
    fmtDatum: fmtDatum,
    heatColor: heatColor,
    stufe: stufe,
    sparkline: sparkline,
    dataTable: dataTable,
    lade: lade,
    sichere: sichere,
    anfrage: anfrage,
    get: function (p) { return anfrage("GET", p); },
    post: function (p, k) { return anfrage("POST", p, k === undefined ? {} : k); },
    put: function (p, k) { return anfrage("PUT", p, k); },
    patch: function (p, k) { return anfrage("PATCH", p, k); },
    entferne: function (p) { return anfrage("DELETE", p); }
  };
})(window);
