"""
Reverse geocoding di un elenco di coordinate: aggiunge via, civico, comune e
provincia a un file Excel usando Nominatim (OpenStreetMap).

In ingresso accetta sia xlsx/xlsm sia il vecchio xls di Excel 97-2003; il file
prodotto e' sempre xlsx.

Vengono esaminati TUTTI i fogli del file: quelli che contengono le colonne delle
coordinate sono elaborati e ricevono le quattro colonne nuove, gli altri vengono
ricopiati invariati, cosi' il file di output conserva la struttura dell'originale.

Il lavoro e' diviso in due fasi distinte:
  Fase 1 - risoluzione: si estraggono le coordinate DISTINTE di tutti i fogli da
    elaborare, unite in un solo elenco, e si interroga Nominatim una volta per
    ognuna, a blocchi di DIMENSIONE_BLOCCO punti risolti da WORKER_THREADS
    thread, riempiendo la cache. Nessuna riga dell'Excel viene toccata qui.
  Fase 2 - elaborazione: le righe di ogni foglio vengono compilate leggendo solo
    dalla cache (nessuna richiesta di rete) e l'Excel viene scritto una volta
    sola, con tutti i fogli.

Ottimizzazioni:
  - cache delle coordinate (in memoria + su disco in _cache_geo/): una coppia
    lat/lon ripetuta viene cercata una volta sola - anche se compare in fogli
    diversi - e le esecuzioni successive partono da quanto gia' risolto.
  - la cache su disco viene salvata una volta per blocco, non a ogni risposta:
    meno scritture del file, e in caso di interruzione si perde al massimo il
    blocco in corso.
  - i thread NON aumentano le richieste al secondo (il limite di Nominatim resta
    1/s, garantito dal RateLimiter condiviso): coprono il tempo di risposta del
    servizio, che altrimenti si sommerebbe all'attesa obbligatoria.
  - le righe senza coordinate valide non generano alcuna richiesta.
"""

import json
import logging
import os
import sys
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

def cartella_base():
    """
    Cartella di riferimento del programma. Quando il progetto viene impacchettato
    con PyInstaller __file__ punta dentro l'archivio temporaneo, che e' di sola
    lettura e viene cancellato all'uscita: in quel caso serve la cartella
    dell'eseguibile.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = cartella_base()

# Suffisso aggiunto al nome del file di input per ottenere quello di output, che
# viene creato nella stessa cartella dell'originale.
SUFFISSO_OUTPUT = "_ELABORATO"

# Nomi accettati per le colonne delle coordinate: viene usato il primo che esiste
# nell'Excel. Il confronto ignora spazi iniziali/finali e maiuscole/minuscole, perche'
# le intestazioni reali variano ("Lat ", "Lat", "Latitude").
COL_LAT = ("Latitude", "Lat")
COL_LON = ("Longitude", "Lon", "Long")

# Formati accettati in ingresso. L'estensione serve solo a elencare i file nella
# cartella: il motore di lettura viene scelto dai primi byte del file (vedi
# leggi_excel), perche' l'estensione mente spesso.
ESTENSIONI_EXCEL = (".xlsx", ".xlsm", ".xls")

# Firme dei due contenitori: xlsx/xlsm sono archivi ZIP, il vecchio xls di
# Excel 97-2003 e' un contenitore OLE2.
FIRMA_ZIP = b"PK\x03\x04"
FIRMA_OLE = b"\xd0\xcf\x11\xe0"

# La fase 1 lavora a blocchi: i punti del blocco vengono risolti in parallelo e
# alla fine del blocco la cache viene scritta su disco una volta sola (non a ogni
# singola risposta).
DIMENSIONE_BLOCCO = 100

# Thread usati dentro ogni blocco. ATTENZIONE: non aumentano il numero di
# richieste al secondo - il RateLimiter e' condiviso e thread-safe, quindi il
# tetto di 1 richiesta/secondo imposto da Nominatim resta garantito. Servono solo
# a non sommare l'attesa obbligatoria e il tempo di risposta: mentre un thread
# aspetta la risposta, un altro puo' consumare lo slot successivo. Oltre
# (tempo_di_risposta / MIN_DELAY_SECONDS) thread non si guadagna piu' nulla,
# percio' 100 thread darebbero esattamente lo stesso risultato di 4.
WORKER_THREADS = max(1, int(os.getenv("GEO_WORKERS", "4")))

# Cache su disco delle coordinate gia' risolte. Di default sta accanto
# all'eseguibile, cosi' il pacchetto resta trasportabile; se quella cartella non
# e' scrivibile (share di rete in sola lettura, Program Files) si passa al
# profilo utente. imposta_cartella_cache() decide quale usare all'avvio.
CACHE_DIR = BASE_DIR / "_cache_geo"
CACHE_FILE = CACHE_DIR / "reverse_geocode_cache.json"
NOME_CACHE = "reverse_geocode_cache.json"

# Decimali usati per la chiave di cache: 4 equivalgono a ~11 m, quindi rilevazioni
# GPS vicinissime (stesso veicolo fermo nello stesso posto) condividono una sola
# ricerca senza cambiare edificio. Alzare a 6 (~0,1 m) per accorpare solo i
# duplicati esatti; abbassare a 3 (~110 m) dimezza i tempi ma via e civico
# diventano approssimativi.
COORD_DECIMALS = 4

# Nominatim richiede obbligatoriamente max 1 richiesta al secondo: non abbassare
# questo valore, e' il limite imposto dal servizio pubblico.
MIN_DELAY_SECONDS = 1.1

# Secondi di attesa per connessione e risposta. geopy usa 1 secondo di default:
# troppo poco, basta una rete aziendale lenta o un DNS pigro per far scadere la
# connessione prima che venga stabilita (ConnectTimeout con "connect timeout=1").
TIMEOUT_SECONDS = 20

# Quanti tentativi automatici fa il RateLimiter su un errore di rete, e quanto
# aspetta tra uno e l'altro (deve essere >= MIN_DELAY_SECONDS).
RATE_LIMITER_RETRIES = 3
ATTESA_DOPO_ERRORE = 10.0

# Quante volte riprovare, nella stessa esecuzione, un punto che da' errore.
# Lavorando sulle coordinate distinte ogni punto viene affrontato una volta sola,
# quindi i tentativi si esauriscono qui e non si ripetono riga per riga.
MAX_TENTATIVI_PER_PUNTO = 2

# Se arrivano cosi' tanti errori di fila la rete o il servizio sono giu': meglio
# fermarsi salvando il lavoro fatto che continuare a sbattere per ore.
MAX_ERRORI_CONSECUTIVI = 25

# geopy registra l'intero traceback (exc_info=True) a ogni tentativo fallito: su
# una rete instabile l'output diventa illeggibile. Qui zittiamo quei messaggi,
# tanto get_address_details stampa comunque una riga di errore per ogni punto.
logging.getLogger("geopy").setLevel(logging.ERROR)

# Inizializza il geolocalizzatore (inserisci un nome identificativo per lo user_agent)
geolocator = Nominatim(user_agent="geocoding_parco_auto_script", timeout=TIMEOUT_SECONDS)

# swallow_exceptions=False e' essenziale: con il default (True) il RateLimiter,
# esauriti i tentativi, restituisce None SENZA sollevare eccezioni. Un timeout di
# rete sarebbe quindi indistinguibile da "nessun indirizzo trovato" e finirebbe
# in cache come risultato vuoto definitivo. Cosi' invece l'errore arriva fino a
# get_address_details, che restituisce None e lascia il punto fuori dalla cache.
geocode_reverse = RateLimiter(
    geolocator.reverse,
    min_delay_seconds=MIN_DELAY_SECONDS,
    max_retries=RATE_LIMITER_RETRIES,
    error_wait_seconds=ATTESA_DOPO_ERRORE,
    swallow_exceptions=False,
)

VUOTO = (None, None, None, None)


def get_address_details(lat, lon):
    """
    Dato lat e lon, restituisce una tupla (via, civico, comune, provincia)
    estraendo i campi strutturati restituiti da Nominatim.

    Il civico e' presente solo se OpenStreetMap ha un edificio con indirizzo
    vicino al punto: se le coordinate cadono sulla carreggiata Nominatim risponde
    con la sola strada e il civico resta None.

    Restituisce None (non una tupla) se la richiesta e' fallita: in quel caso il
    chiamante non deve mettere nulla in cache, cosi' un errore di rete temporaneo
    non resta memorizzato come risultato definitivo.
    """
    try:
        # language='it' garantisce i nomi italiani; zoom=18 = livello edificio, il
        # piu' dettagliato, quello che puo' restituire anche il numero civico
        location = geocode_reverse((lat, lon), language="it", addressdetails=True, zoom=18)
        if not location or "address" not in location.raw:
            return VUOTO

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
        # I messaggi di rete di urllib3/requests sono lunghissimi e su piu' righe:
        # li riduco a una riga, il tipo di errore e' l'informazione che serve.
        dettaglio = " ".join(str(e).split())[:160] or type(e).__name__
        print(f"Errore su coordinate ({lat}, {lon}): {type(e).__name__}: {dettaglio}",
              file=sys.stderr)
        return None


def trova_colonna(df, nomi):
    """Restituisce il nome reale della prima colonna che corrisponde a uno dei `nomi`,
    ignorando spazi iniziali/finali e maiuscole/minuscole."""
    if isinstance(nomi, str):
        nomi = (nomi,)
    presenti = {str(c).strip().lower(): c for c in df.columns}
    for nome in nomi:
        colonna = presenti.get(nome.strip().lower())
        if colonna is not None:
            return colonna
    raise KeyError(
        f"Nessuna colonna tra {list(nomi)} trovata. "
        f"Colonne presenti: {[str(c) for c in df.columns]}"
    )


def a_numero(serie):
    """Converte in float anche le coordinate scritte come testo con la virgola
    decimale ('40,9054'); i valori non numerici diventano NaN e vengono saltati."""
    if serie.dtype == object or str(serie.dtype).startswith("str"):
        serie = serie.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(serie, errors="coerce")


def chiave_cache(lat, lon):
    """Chiave univoca per una coppia di coordinate; None se non sono valide."""
    if pd.isna(lat) or pd.isna(lon):
        return None
    return f"{round(float(lat), COORD_DECIMALS)},{round(float(lon), COORD_DECIMALS)}"


def imposta_cartella_cache():
    """
    Sceglie la prima cartella scrivibile tra: GEO_CACHE_DIR, la cartella del
    programma e il profilo utente. Aggiorna CACHE_DIR/CACHE_FILE e restituisce
    la cartella scelta (None se nessuna e' utilizzabile: si lavora senza cache).
    """
    global CACHE_DIR, CACHE_FILE

    indicata = os.getenv("GEO_CACHE_DIR", "").strip()
    locale = os.getenv("LOCALAPPDATA", "").strip()
    candidate = []
    if indicata:
        candidate.append(Path(indicata))
    candidate.append(BASE_DIR / "_cache_geo")
    if locale:
        candidate.append(Path(locale) / "FindIndirizzoByCoordinate" / "_cache_geo")

    for cartella in candidate:
        try:
            cartella.mkdir(parents=True, exist_ok=True)
            # mkdir riesce anche su share dove poi la scrittura e' negata:
            # l'unica verifica affidabile e' creare davvero un file.
            prova = cartella / ".scrittura_ok"
            prova.write_text("ok", encoding="utf-8")
            prova.unlink()
        except OSError:
            continue
        CACHE_DIR = cartella
        CACHE_FILE = cartella / NOME_CACHE
        return cartella

    print("[cache] nessuna cartella scrivibile: le coordinate non verranno "
          "memorizzate tra un'esecuzione e l'altra.", file=sys.stderr)
    return None


def _conta(numero, singolare, plurale):
    """'1 foglio' / '3 fogli': il riepilogo e' testo che legge l'utente."""
    return f"{numero} {singolare if numero == 1 else plurale}"


def _ripulisci_percorso(testo):
    """
    Toglie spazi e virgolette da un percorso incollato o trascinato. Rimuove
    anche il BOM, che compare quando il testo arriva da un file o da una pipe
    UTF-8 e renderebbe irriconoscibile un nome altrimenti corretto.
    """
    return testo.replace("﻿", "").strip().strip('"').strip("'").strip()


def _completa_estensione(percorso):
    """
    Il nome e' stato digitato senza estensione: prova quelle Excel conosciute e
    restituisce il primo file che esiste, altrimenti None.
    """
    for estensione in ESTENSIONI_EXCEL:
        candidato = percorso.with_name(percorso.name + estensione)
        if candidato.is_file():
            return candidato
    return None


def chiedi_file_input(argomenti):
    """
    Ricava il file Excel da elaborare. Accetta il percorso come argomento
    (utile per il trascinamento sul .bat), altrimenti lo chiede a prompt:
    prima la cartella (o direttamente il percorso completo), poi il nome.

    Restituisce un Path esistente, o None se l'utente rinuncia.
    """
    if len(argomenti) > 1:
        indicato = Path(_ripulisci_percorso(argomenti[1]))
        if indicato.is_file():
            return indicato
        print(f"Percorso indicato non valido: {indicato}", file=sys.stderr)

    while True:
        print()
        risposta = _ripulisci_percorso(
            input("Cartella del file (o percorso completo del .xlsx/.xls), INVIO per uscire: ")
        )
        if not risposta:
            return None

        percorso = Path(risposta)

        # Percorso completo di un file: non serve chiedere altro.
        if percorso.is_file():
            return percorso

        if not percorso.is_dir():
            # Puo' essere un file senza estensione, o un percorso inesistente.
            con_estensione = _completa_estensione(percorso)
            if con_estensione is not None:
                return con_estensione
            print(f"Non trovo '{percorso}'. Controlla il percorso e riprova.", file=sys.stderr)
            continue

        # E' una cartella: mostro i file disponibili e chiedo il nome. I file che
        # iniziano con ~$ sono i temporanei che Excel crea sui file aperti.
        disponibili = sorted(
            f for f in percorso.iterdir()
            if f.suffix.lower() in ESTENSIONI_EXCEL
            and f.is_file()
            and not f.name.startswith("~$")
            and SUFFISSO_OUTPUT not in f.stem
        )
        if disponibili:
            print(f"\nFile Excel in {percorso}:")
            for f in disponibili:
                print(f"  - {f.name}")
        else:
            print(f"\nNessun file Excel in {percorso}.", file=sys.stderr)

        nome = _ripulisci_percorso(
            input("Nome del file .xlsx o .xls (INVIO per cambiare cartella): ")
        )
        if not nome:
            continue

        candidato = percorso / nome
        if candidato.is_file():
            return candidato
        # Nome digitato senza estensione.
        con_estensione = _completa_estensione(candidato)
        if con_estensione is not None:
            return con_estensione
        print(f"Non trovo '{candidato}'.", file=sys.stderr)


def percorso_output(file_input):
    """
    Nome del file elaborato: stessa cartella e stesso nome dell'originale con il
    suffisso SUFFISSO_OUTPUT. L'estensione e' sempre .xlsx perche' e' l'unico
    formato che openpyxl sa scrivere (un .xls in ingresso resta leggibile).
    """
    return file_input.with_name(f"{file_input.stem}{SUFFISSO_OUTPUT}.xlsx")


def conferma_sovrascrittura(destinazione):
    """True se si puo' scrivere: il file non esiste o l'utente accetta di sostituirlo."""
    if not destinazione.exists():
        return True
    risposta = input(f"\n{destinazione.name} esiste gia': lo sostituisco? [s/N] ").strip().lower()
    return risposta in ("s", "si", "sì", "y", "yes")


def chiedi_foglio(fogli):
    """
    Chiede quale foglio elaborare, quando in nessuno sono state riconosciute le
    colonne delle coordinate. Con un solo foglio non c'e' nulla da scegliere.

    Restituisce il nome del foglio oppure None se l'utente rinuncia.
    """
    nomi = list(fogli)
    if len(nomi) == 1:
        return nomi[0]

    print("\nFogli presenti nel file:")
    for numero, nome in enumerate(nomi, start=1):
        colonne = ", ".join(str(c) for c in fogli[nome].columns[:8])
        print(f"  {numero}. {nome} ({len(fogli[nome])} righe) -> {colonne}")

    while True:
        risposta = _ripulisci_percorso(
            input("Numero o nome del foglio da elaborare (INVIO per uscire): ")
        )
        if not risposta:
            return None
        if risposta.isdigit() and 1 <= int(risposta) <= len(nomi):
            return nomi[int(risposta) - 1]
        for nome in nomi:
            if str(nome).strip().lower() == risposta.lower():
                return nome
        print(f"'{risposta}' non corrisponde a nessun foglio.", file=sys.stderr)


def chiedi_colonne(df):
    """
    Chiede a prompt le colonne delle coordinate, quando il riconoscimento
    automatico non trova nomi noti. Nel pacchetto compilato l'utente non puo'
    modificare COL_LAT/COL_LON, quindi deve poterle indicare qui.

    Restituisce (col_lat, col_lon) oppure None se l'utente rinuncia.
    """
    print("\nColonne presenti nel foglio:")
    for colonna in df.columns:
        print(f"  - {colonna}")

    scelte = []
    for etichetta in ("latitudine", "longitudine"):
        while True:
            nome = _ripulisci_percorso(
                input(f"Nome esatto della colonna {etichetta} (INVIO per uscire): ")
            )
            if not nome:
                return None
            try:
                scelte.append(trova_colonna(df, nome))
                break
            except KeyError:
                print(f"'{nome}' non e' tra le colonne elencate.", file=sys.stderr)
    return tuple(scelte)


def carica_cache():
    """Legge la cache su disco; una cache illeggibile viene semplicemente ignorata."""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as handle:
            dati = json.load(handle)
        # JSON non ha tuple: i valori sono liste, li riporto a tuple di 4 elementi.
        return {k: tuple(v)[:4] for k, v in dati.items() if isinstance(v, list)}
    except (OSError, ValueError) as exc:
        print(f"[cache] file non leggibile, la ricostruisco da zero: {exc}")
        return {}


def salva_cache(cache):
    """Scrive la cache in modo atomico, per non corromperla se il processo muore."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporaneo = CACHE_FILE.with_suffix(".tmp")
        with open(temporaneo, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
        os.replace(temporaneo, CACHE_FILE)
    except OSError as exc:
        print(f"[cache] salvataggio non riuscito: {exc}", file=sys.stderr)


def _formato_reale(percorso):
    """
    Riconosce il formato dai primi byte invece che dall'estensione: molti
    gestionali esportano file chiamati .xls che in realta' sono xlsx, oppure
    tabelle HTML, e scegliendo il motore per estensione la lettura fallirebbe.
    """
    try:
        with open(percorso, "rb") as handle:
            testa = handle.read(8)
    except OSError:
        return None
    if testa.startswith(FIRMA_ZIP):
        return "xlsx"
    if testa.startswith(FIRMA_OLE):
        return "xls"
    return "sconosciuto"


def leggi_excel(percorso):
    """
    Legge TUTTI i fogli del file e restituisce {nome_foglio: DataFrame} nello
    stesso ordine in cui si trovano nel file. Gestisce sia i formati moderni
    xlsx/xlsm (motore openpyxl) sia il vecchio binario xls di Excel 97-2003
    (motore xlrd).

    Solleva RuntimeError con un messaggio comprensibile all'utente finale, che
    nel pacchetto compilato non ha modo di leggere un traceback.
    """
    formato = _formato_reale(percorso)
    if formato is None:
        raise RuntimeError(f"Impossibile aprire '{percorso}'.")
    if formato == "sconosciuto":
        raise RuntimeError(
            f"'{percorso.name}' non e' un file Excel: i primi byte non corrispondono "
            "ne' al formato xlsx ne' al formato xls.\n"
            "Spesso si tratta di esportazioni in HTML o CSV salvate con estensione "
            ".xls: aprile in Excel e risalvale come .xlsx, poi riprova."
        )

    # Avviso quando estensione e contenuto non coincidono: il file viene letto
    # comunque, ma e' utile sapere che il nome e' fuorviante.
    atteso = ".xlsx" if formato == "xlsx" else ".xls"
    if percorso.suffix.lower() not in (atteso, ".xlsm"):
        print(f"[i] '{percorso.name}' ha estensione {percorso.suffix} ma il contenuto e' "
              f"in formato {formato}: lo leggo come {formato}.")

    motore = "openpyxl" if formato == "xlsx" else "xlrd"
    try:
        # sheet_name=None: tutti i fogli, non solo il primo.
        return pd.read_excel(percorso, engine=motore, sheet_name=None)
    except ImportError as exc:
        # Si verifica se il motore non e' installato (o non e' stato incluso nel
        # pacchetto compilato): il messaggio di pandas da solo non lo spiega.
        raise RuntimeError(
            f"Manca la libreria necessaria per leggere i file {formato}: {exc}\n"
            f"Installa il motore '{motore}' (pip install {motore})."
        ) from exc


# Un foglio riconosciuto come elaborabile, con tutto quello che serve alle due
# fasi: le colonne individuate, la chiave di cache di ogni riga (None se la riga
# non ha coordinate valide) e le sue coordinate distinte.
Foglio = namedtuple("Foglio", "nome df col_lat col_lon chiavi coordinate")


def prepara_foglio(nome, df, col_lat, col_lon):
    """
    Calcola per un foglio la chiave di cache di ogni riga e l'elenco delle sue
    coordinate distinte, che e' l'unita' di lavoro della fase 1.
    """
    latitudini = a_numero(df[col_lat])
    longitudini = a_numero(df[col_lon])
    chiavi = [chiave_cache(lat, lon) for lat, lon in zip(latitudini, longitudini)]

    coordinate = {}
    for chiave, lat, lon in zip(chiavi, latitudini, longitudini):
        if chiave is not None and chiave not in coordinate:
            coordinate[chiave] = (lat, lon)

    return Foglio(nome, df, col_lat, col_lon, chiavi, coordinate)


def seleziona_fogli(fogli):
    """
    Cerca in OGNI foglio del file le colonne delle coordinate.

    Restituisce (elaborabili, ignorati): i fogli con entrambe le colonne, e
    quelli saltati con il motivo. I fogli saltati non vengono scartati, vengono
    ricopiati invariati nel file di output.
    """
    elaborabili, ignorati = [], []
    for nome, df in fogli.items():
        if df.empty:
            ignorati.append((nome, "foglio vuoto"))
            continue
        try:
            col_lat = trova_colonna(df, COL_LAT)
            col_lon = trova_colonna(df, COL_LON)
        except KeyError:
            ignorati.append((nome, "nessuna colonna di coordinate"))
            continue
        elaborabili.append(prepara_foglio(nome, df, col_lat, col_lon))
    return elaborabili, ignorati


def aggiungi_colonne(df, risultati):
    """Copia del foglio con le quattro colonne calcolate in piu'."""
    df = df.copy()
    df["Via"] = [r[0] for r in risultati]
    df["Civico"] = [r[1] for r in risultati]
    df["Comune"] = [r[2] for r in risultati]
    df["Provincia"] = [r[3] for r in risultati]
    return df


def scrivi_excel(fogli, elaborati, destinazione, parziale=False):
    """
    Salva l'Excel di output conservando la struttura dell'originale: i fogli
    elaborati con le quattro colonne in piu', gli altri copiati come sono.

    True se il file e' stato scritto.
    """
    try:
        with pd.ExcelWriter(destinazione, engine="openpyxl") as scrittore:
            for nome, df in fogli.items():
                uscita = elaborati.get(nome, df)
                uscita.to_excel(scrittore, sheet_name=nome, index=False)
        return True
    except PermissionError:
        # Tipico su Windows quando il file di output e' aperto in Excel.
        messaggio = f"Impossibile scrivere {destinazione}: chiudi il file in Excel"
        if parziale:
            print(f"[checkpoint] {messaggio}; continuo comunque.", file=sys.stderr)
            return False
        print(f"{messaggio} e riprova.", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"Impossibile scrivere {destinazione}: {exc}", file=sys.stderr)
        return False


def _risolvi_punto(lavoro, evento_stop):
    """
    Eseguito dentro un thread: risolve un singolo punto consumando subito i
    tentativi previsti. Il punto viene affrontato una volta sola in tutta
    l'esecuzione, quindi qui si decide se finira' in cache o no.

    Restituisce (chiave, esito, richieste_fallite); esito None = punto non risolto.
    `evento_stop` permette di abbandonare in fretta il lavoro ancora in coda
    quando il servizio risulta irraggiungibile o l'utente interrompe.
    """
    chiave, (lat, lon) = lavoro
    if evento_stop.is_set():
        return chiave, None, 0

    fallite = 0
    for _ in range(MAX_TENTATIVI_PER_PUNTO):
        esito = get_address_details(lat, lon)
        if esito is not None:
            return chiave, esito, fallite
        fallite += 1
        if evento_stop.is_set():
            break
    return chiave, None, fallite


def cerca_coordinate(da_cercare, coordinate, cache):
    """
    Fase 1: risolve tutte le coordinate distinte non ancora in cache.

    `da_cercare` e' la lista ordinata delle chiavi da risolvere, `coordinate`
    associa a ogni chiave la coppia (lat, lon) da interrogare, `cache` viene
    riempita in memoria e scritta su disco alla fine di ogni blocco.

    I punti di un blocco vengono risolti da piu' thread, che condividono lo
    stesso RateLimiter: il limite di 1 richiesta/secondo resta rispettato, i
    thread servono solo a coprire il tempo di risposta del servizio.

    Restituisce (nuovi, errori, falliti, interrotto, servizio_giu).
    """
    totale = len(da_cercare)
    nuovi = errori = 0
    errori_consecutivi = 0
    falliti = {}            # chiave -> richieste fallite, per il riepilogo finale
    interrotto = servizio_giu = False

    evento_stop = threading.Event()
    lavoratore = partial(_risolvi_punto, evento_stop=evento_stop)
    esecutore = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="geocode")

    print(f"\n[Fase 1/2] Risoluzione di {totale} coordinate distinte, blocchi da "
          f"{DIMENSIONE_BLOCCO} con {WORKER_THREADS} thread "
          f"(stima minima {totale * MIN_DELAY_SECONDS / 60:.1f} min, "
          "limite del servizio: 1 richiesta/secondo)...")

    inizio_fase = time.monotonic()
    try:
        for inizio in range(0, totale, DIMENSIONE_BLOCCO):
            blocco = da_cercare[inizio:inizio + DIMENSIONE_BLOCCO]
            lavori = [(chiave, coordinate[chiave]) for chiave in blocco]

            # map restituisce i risultati nell'ordine dei lavori, non di arrivo:
            # il conteggio degli errori "consecutivi" e' quindi sull'ordine della
            # lista, un'approssimazione sufficiente a capire che il servizio e' giu'.
            for chiave, esito, fallite in esecutore.map(lavoratore, lavori):
                errori += fallite
                if esito is None:
                    if fallite:
                        falliti[chiave] = fallite
                        errori_consecutivi += fallite
                else:
                    cache[chiave] = esito
                    nuovi += 1
                    errori_consecutivi = 0

                if errori_consecutivi >= MAX_ERRORI_CONSECUTIVI and not evento_stop.is_set():
                    print(f"\n{errori_consecutivi} richieste fallite di fila: servizio o rete "
                          "non raggiungibili, chiudo il blocco e salvo il lavoro svolto.",
                          file=sys.stderr)
                    evento_stop.set()   # i punti ancora in coda escono subito
                    servizio_giu = True

            # Fine del blocco: una sola scrittura della cache su disco.
            salva_cache(cache)
            fatti = inizio + len(blocco)
            trascorso = time.monotonic() - inizio_fase
            avanzamento = (f"[blocco {fatti}/{totale}] cache: {len(cache)} punti "
                           f"| risolte: {nuovi} | non risolte: {len(falliti)}")
            # Velocita' e stima solo quando ci sono dati sufficienti: su un blocco
            # finito subito (es. servizio giu') sarebbero numeri senza senso.
            if trascorso >= 1:
                velocita = fatti / trascorso
                avanzamento += (f" | {velocita:.2f} punti/s | stimati "
                                f"{(totale - fatti) / velocita / 60:.1f} min alla fine")
            print(avanzamento)

            if servizio_giu:
                break
    except KeyboardInterrupt:
        interrotto = True
        evento_stop.set()
        print("\nInterrotto: salvo le coordinate risolte finora...")
        salva_cache(cache)
    finally:
        # cancel_futures scarta i punti non ancora avviati senza aspettarli.
        esecutore.shutdown(wait=False, cancel_futures=True)

    return nuovi, errori, falliti, interrotto, servizio_giu


def main(argomenti=()):
    print("=" * 70)
    print(" Reverse geocoding coordinate -> Via, Civico, Comune, Provincia")
    print("=" * 70)

    file_input = chiedi_file_input(argomenti)
    if file_input is None:
        print("Operazione annullata.")
        return 1

    destinazione = percorso_output(file_input)
    if not conferma_sovrascrittura(destinazione):
        print("Operazione annullata: il file esistente non e' stato toccato.")
        return 1

    cartella_cache_usata = imposta_cartella_cache()
    if cartella_cache_usata:
        print(f"Cache coordinate: {CACHE_FILE}")

    print(f"\nCaricamento file {file_input}...")
    try:
        fogli = leggi_excel(file_input)
    except RuntimeError as exc:
        # Messaggio gia' formulato per l'utente da leggi_excel.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Impossibile leggere {file_input}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Tutti i fogli vengono esaminati: quelli con le colonne delle coordinate
    # vengono elaborati, gli altri ricopiati invariati nel file di output.
    da_elaborare, ignorati = seleziona_fogli(fogli)

    if not da_elaborare:
        # Nessun riconoscimento automatico: nel pacchetto compilato le costanti
        # COL_LAT/COL_LON non sono modificabili, quindi si chiede all'utente.
        print(f"\nAttenzione: in nessuno dei {len(fogli)} fogli ho trovato le colonne "
              f"{list(COL_LAT)} e {list(COL_LON)}.", file=sys.stderr)
        nome = chiedi_foglio(fogli)
        if nome is None:
            print("Operazione annullata.")
            return 1
        scelte = chiedi_colonne(fogli[nome])
        if scelte is None:
            print("Operazione annullata.")
            return 1
        da_elaborare = [prepara_foglio(nome, fogli[nome], *scelte)]
        ignorati = [(n, motivo) for n, motivo in ignorati if n != nome]

    print(f"\nFogli nel file: {len(fogli)} | da elaborare: {len(da_elaborare)}")
    for foglio in da_elaborare:
        print(f"  [x] {foglio.nome}: {len(foglio.df)} righe, colonne "
              f"'{foglio.col_lat}' / '{foglio.col_lon}', "
              f"{len(foglio.coordinate)} coordinate distinte")
    for nome, motivo in ignorati:
        print(f"  [ ] {nome}: saltato ({motivo}), verra' copiato invariato")

    # Le coordinate distinte dei vari fogli vengono unite: un punto che compare
    # in piu' fogli viene cercato una volta sola.
    coordinate = {}
    for foglio in da_elaborare:
        for chiave, coppia in foglio.coordinate.items():
            coordinate.setdefault(chiave, coppia)

    cache = carica_cache()
    da_cercare = sorted(coordinate.keys() - cache.keys())

    totale = sum(len(foglio.df) for foglio in da_elaborare)
    senza_coordinate = sum(1 for foglio in da_elaborare for k in foglio.chiavi if k is None)
    nota_vuote = ""
    if senza_coordinate:
        nota_vuote = f" | {_conta(senza_coordinate, 'riga', 'righe')} senza coordinate"
    print(f"\nRighe da elaborare: {totale} | "
          f"coordinate valide: {len(coordinate)} distinte{nota_vuote}")
    print(f"Cache: {len(cache)} punti gia' noti -> {len(da_cercare)} da risolvere")

    inizio = time.monotonic()
    nuovi = errori = 0
    falliti = {}
    interrotto = servizio_giu = False

    if da_cercare:
        nuovi, errori, falliti, interrotto, servizio_giu = cerca_coordinate(
            da_cercare, coordinate, cache
        )
    else:
        print("\n[Fase 1/2] Nessuna ricerca necessaria: tutte le coordinate sono in cache.")

    # Fase 2: solo letture dalla cache, nessuna richiesta di rete. Le coordinate
    # non risolte (errori, o interruzione prima di arrivarci) restano vuote.
    print(f"\n[Fase 2/2] Compilazione di {_conta(totale, 'riga', 'righe')} dalla cache"
          f" su {_conta(len(da_elaborare), 'foglio', 'fogli')}...")
    elaborati = {}
    righe_vuote = 0
    for foglio in da_elaborare:
        risultati = [cache.get(k, VUOTO) if k is not None else VUOTO for k in foglio.chiavi]
        elaborati[foglio.nome] = aggiungi_colonne(foglio.df, risultati)
        mancanti = sum(1 for k in foglio.chiavi if k is not None and k not in cache)
        righe_vuote += mancanti
        print(f"  {foglio.nome}: {len(foglio.df)} righe"
              f"{f', {mancanti} senza indirizzo' if mancanti else ''}")

    non_risolte = [k for k in coordinate if k not in cache]

    incompleto = interrotto or servizio_giu
    scritto = scrivi_excel(fogli, elaborati, destinazione, parziale=incompleto)

    durata = time.monotonic() - inizio
    print(f"\nRiepilogo: {_conta(len(elaborati), 'foglio elaborato', 'fogli elaborati')}, "
          f"{nuovi} nuove ricerche, "
          f"{len(coordinate) - len(non_risolte)} coordinate risolte su {len(coordinate)}, "
          f"{errori} errori su {len(falliti)} punti, "
          f"{_conta(senza_coordinate, 'riga', 'righe')} senza coordinate "
          f"(in {durata / 60:.1f} min)")
    if incompleto:
        print(f"Parziale salvato in: {destinazione} - rilancia per completare "
              "(le coordinate gia' risolte arriveranno dalla cache)")
        return 1
    if not scritto:
        return 1
    if non_risolte:
        # Output salvato ma incompleto: non va segnalato come successo pieno.
        print(f"Attenzione: {len(non_risolte)} coordinate non risolte per errori "
              f"({righe_vuote} righe senza indirizzo) in {destinazione}.\n"
              "Rilancia il programma per riprovare solo quelle: il resto arriva dalla cache.")
        return 1
    print(f"\nOperazione completata con successo!\nFile creato: {destinazione}")
    return 0


def avvia():
    """
    Punto di ingresso dell'eseguibile: raccoglie gli imprevisti e tiene aperta la
    finestra, altrimenti un doppio clic sull'exe farebbe sparire ogni messaggio.
    """
    try:
        codice = main(sys.argv)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        codice = 1
    except EOFError:
        # Nessun input disponibile: lanciato senza console interattiva.
        print("\nNessuna risposta ricevuta: indica il file come argomento "
              "(trascinandolo sul .bat) oppure lancia il programma da una finestra.",
              file=sys.stderr)
        codice = 1
    except Exception as exc:  # errore imprevisto: va mostrato, non nascosto
        import traceback
        traceback.print_exc()
        print(f"\nErrore inatteso: {type(exc).__name__}: {exc}", file=sys.stderr)
        codice = 1

    # Solo nel pacchetto compilato: lanciato da un .bat o a doppio clic, serve
    # una pausa per leggere il risultato. In sviluppo sarebbe solo un fastidio.
    if getattr(sys, "frozen", False):
        try:
            input("\nPremi INVIO per chiudere...")
        except (EOFError, KeyboardInterrupt):
            pass
    return codice


if __name__ == "__main__":
    raise SystemExit(avvia())
