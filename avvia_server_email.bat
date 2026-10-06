@echo off
setlocal enabledelayedexpansion
title Email API - server in broadcast
cd /d "%~dp0"

set "PORT=8000"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

echo ==========================================================
echo   EMAIL API - avvio in modalita' broadcast (rete locale)
echo ==========================================================
echo.

rem --- Controlli preliminari -------------------------------------------
if not exist "%PYTHON%" (
    echo [ERRORE] Interprete Python non trovato:
    echo          %PYTHON%
    echo          Verifica che il virtualenv .venv esista.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo [ERRORE] File .env mancante.
    echo          Copia .env.example in .env e compila SMTP_PASSWORD.
    echo.
    pause
    exit /b 1
)

rem --- Avviso se l'API e' esposta senza chiave --------------------------
set "APIKEY="
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
    if /i "%%A"=="EMAIL_API_KEY" set "APIKEY=%%B"
)
if "!APIKEY!"=="" (
    echo [ATTENZIONE] EMAIL_API_KEY non impostata in .env.
    echo              Il server sara' raggiungibile da CHIUNQUE sulla rete
    echo              e potra' inviare e-mail dal tuo account Gmail.
    echo              Per proteggerlo: valorizza EMAIL_API_KEY nel file .env.
    echo.
)

rem --- Indirizzi di ascolto ---------------------------------------------
echo Raggiungibile dagli altri PC in rete a uno di questi indirizzi:
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%B in ("%%A") do echo     http://%%B:%PORT%/docs
)
echo.
echo Da questo PC:  http://127.0.0.1:%PORT%/docs
echo.
echo Se gli altri PC non riescono a connettersi, apri la porta nel firewall
echo eseguendo come amministratore: apri_firewall_email.bat
echo.
echo Premi CTRL+C per fermare il server.
echo ----------------------------------------------------------
echo.

"%PYTHON%" -m uvicorn email_api:app --host 0.0.0.0 --port %PORT%

echo.
echo Server terminato.
pause
