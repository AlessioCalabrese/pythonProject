"""
Scarica l'export Excel da dbco.cosmopol.it.

Flusso:
  1) POST /api/standard/authenticate/login  -> raccoglie i cookie di sessione
  2) GET  /api/standard/archived/export     -> risposta con il file Excel in base64
  3) GET  /api/standard/stats/stamps        -> export delle statistiche, da cui
     vengono eliminate le righe con la colonna Compagnia valorizzata
  4) decodifica e salva i .xlsx in una cartella dedicata

Uso:
    python scarica_export_cosmopol.py                      # ieri (default)
    python scarica_export_cosmopol.py 2026-09-23           # singolo giorno
    python scarica_export_cosmopol.py 2026-09-01 2026-09-23  # intervallo
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import re
import sys
import warnings
from io import BytesIO
from pathlib import Path

import requests

BASE_URL = "https://dbco.cosmopol.it"
LOGIN_URL = f"{BASE_URL}/api/standard/authenticate/login"
EXPORT_URL = f"{BASE_URL}/api/standard/archived/export"
EXPORT_URL_STATISTICHE = f"{BASE_URL}/api/standard/stats/stamps"

# Credenziali: sovrascrivibili da variabili d'ambiente per non tenerle nel codice.
USERNAME = os.getenv("COSMOPOL_USER", "ict.controllo")
PASSWORD = os.getenv("COSMOPOL_PASS", "iniziale26")

# Cartella di destinazione (unita' di rete mappata). Deve essere un Path, non una str:
# il codice usa OUTPUT_DIR.mkdir() e l'operatore '/' per comporre il nome del file.
OUTPUT_DIR = Path(os.getenv("COSMOPOL_OUTPUT_DIR", r"Z:\timbrature"))
OUTPUT_DIR_ANOMALIA = Path(os.getenv("COSMOPOL_OUTPUT_DIR", r"Z:\anomalie_timbrature"))
TIMEOUT = 120

# ATTENZIONE: dbco.cosmopol.it espone un certificato TLS non valido per quel dominio
# (CN=*.serimat.it, emesso da RapidSSL, scaduto il 18/03/2025). Con la verifica attiva
# qualunque client rifiuta la connessione, quindi qui di default la disattiviamo.
# Quando il certificato verra' sistemato: COSMOPOL_VERIFY_SSL=1 per riattivarla.
VERIFY_SSL = os.getenv("COSMOPOL_VERIFY_SSL", "0") == "1"

# Pulizia dell'export delle statistiche: le righe che hanno un valore in questa
# colonna vengono eliminate dal file salvato.
COLONNA_DA_SCARTARE = "Compagnia"

# Entro quante righe iniziali cercare l'intestazione della tabella.
RIGHE_INTESTAZIONE = 10

# Chiavi in cui l'API potrebbe mettere il base64 del file.
CHIAVI_BASE64 = (
    "file", "fileContent", "fileContents", "base64", "data", "content",
    "result", "value", "excel", "document", "body", "stream", "bytes",
)


def recupera_cookie_malformati(session: requests.Session, resp: requests.Response) -> None:
    """
    Il server manda 'Set-Cookie: ...; domain=; path=/' con l'attributo domain VUOTO.
    E' un header malformato: http.cookiejar lo scarta e la sessione resta senza cookie
    (l'export risponderebbe 401). Qui rileggiamo gli header grezzi e reinseriamo i
    cookie a mano, assegnando il dominio corretto.
    """
    raw = resp.raw.headers
    intestazioni = raw.getlist("Set-Cookie") if hasattr(raw, "getlist") else []
    if not intestazioni:
        singolo = resp.headers.get("Set-Cookie")
        intestazioni = [singolo] if singolo else []

    dominio = requests.utils.urlparse(resp.url).hostname
    for intestazione in intestazioni:
        coppia = intestazione.split(";", 1)[0].strip()
        if "=" not in coppia:
            continue
        nome, _, valore = coppia.partition("=")
        session.cookies.set(nome.strip(), valore.strip(), domain=dominio, path="/")


def login(session: requests.Session) -> None:
    """Esegue la login e lascia i cookie di sessione dentro la Session."""
    resp = session.post(
        LOGIN_URL,
        json={"username": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()

    if not session.cookies:
        recupera_cookie_malformati(session, resp)

    # Alcune API restituiscono anche un token JWT: se c'e', lo usiamo in aggiunta ai cookie.
    token = None
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for chiave in ("token", "accessToken", "access_token", "jwt", "id_token"):
            valore = payload.get(chiave)
            if isinstance(valore, str) and valore:
                token = valore
                break
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    if not session.cookies and not token:
        raise RuntimeError(
            f"Login senza cookie ne' token (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    print(f"[OK] Login riuscita - cookie: {', '.join(session.cookies.keys()) or 'nessuno'}"
          f"{' + token Bearer' if token else ''}")


def cerca_base64(nodo) -> str | None:
    """Cerca ricorsivamente nel JSON la stringa piu' lunga che sembra un base64 di file."""
    migliore = None

    def candidato(valore: str) -> str | None:
        pulito = valore.strip()
        # Gestisce il formato data URI: data:application/...;base64,XXXX
        if pulito.startswith("data:"):
            pulito = pulito.split(",", 1)[-1]
        if len(pulito) < 100 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", pulito):
            return None
        return pulito

    def visita(n, chiave_padre=""):
        nonlocal migliore
        if isinstance(n, str):
            c = candidato(n)
            if c and (migliore is None or len(c) > len(migliore)):
                migliore = c
        elif isinstance(n, dict):
            # Prima le chiavi "note", cosi' vincono in caso di parita'.
            for k in CHIAVI_BASE64:
                if k in n:
                    visita(n[k], k)
            for k, v in n.items():
                if k not in CHIAVI_BASE64:
                    visita(v, k)
        elif isinstance(n, list):
            for v in n:
                visita(v, chiave_padre)

    visita(nodo)
    return migliore


def estrai_bytes(resp: requests.Response) -> bytes:
    """Ricava i byte del file dalla risposta, sia essa JSON/base64, testo base64 o binario."""
    contenuto = resp.content

    # Caso 1: il server ha gia' risposto con il file binario (xlsx = archivio ZIP -> 'PK').
    if contenuto[:2] == b"PK":
        return contenuto

    # Caso 2: JSON con il base64 in un campo.
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    testo_b64 = None
    if payload is not None:
        testo_b64 = cerca_base64(payload)
        if testo_b64 is None:
            raise RuntimeError(
                "JSON ricevuto ma nessun campo base64 riconosciuto. "
                f"Chiavi: {json.dumps(payload, ensure_ascii=False)[:500]}"
            )
    else:
        # Caso 3: il body e' direttamente una stringa base64 (magari tra virgolette).
        testo_b64 = contenuto.decode("utf-8", errors="ignore").strip().strip('"')
        if testo_b64.startswith("data:"):
            testo_b64 = testo_b64.split(",", 1)[-1]

    try:
        dati = base64.b64decode(re.sub(r"\s+", "", testo_b64), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"Base64 non decodificabile: {exc}. Inizio body: {testo_b64[:200]}") from exc

    if dati[:2] != b"PK":
        print(f"[!] Attenzione: i byte decodificati non iniziano con 'PK' (magic: {dati[:8]!r}). "
              "Potrebbe non essere un .xlsx valido.")
    return dati


def _senza_valore(valore) -> bool:
    """True se la cella va considerata priva di valore (vuota o soli spazi)."""
    if valore is None:
        return True
    return isinstance(valore, str) and not valore.strip()


def _trova_intestazione(foglio, titolo: str) -> tuple[int, int] | None:
    """
    Cerca nelle prime RIGHE_INTESTAZIONE righe la cella di intestazione `titolo`
    (ignorando spazi e maiuscole/minuscole): alcuni export mettono una riga di
    titolo prima della tabella, quindi non si puo' dare per scontata la riga 1.

    Restituisce (riga_intestazione, colonna) oppure None se non c'e'.
    """
    atteso = titolo.strip().lower()
    ultima = min(RIGHE_INTESTAZIONE, foglio.max_row or 0)
    for riga in foglio.iter_rows(min_row=1, max_row=ultima):
        for cella in riga:
            if isinstance(cella.value, str) and cella.value.strip().lower() == atteso:
                return cella.row, cella.column
    return None


def _blocchi_contigui(righe: list[int]) -> list[tuple[int, int]]:
    """
    Raggruppa numeri di riga crescenti in blocchi (prima_riga, quante): serve per
    fare una sola delete_rows per blocco invece di una per riga, che su migliaia
    di righe sarebbe molto piu' lento.
    """
    blocchi: list[tuple[int, int]] = []
    for riga in righe:
        if blocchi and riga == blocchi[-1][0] + blocchi[-1][1]:
            inizio, quante = blocchi[-1]
            blocchi[-1] = (inizio, quante + 1)
        else:
            blocchi.append((riga, 1))
    return blocchi


def ripulisci_export(dati: bytes, titolo: str = COLONNA_DA_SCARTARE) -> bytes:
    """
    Elimina dall'Excel tutte le righe che hanno un valore nella colonna `titolo`,
    in ogni foglio in cui quella colonna esiste, e restituisce i byte del file
    ripulito (gli stessi di partenza se non c'era nulla da togliere).

    Lavora in memoria: sul disco - o sulla share di rete - finisce direttamente
    il file definitivo, senza passare da una versione da ribonificare.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        # Errore di configurazione: senza openpyxl ogni esecuzione consegnerebbe
        # un file non ripulito, quindi e' giusto fermarsi.
        raise RuntimeError(
            f"Serve openpyxl per ripulire l'export ({exc}): pip install openpyxl"
        ) from exc

    try:
        # L'export del gestionale non porta stili denominati ("Normal"):
        # openpyxl applica i propri e lo segnala con un UserWarning a ogni
        # apertura ("Workbook contains no default style" / "...no stylesheet").
        # E' innocuo - cambia solo l'aspetto predefinito - e nel log di un task
        # pianificato e' puro rumore, quindi si silenziano QUEI due messaggi,
        # lasciando visibile qualunque altro avviso.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Workbook contains no (stylesheet|default style)",
                category=UserWarning,
            )
            libro = load_workbook(BytesIO(dati))
    except Exception as exc:
        # Il body scaricato non e' un xlsx leggibile (vedi l'avviso di
        # estrai_bytes): la pulizia non ha senso, ma conviene salvare comunque
        # il file cosi' com'e' per poterlo esaminare.
        print(f"[!] Impossibile aprire l'export per la pulizia "
              f"({type(exc).__name__}: {exc}): salvato senza modifiche.",
              file=sys.stderr)
        return dati
    eliminate = 0
    trovata = False

    for foglio in libro.worksheets:
        posizione = _trova_intestazione(foglio, titolo)
        if posizione is None:
            continue
        trovata = True
        riga_intestazione, colonna = posizione

        da_eliminare = [
            riga
            for riga in range(riga_intestazione + 1, (foglio.max_row or 0) + 1)
            if not _senza_valore(foglio.cell(row=riga, column=colonna).value)
        ]
        # Dal basso verso l'alto: eliminando dall'alto gli indici successivi
        # slitterebbero e si cancellerebbero le righe sbagliate.
        for inizio, quante in reversed(_blocchi_contigui(da_eliminare)):
            foglio.delete_rows(inizio, quante)

        eliminate += len(da_eliminare)
        print(f"[OK] Foglio '{foglio.title}': {len(da_eliminare)} righe eliminate "
              f"(colonna '{titolo}' valorizzata)")

    if not trovata:
        print(f"[!] Colonna '{titolo}' non trovata in nessun foglio: "
              "il file viene salvato invariato.")
    if not eliminate:
        libro.close()
        return dati

    uscita = BytesIO()
    libro.save(uscita)
    libro.close()
    return uscita.getvalue()


def scarica_export_statistiche(session: requests.Session, dal: str, al: str, search: str = "") -> Path:
    resp = session.get(
        EXPORT_URL_STATISTICHE,
        params={"from": dal, "to": al, "search": search},
        headers={"Accept": "application/json, text/plain, */*"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    print(f"[OK] Export scaricato - HTTP {resp.status_code}, "
          f"content-type: {resp.headers.get('Content-Type', 'n/d')}, {len(resp.content)} byte")

    dati = estrai_bytes(resp)

    try:
        OUTPUT_DIR_ANOMALIA.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Tipico su unita' di rete: share non raggiungibile, mappatura Z: assente
        # (es. sotto un servizio o task pianificato) o permessi di scrittura mancanti.
        raise RuntimeError(
            f"Impossibile creare/accedere alla cartella '{OUTPUT_DIR_ANOMALIA}': {exc}. "
            "Verifica che l'unita' di rete sia mappata e scrivibile "
            "(in alternativa usa il percorso UNC, es. COSMOPOL_OUTPUT_DIR_ANOMALIA=\\\\\\\\server\\\\share\\\\cartella)."
        ) from exc

    # L'export delle statistiche deve arrivare senza le righe che hanno la
    # colonna Compagnia valorizzata.
    dati = ripulisci_export(dati)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    destinazione = OUTPUT_DIR_ANOMALIA / f"export_STATISTICHE_{dal}_{al}_{stamp}.xlsx"
    destinazione.write_bytes(dati)
    return destinazione


def scarica_export(session: requests.Session, dal: str, al: str, search: str = "") -> Path:
    resp = session.get(
        EXPORT_URL,
        params={"from": dal, "to": al, "search": search},
        headers={"Accept": "application/json, text/plain, */*"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    print(f"[OK] Export scaricato - HTTP {resp.status_code}, "
          f"content-type: {resp.headers.get('Content-Type', 'n/d')}, {len(resp.content)} byte")

    dati = estrai_bytes(resp)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Tipico su unita' di rete: share non raggiungibile, mappatura Z: assente
        # (es. sotto un servizio o task pianificato) o permessi di scrittura mancanti.
        raise RuntimeError(
            f"Impossibile creare/accedere alla cartella '{OUTPUT_DIR}': {exc}. "
            "Verifica che l'unita' di rete sia mappata e scrivibile "
            "(in alternativa usa il percorso UNC, es. COSMOPOL_OUTPUT_DIR=\\\\\\\\server\\\\share\\\\cartella)."
        ) from exc

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    destinazione = OUTPUT_DIR / f"export_{dal}_{al}_{stamp}.xlsx"
    destinazione.write_bytes(dati)
    return destinazione


def main(argv: list[str]) -> int:
    # Senza date in input si usa il giorno precedente a oggi.
    ieri = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    if len(argv) >= 3:
        dal, al = argv[1], argv[2]
    elif len(argv) == 2:
        dal = al = argv[1]
    else:
        dal = al = ieri
        print(f"[i] Nessuna data indicata: uso ieri ({ieri}).")
    search = argv[3] if len(argv) >= 4 else ""

    with requests.Session() as session:
        session.verify = VERIFY_SSL
        if not VERIFY_SSL:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
            print("[!] Verifica certificato TLS DISATTIVATA "
                  "(il server espone un certificato *.serimat.it scaduto).")
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) export-script/1.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        })
        try:
            login(session)
            percorso = scarica_export(session, dal, al, search)
            percorso_statistica = scarica_export_statistiche(session, dal, al)
        except requests.HTTPError as exc:
            resp = exc.response
            print(f"[ERRORE] HTTP {resp.status_code} su {resp.url}\n{resp.text[:500]}", file=sys.stderr)
            return 1
        except (requests.RequestException, RuntimeError, OSError) as exc:
            print(f"[ERRORE] {exc}", file=sys.stderr)
            return 1

    print(f"[FATTO] File salvato in: {percorso}  ({percorso.stat().st_size} byte)")
    print(f"[FATTO] File salvato in: {percorso_statistica}  ({percorso_statistica.stat().st_size} byte)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
