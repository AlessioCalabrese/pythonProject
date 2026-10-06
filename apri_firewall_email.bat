@echo off
title Email API - regola firewall porta 8000
set "PORT=8000"
set "RULE=Email API (porta %PORT%)"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERRORE] Servono i privilegi di amministratore.
    echo          Tasto destro sul file ^> "Esegui come amministratore".
    echo.
    pause
    exit /b 1
)

echo Creazione regola firewall in ingresso per la porta %PORT% (profilo privato)...
netsh advfirewall firewall delete rule name="%RULE%" >nul 2>&1
netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=%PORT% profile=private

if errorlevel 1 (
    echo.
    echo [ERRORE] Creazione della regola non riuscita.
) else (
    echo.
    echo Regola creata. Per rimuoverla in seguito:
    echo   netsh advfirewall firewall delete rule name="%RULE%"
)
echo.
pause
