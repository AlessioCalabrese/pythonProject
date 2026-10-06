"""API REST per l'invio di e-mail, costruita sopra il modulo `sendEmail`.

Avvio:
    .venv\\Scripts\\python.exe -m uvicorn email_api:app --reload --port 8000

Documentazione interattiva: http://127.0.0.1:8000/docs

Il corpo e' interpretato come HTML se `body_format` non viene specificato.

Esempio:
    curl -X POST http://127.0.0.1:8000/send-email \\
         -H "Content-Type: application/json" \\
         -d '{"to": "mario@example.it", "subject": "Test", "body": "<b>Ciao!</b>"}'
"""

import os
from typing import List, Literal, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sendEmail import EmailConfigError, EmailError, EmailSendError, send_email

API_KEY = os.getenv("EMAIL_API_KEY")

# Origini autorizzate a chiamare l'API da browser, separate da virgola.
# "*" (default) accetta qualunque origine: comodo in LAN, da restringere se esposto.
CORS_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()
]

app = FastAPI(
    title="Email API",
    description="Servizio generico per l'invio di e-mail via SMTP.",
    version="1.0.0",
)

# Senza questo middleware il preflight OPTIONS inviato dai browser cross-origin
# riceve 405 Method Not Allowed e il POST non viene mai eseguito.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    max_age=3600,
)


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    """Protegge gli endpoint se EMAIL_API_KEY è impostata; altrimenti l'API resta aperta."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key mancante o non valida (header X-API-Key)",
        )


class EmailRequest(BaseModel):
    to: Union[str, List[str]] = Field(..., description="Destinatario/i")
    subject: Optional[str] = Field(
        default=None,
        description="Oggetto del messaggio; se omesso o vuoto usa EMAIL_DEFAULT_SUBJECT",
    )
    body: str = Field(..., description="Corpo del messaggio")
    body_format: Literal["html", "text"] = Field(
        default="html", description="Formato del corpo; se omesso il corpo e' trattato come HTML"
    )
    cc: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Destinatari in copia; se omesso usa EMAIL_DEFAULT_CC "
                    '(ict@cosmopol.it). Passare "" per inviare senza copia.',
    )
    bcc: Optional[Union[str, List[str]]] = None
    reply_to: Optional[str] = None
    sender: Optional[str] = Field(default=None, description="Override del mittente")
    sender_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "to": "destinatario@example.it",
                    "subject": "Report automatico",
                    "body": "<p>Tutto regolare.</p>",
                }
            ]
        }
    }


class EmailResponse(BaseModel):
    status: str
    message_id: str
    sent_at: str = Field(..., description="Data/ora dell'invio (ISO 8601, ora locale)")
    sender: str = Field(..., alias="from")
    to: List[str]
    cc: List[str]
    bcc: List[str]
    subject: str

    model_config = {"populate_by_name": True}


@app.get("/health", tags=["service"])
def health():
    return {"status": "ok"}


@app.post(
    "/send-email",
    response_model=EmailResponse,
    response_model_by_alias=True,
    tags=["email"],
    dependencies=[Depends(require_api_key)],
)
def send_email_endpoint(payload: EmailRequest):
    """Invia un'e-mail con destinatario, oggetto e corpo indicati nella richiesta."""
    try:
        result = send_email(
            payload.to,
            payload.subject,
            payload.body,
            body_format=payload.body_format,
            cc=payload.cc,
            bcc=payload.bcc,
            reply_to=payload.reply_to,
            sender=payload.sender,
            sender_name=payload.sender_name,
        )
    except EmailConfigError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except EmailSendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except EmailError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    return result
