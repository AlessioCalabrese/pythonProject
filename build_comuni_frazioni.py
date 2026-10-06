#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Costruisce un CSV unico con TUTTI i comuni, le province e le frazioni italiane
con latitudine e longitudine.

Fonti (entrambe gratuite):
  1) Garda Informatica - DB comuni italiani (derivato da ISTAT, licenza MIT)
     -> comuni, province, regioni, codici ISTAT/Belfiore, lat/lon
  2) GeoNames - dump Italia (licenza CC BY 4.0)
     -> localita' abitate = frazioni, con lat/lon

Uso:
    pip install requests        # opzionale, altrimenti usa urllib
    python build_comuni_frazioni.py

Output:
    italia_comuni_frazioni.csv

Colonne:
    tipo                 comune | frazione
    nome                 nome della localita'
    comune               comune di appartenenza
    codice_istat_comune  codice ISTAT a 6 cifre del comune
    provincia            denominazione provincia / citta' metropolitana
    sigla_provincia      sigla automobilistica (NA, MI, RM, ...)
    regione              denominazione regione
    lat                  latitudine WGS84, pronta per DECIMAL(8,6)
    lon                  longitudine WGS84, pronta per DECIMAL(9,6)
    fonte                istat | geonames

Le coordinate sono gia' arrotondate a 6 decimali con ROUND_HALF_UP e validate
nei range -90..90 / -180..180, quindi il CSV e' caricabile direttamente in
colonne MySQL DECIMAL(8,6) e DECIMAL(9,6) senza troncamenti o warning.
"""

import csv
import io
import math
import os
import sys
import unicodedata
import zipfile
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from urllib.request import urlopen, Request

# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------

# Verifica su https://www.gardainformatica.it/database-comuni-italiani che
# questo sia l'ultimo pacchetto disponibile (cambia ad ogni aggiornamento).
URL_COMUNI = "https://www.gardainformatica.it/gi_db_comuni/gi_db_comuni-2026-09-11-f31d2.zip"

URL_GEONAMES = "https://download.geonames.org/export/dump/IT.zip"

OUTPUT = "italia_comuni_frazioni.csv"
CACHE_DIR = "_cache_geo"

# Feature code GeoNames considerati "localita' abitate".
# PPL = populated place, PPLX = quartiere/frazione, PPLA* = capoluoghi.
FEATURE_CODES = {"PPL", "PPLX", "PPLL", "PPLA", "PPLA2", "PPLA3", "PPLA4",
                 "PPLC", "PPLF", "PPLS", "PPLG"}

# Raggio massimo (km) entro cui una localita' GeoNames viene assegnata a un
# comune. Oltre questa distanza la riga viene scartata (dati sporchi/isole).
MAX_DIST_KM = 25.0


# ---------------------------------------------------------------------------
# UTILITY
# ---------------------------------------------------------------------------

def scarica(url, nome_file):
    """Scarica con cache su disco: la seconda esecuzione non riscarica nulla."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, nome_file)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  [cache] {nome_file}")
        return path
    print(f"  [download] {url}")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (script comuni IT)"})
    with urlopen(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return path


def norm(s):
    """Normalizza un nome per il confronto: minuscolo, senza accenti/punteggiatura."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", " ").replace("-", " ").replace(".", " ")
    return " ".join(s.split())


def leggi_csv_da_zip(zf, nome_contiene):
    """Estrae dal file zip il primo CSV il cui nome contiene la stringa data."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = os.path.basename(info.filename).lower()
        if nome_contiene in base and base.endswith(".csv"):
            raw = zf.read(info)
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    testo = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            # il separatore e' ';' in questi dataset, ma restiamo tolleranti
            prima_riga = testo.split("\n", 1)[0]
            sep = ";" if prima_riga.count(";") >= prima_riga.count(",") else ","
            return list(csv.DictReader(io.StringIO(testo), delimiter=sep))
    raise FileNotFoundError(f"Nessun CSV '{nome_contiene}' dentro lo zip")


def to_float(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


# --- conversione a DECIMAL(8,6) / DECIMAL(9,6) ------------------------------

SEI_DECIMALI = Decimal("0.000001")


def _quantizza(valore, limite):
    """Arrotonda a 6 decimali (HALF_UP) e verifica il range. None se non valido."""
    if valore is None:
        return None
    try:
        d = Decimal(str(valore)).quantize(SEI_DECIMALI, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if not (-limite <= d <= limite):
        return None
    if d == 0:  # evita il "-0.000000" prodotto dagli arrotondamenti
        return SEI_DECIMALI * 0
    return d


def dec_lat(valore):
    """Latitudine come DECIMAL(8,6): max 2 cifre intere + 6 decimali."""
    return _quantizza(valore, Decimal("90"))


def dec_lon(valore):
    """Longitudine come DECIMAL(9,6): max 3 cifre intere + 6 decimali."""
    return _quantizza(valore, Decimal("180"))


# ---------------------------------------------------------------------------
# INDICE SPAZIALE (griglia) per trovare il comune piu' vicino velocemente
# ---------------------------------------------------------------------------

class GrigliaComuni:
    CELLA = 0.25  # gradi (~25 km)

    def __init__(self, comuni):
        self.celle = {}
        for c in comuni:
            key = (int(c["lat"] // self.CELLA), int(c["lon"] // self.CELLA))
            self.celle.setdefault(key, []).append(c)

    def piu_vicino(self, lat, lon):
        ci, cj = int(lat // self.CELLA), int(lon // self.CELLA)
        migliore, dmin = None, float("inf")
        raggio = 1
        while raggio <= 4:
            candidati = []
            for i in range(ci - raggio, ci + raggio + 1):
                for j in range(cj - raggio, cj + raggio + 1):
                    candidati.extend(self.celle.get((i, j), ()))
            for c in candidati:
                d = distanza_km(lat, lon, c["lat"], c["lon"])
                if d < dmin:
                    migliore, dmin = c, d
            if migliore is not None:
                return migliore, dmin
            raggio += 1
        return None, float("inf")


def distanza_km(lat1, lon1, lat2, lon2):
    """Haversine."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("1/4 Scarico i dati ISTAT (comuni, province, regioni)...")
    zip_comuni = scarica(URL_COMUNI, "gi_db_comuni.zip")

    with zipfile.ZipFile(zip_comuni) as zf:
        righe_comuni = leggi_csv_da_zip(zf, "comuni")
        righe_province = leggi_csv_da_zip(zf, "province")
        righe_regioni = leggi_csv_da_zip(zf, "regioni")

    regioni = {r["codice_regione"]: r["denominazione_regione"] for r in righe_regioni}
    province = {}
    for p in righe_province:
        province[p["sigla_provincia"]] = {
            "nome": p["denominazione_provincia"],
            "sigla": p["sigla_provincia"],
            "tipologia": p.get("tipologia_provincia", ""),
            "regione": regioni.get(p["codice_regione"], ""),
        }

    comuni = []
    scartati_coord = 0
    for c in righe_comuni:
        lat, lon = to_float(c.get("lat")), to_float(c.get("lon"))
        d_lat, d_lon = dec_lat(lat), dec_lon(lon)
        if d_lat is None or d_lon is None:
            scartati_coord += 1
            continue
        prov = province.get(c["sigla_provincia"], {})
        comuni.append({
            "nome": c.get("denominazione_ita") or c.get("denominazione_ita_altra", ""),
            "codice_istat": c.get("codice_istat", ""),
            "provincia": prov.get("nome", ""),
            "sigla": c.get("sigla_provincia", ""),
            "regione": prov.get("regione", ""),
            "lat": lat,          # float, usato per i calcoli di distanza
            "lon": lon,
            "d_lat": d_lat,      # Decimal(8,6), usato per l'output
            "d_lon": d_lon,      # Decimal(9,6)
        })
    print(f"    -> {len(comuni)} comuni, {len(province)} province, {len(regioni)} regioni")
    if scartati_coord:
        print(f"    -> {scartati_coord} comuni scartati per coordinate mancanti o fuori range")

    print("2/4 Scarico GeoNames (localita' e frazioni)...")
    zip_geo = scarica(URL_GEONAMES, "IT.zip")

    print("3/4 Associo ogni frazione al suo comune...")
    griglia = GrigliaComuni(comuni)

    # nomi dei comuni per provincia, per non duplicare il comune come frazione
    nomi_comune_per_prov = {}
    for c in comuni:
        nomi_comune_per_prov.setdefault(c["sigla"], set()).add(norm(c["nome"]))

    frazioni = []
    visti = set()
    with zipfile.ZipFile(zip_geo) as zf:
        with zf.open("IT.txt") as fh:
            for linea in io.TextIOWrapper(fh, encoding="utf-8"):
                campi = linea.rstrip("\n").split("\t")
                if len(campi) < 15:
                    continue
                nome, lat, lon = campi[1], to_float(campi[4]), to_float(campi[5])
                fclass, fcode = campi[6], campi[7]
                if fclass != "P" or fcode not in FEATURE_CODES:
                    continue
                d_lat, d_lon = dec_lat(lat), dec_lon(lon)
                if d_lat is None or d_lon is None or not nome:
                    continue

                com, dist = griglia.piu_vicino(lat, lon)
                if com is None or dist > MAX_DIST_KM:
                    continue

                n = norm(nome)
                # scarta la localita' che coincide col comune stesso
                if n in nomi_comune_per_prov.get(com["sigla"], ()):
                    continue
                chiave = (n, com["codice_istat"])
                if chiave in visti:
                    continue
                visti.add(chiave)

                frazioni.append({
                    "tipo": "frazione",
                    "nome": nome,
                    "comune": com["nome"],
                    "codice_istat_comune": com["codice_istat"],
                    "provincia": com["provincia"],
                    "sigla_provincia": com["sigla"],
                    "regione": com["regione"],
                    "lat": str(d_lat),
                    "lon": str(d_lon),
                    "fonte": "geonames",
                })
    print(f"    -> {len(frazioni)} frazioni/localita'")

    print("4/4 Scrivo il CSV...")
    campi = ["tipo", "nome", "comune", "codice_istat_comune", "provincia",
             "sigla_provincia", "regione", "lat", "lon", "fonte"]

    with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campi, delimiter=";")
        w.writeheader()
        for c in sorted(comuni, key=lambda x: (x["regione"], x["provincia"], x["nome"])):
            w.writerow({
                "tipo": "comune",
                "nome": c["nome"],
                "comune": c["nome"],
                "codice_istat_comune": c["codice_istat"],
                "provincia": c["provincia"],
                "sigla_provincia": c["sigla"],
                "regione": c["regione"],
                "lat": str(c["d_lat"]),
                "lon": str(c["d_lon"]),
                "fonte": "istat",
            })
        for fr in sorted(frazioni, key=lambda x: (x["regione"], x["provincia"], x["comune"], x["nome"])):
            w.writerow(fr)

    tot = len(comuni) + len(frazioni)
    print(f"\nFatto: {OUTPUT} ({tot} righe)")
    print("\nColonne MySQL suggerite:")
    print("    lat DECIMAL(8,6) NOT NULL,")
    print("    lon DECIMAL(9,6) NOT NULL,")
    print("Attribuzione richiesta: dati comuni da ISTAT via Garda Informatica (MIT), "
          "frazioni da GeoNames (CC BY 4.0).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nErrore: {e}", file=sys.stderr)
        sys.exit(1)
