import sys
from pathlib import Path

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# 1. Configurazione file e colonne (percorsi relativi alla cartella dello script,
#    cosi' funziona qualunque sia la cartella da cui viene lanciato)
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "fornitori.xlsx"
OUTPUT_FILE = BASE_DIR / "fornitori_aggiornato.xlsx"

# Nomi delle colonne nell'Excel: il confronto ignora spazi iniziali/finali e maiuscole
# (l'intestazione reale e' "Lat " con uno spazio finale, che causava KeyError: 'Lat').
COL_LAT = "Lat"
COL_LON = "Lon"

# Inizializza il geolocalizzatore (inserisci un nome identificativo per lo user_agent)
geolocator = Nominatim(user_agent="geocoding_parco_auto_script")

# Nominatim richiede obbligatoriamente un limite di max 1 richiesta al secondo
geocode_reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1.1, max_retries=2)

def get_address_details(lat, lon):
    """
    Dato lat e lon, restituisce una tupla (via, civico, comune, provincia)
    estraendo i campi strutturati restituiti da Nominatim.

    Il civico e' presente solo se OpenStreetMap ha un edificio con indirizzo
    vicino al punto: se le coordinate cadono sulla carreggiata Nominatim risponde
    con la sola strada e il civico resta None.
    """
    if pd.isna(lat) or pd.isna(lon):
        return None, None, None, None

    try:
        # language='it' garantisce i nomi italiani; zoom=18 = livello edificio, il
        # piu' dettagliato, quello che puo' restituire anche il numero civico
        location = geocode_reverse((lat, lon), language="it", addressdetails=True, zoom=18)
        if not location or "address" not in location.raw:
            return None, None, None, None

        addr = location.raw["address"]

        # Via e civico in colonne separate
        via = addr.get("road") or None
        civico = addr.get("house_number") or None

        # Estrazione Comune (Nominatim usa diverse chiavi a seconda del tipo di centro abitato)
        comune = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
        )

        # Estrazione Provincia (o sigla contea/distretto)
        provincia = addr.get("county") or addr.get("province") or addr.get("state_district")

        return via, civico, comune, provincia

    except Exception as e:
        print(f"Errore su coordinate ({lat}, {lon}): {e}")
        return None, None, None, None

def trova_colonna(df, nome):
    """Restituisce il nome reale della colonna che corrisponde a `nome`,
    ignorando spazi iniziali/finali e maiuscole/minuscole."""
    for colonna in df.columns:
        if str(colonna).strip().lower() == nome.strip().lower():
            return colonna
    raise KeyError(
        f"Colonna '{nome}' non trovata. Colonne presenti: {[str(c) for c in df.columns]}"
    )


def a_numero(serie):
    """Converte in float anche le coordinate scritte come testo con la virgola
    decimale ('40,9054'); i valori non numerici diventano NaN e vengono saltati."""
    if serie.dtype == object or str(serie.dtype).startswith("str"):
        serie = serie.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(serie, errors="coerce")


def main():
    if not INPUT_FILE.exists():
        print(f"File di input non trovato: {INPUT_FILE}", file=sys.stderr)
        return 1

    print(f"Caricamento file {INPUT_FILE}...")
    df = pd.read_excel(INPUT_FILE)

    try:
        col_lat = trova_colonna(df, COL_LAT)
        col_lon = trova_colonna(df, COL_LON)
    except KeyError as exc:
        print(f"Errore: {exc.args[0]}", file=sys.stderr)
        return 1

    latitudini = a_numero(df[col_lat])
    longitudini = a_numero(df[col_lon])

    # Creazione delle nuove colonne
    vie, civici, comuni, province = [], [], [], []

    totale = len(df)
    print(f"Elaborazione di {totale} righe in corso...")
    # enumerate: il contatore segue la posizione, non l'indice del DataFrame
    for n, (lat, lon) in enumerate(zip(latitudini, longitudini), start=1):
        via, civ, com, prov = get_address_details(lat, lon)
        vie.append(via)
        civici.append(civ)
        comuni.append(com)
        province.append(prov)

        if n % 10 == 0 or n == totale:
            print(f"Avanzamento: {n}/{totale} completati")

    # Aggiunta colonne al DataFrame
    df["Via"] = vie
    df["Civico"] = civici
    df["Comune"] = comuni
    df["Provincia"] = province

    # Salvataggio su file Excel
    try:
        df.to_excel(OUTPUT_FILE, index=False)
    except PermissionError:
        # Tipico su Windows quando il file di output e' aperto in Excel.
        print(f"Impossibile scrivere {OUTPUT_FILE}: chiudi il file in Excel e riprova.",
              file=sys.stderr)
        return 1
    print(f"Operazione completata con successo! Salvato in: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
