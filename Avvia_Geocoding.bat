@echo off
REM ============================================================================
REM  Avvia il programma di reverse geocoding.
REM  Questo file va tenuto nella stessa cartella di GeocodingCoordinate.exe.
REM  Non serve Python installato: l'interprete e' dentro il pacchetto.
REM
REM  Si puo' anche trascinare un file .xlsx o .xls sopra questo .bat: in quel
REM  caso il file viene elaborato senza chiedere nulla.
REM ============================================================================
setlocal

title Reverse geocoding coordinate
cd /d "%~dp0"

REM Percorso assoluto dell'eseguibile: su alcune installazioni la variabile
REM NoDefaultCurrentDirectoryInExePath impedisce a cmd di cercare nella cartella
REM corrente, e un nome "nudo" non verrebbe trovato (errore 9009).
set EXE=%~dp0GeocodingCoordinate.exe

if not exist "%EXE%" (
    echo [ERRORE] GeocodingCoordinate.exe non trovato in questa cartella:
    echo          %~dp0
    echo Copia l'intera cartella del pacchetto, non solo questo file.
    echo.
    pause
    exit /b 1
)

REM %* inoltra l'eventuale file trascinato sopra il .bat.
"%EXE%" %*

REM L'exe fa gia' la sua pausa finale; questa copre i casi di avvio mancato.
if errorlevel 1 (
    echo.
    echo Il programma si e' chiuso segnalando un problema ^(codice %errorlevel%^).
    pause
)
endlocal
