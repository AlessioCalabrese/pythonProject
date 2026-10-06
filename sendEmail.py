"""Modulo generico per l'invio di e-mail via SMTP.

Espone una funzione riutilizzabile `send_email()` e una CLI.
Le credenziali vengono lette da variabili d'ambiente (vedi .env.example).

Il corpo del messaggio e' interpretato come HTML salvo diversa indicazione.

Ogni invio (riuscito o fallito) viene registrato con data/ora e destinatari su
stderr e nel file di log (default: logs/email.log, vedi EMAIL_LOG_FILE).

Se 'cc' non viene specificato, la copia va a EMAIL_DEFAULT_CC (default:
ict@cosmopol.it); per inviare senza copia passare cc="".

Esempi:
    from sendEmail import send_email
    send_email("mario@example.it", "Oggetto", "<b>Ciao!</b>")
    send_email(["a@x.it", "b@x.it"], "Report", "Testo semplice", body_format="text")

    python sendEmail.py --to mario@example.it --subject "Test" --body "<b>Ciao</b>"
"""

import argparse
import datetime as dt
import logging
import logging.handlers
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv è opzionale
    pass

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT = 30
DEFAULT_SUBJECT = "Notifica automatica"
# Copia conoscenza applicata quando il chiamante non specifica 'cc'.
# Override con EMAIL_DEFAULT_CC ("none" per non mettere nessuno in copia).
DEFAULT_CC = "ict@cosmopol.it"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Log degli invii: data/ora e destinatari finiscono su console e su file.
# Percorso del file configurabile con EMAIL_LOG_FILE ("none" = solo console).
DEFAULT_LOG_FILE = Path(__file__).resolve().parent / "logs" / "email.log"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5

logger = logging.getLogger("sendEmail")
_log_configured = False


def _setup_logging():
    """Installa gli handler sul logger del modulo, una sola volta.

    Gli handler stanno sul logger del modulo e non sul root, per non interferire
    con la configurazione di uvicorn quando il modulo e' usato da email_api.
    """
    global _log_configured
    if _log_configured:
        return
    _log_configured = True

    logger.setLevel(os.getenv("EMAIL_LOG_LEVEL", "INFO").upper())
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # Variabile assente o vuota -> file di default; "none"/"off" -> solo console.
    percorso = os.getenv("EMAIL_LOG_FILE", "").strip() or str(DEFAULT_LOG_FILE)
    if percorso.lower() in ("none", "off"):
        return
    try:
        log_file = Path(percorso)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # Un log su file non scrivibile non deve impedire l'invio delle e-mail.
        logger.warning("Log su file non disponibile (%s): %s", percorso, exc)


class EmailError(Exception):
    """Errore generico del modulo."""


class EmailConfigError(EmailError):
    """Configurazione SMTP mancante o non valida."""


class EmailSendError(EmailError):
    """Errore durante l'invio del messaggio."""


def _normalize_recipients(value, field):
    """Accetta str (anche separata da , o ;), lista o tupla -> lista di indirizzi."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,;]", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = []
        for item in value:
            candidates.extend(re.split(r"[,;]", str(item)))
    else:
        raise EmailError(f"'{field}' deve essere una stringa o una lista di stringhe")

    recipients = []
    for candidate in candidates:
        address = candidate.strip()
        if not address:
            continue
        if not _EMAIL_RE.match(address):
            raise EmailError(f"Indirizzo non valido in '{field}': {address!r}")
        recipients.append(address)
    return recipients


def load_smtp_config(overrides=None):
    """Costruisce la configurazione SMTP da variabili d'ambiente, con override opzionali."""
    config = {
        "host": os.getenv("SMTP_HOST", DEFAULT_SMTP_HOST),
        "port": int(os.getenv("SMTP_PORT", DEFAULT_SMTP_PORT)),
        "user": os.getenv("SMTP_USER"),
        "password": os.getenv("SMTP_PASSWORD"),
        "sender": os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"),
        "sender_name": os.getenv("SMTP_FROM_NAME"),
        "timeout": int(os.getenv("SMTP_TIMEOUT", DEFAULT_TIMEOUT)),
    }
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})

    missing = [key for key in ("host", "user", "password") if not config.get(key)]
    if missing:
        env_names = {"host": "SMTP_HOST", "user": "SMTP_USER", "password": "SMTP_PASSWORD"}
        raise EmailConfigError(
            "Configurazione SMTP incompleta, variabili mancanti: "
            + ", ".join(env_names[key] for key in missing)
        )
    if not config.get("sender"):
        config["sender"] = config["user"]
    return config


def send_email(
    to,
    subject=None,
    body="",
    *,
    body_format="html",
    cc=None,
    bcc=None,
    reply_to=None,
    sender=None,
    sender_name=None,
    smtp=None,
):
    """Invia un'e-mail e restituisce un dict con l'esito.

    Args:
        to: destinatario/i (stringa, stringa separata da virgole, o lista).
        subject: oggetto del messaggio; se omesso o vuoto usa EMAIL_DEFAULT_SUBJECT.
        body: corpo del messaggio.
        body_format: "html" (default) o "text".
        cc / bcc: destinatari aggiuntivi (stesso formato di `to`). Se `cc` e' omesso
            viene usato EMAIL_DEFAULT_CC (default: ict@cosmopol.it); per inviare
            senza copia passare cc="".
        reply_to: indirizzo per le risposte.
        sender / sender_name: mittente, default da SMTP_FROM / SMTP_USER.
        smtp: dict di override della configurazione (host, port, user, password, timeout).

    Raises:
        EmailError: destinatari o parametri non validi.
        EmailConfigError: credenziali SMTP mancanti.
        EmailSendError: errore di connessione, autenticazione o invio.
    """
    _setup_logging()

    if body_format not in ("text", "html"):
        raise EmailError("body_format deve essere 'text' o 'html'")
    subject = (subject or "").strip() or os.getenv("EMAIL_DEFAULT_SUBJECT", DEFAULT_SUBJECT)

    # 'cc' omesso (None) -> copia di default; cc="" o cc=[] -> nessuna copia.
    cc_di_default = cc is None
    if cc_di_default:
        cc = os.getenv("EMAIL_DEFAULT_CC", DEFAULT_CC).strip()
        if cc.lower() in ("none", "off"):
            cc = ""

    to_list = _normalize_recipients(to, "to")
    cc_list = _normalize_recipients(cc, "cc")
    bcc_list = _normalize_recipients(bcc, "bcc")

    if cc_di_default:
        # Evita che l'indirizzo di default compaia sia in To che in Cc.
        gia_destinatari = {address.lower() for address in to_list}
        cc_list = [a for a in cc_list if a.lower() not in gia_destinatari]
    if not to_list:
        raise EmailError("È richiesto almeno un destinatario in 'to'")

    config = load_smtp_config(
        {
            **(smtp or {}),
            "sender": sender,
            "sender_name": sender_name,
        }
    )

    from_address = config["sender"]
    display_name = config.get("sender_name")

    msg = EmailMessage()
    msg["From"] = formataddr((display_name, from_address)) if display_name else from_address
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if reply_to:
        msg["Reply-To"] = ", ".join(_normalize_recipients(reply_to, "reply_to"))
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id

    if body_format == "html":
        # fallback testuale per i client che non renderizzano HTML
        msg.set_content(re.sub(r"<[^>]+>", " ", body).strip())
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    all_recipients = to_list + cc_list + bcc_list

    try:
        if config["port"] == 465:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=config["timeout"])
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"])
        with server:
            if config["port"] != 465:
                server.starttls()
            server.login(config["user"], config["password"])
            server.send_message(msg, from_addr=from_address, to_addrs=all_recipients)
    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "Invio NON riuscito (autenticazione SMTP) | to=%s | cc=%s | bcc=%s | oggetto=%r | %s",
            ", ".join(to_list) or "-", ", ".join(cc_list) or "-", ", ".join(bcc_list) or "-",
            subject, exc,
        )
        raise EmailSendError(f"Autenticazione SMTP fallita: {exc}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "Invio NON riuscito | to=%s | cc=%s | bcc=%s | oggetto=%r | %s",
            ", ".join(to_list) or "-", ", ".join(cc_list) or "-", ", ".join(bcc_list) or "-",
            subject, exc,
        )
        raise EmailSendError(f"Invio non riuscito: {exc}") from exc

    # Istante effettivo dell'invio, con fuso orario locale (ISO 8601).
    sent_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    # Il log si ferma all'oggetto: niente corpo del messaggio, niente Message-ID.
    logger.info(
        "E-mail inviata | quando=%s | to=%s | cc=%s | bcc=%s | da=%s | oggetto=%r",
        sent_at,
        ", ".join(to_list),
        ", ".join(cc_list) or "-",
        ", ".join(bcc_list) or "-",
        from_address,
        subject,
    )

    return {
        "status": "sent",
        "message_id": message_id,
        "sent_at": sent_at,
        "from": from_address,
        "to": to_list,
        "cc": cc_list,
        "bcc": bcc_list,
        "subject": subject,
    }


def _build_parser():
    parser = argparse.ArgumentParser(description="Invia un'e-mail via SMTP.")
    parser.add_argument("--to", required=True, help="Destinatario/i separati da virgola")
    parser.add_argument(
        "--subject",
        help=f"Oggetto del messaggio (default: EMAIL_DEFAULT_SUBJECT o '{DEFAULT_SUBJECT}')",
    )
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="Corpo del messaggio")
    body_group.add_argument("--body-file", help="File da cui leggere il corpo del messaggio")
    parser.add_argument(
        "--text",
        action="store_true",
        help="Interpreta il corpo come testo semplice (default: HTML)",
    )
    parser.add_argument(
        "--cc",
        help=f"Destinatari in copia (default: EMAIL_DEFAULT_CC o '{DEFAULT_CC}'; "
             "usare --cc \"\" per non mettere nessuno in copia)",
    )
    parser.add_argument("--bcc", help="Destinatari in copia nascosta")
    parser.add_argument("--reply-to", help="Indirizzo per le risposte")
    parser.add_argument("--from-address", help="Mittente (default: SMTP_FROM / SMTP_USER)")
    parser.add_argument("--from-name", help="Nome visualizzato del mittente")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.body_file:
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
    else:
        body = args.body

    try:
        result = send_email(
            args.to,
            args.subject,
            body,
            body_format="text" if args.text else "html",
            cc=args.cc,
            bcc=args.bcc,
            reply_to=args.reply_to,
            sender=args.from_address,
            sender_name=args.from_name,
        )
    except EmailError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1

    print(
        f"E-mail inviata a {', '.join(result['to'])} "
        f"il {result['sent_at']} (id: {result['message_id']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
