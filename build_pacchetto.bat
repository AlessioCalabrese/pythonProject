@echo off
REM ============================================================================
REM  Crea il pacchetto autoconsistente di FindIndirizzoByCoordinate.
REM
REM  Da lanciare SOLO su questa macchina di sviluppo (serve Python + PyInstaller).
REM  Il risultato e' la cartella dist\GeocodingCoordinate, che contiene tutto il
REM  necessario - interprete Python incluso - e funziona su PC senza Python.
REM ============================================================================
setlocal

set PROGETTO=%~dp0
set PYTHON=%PROGETTO%.venv\Scripts\python.exe
set NOME=GeocodingCoordinate
set USCITA=%PROGETTO%dist\%NOME%

if not exist "%PYTHON%" (
    echo [ERRORE] Interprete non trovato: %PYTHON%
    echo Crea l'ambiente virtuale con: python -m venv .venv
    pause
    exit /b 1
)

echo.
echo [1/4] Verifica di PyInstaller...
"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       non presente, lo installo...
    "%PYTHON%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERRORE] Installazione di PyInstaller non riuscita.
        pause
        exit /b 1
    )
)

echo.
echo [2/4] Pulizia delle build precedenti...
if exist "%PROGETTO%build" rmdir /s /q "%PROGETTO%build"
if exist "%USCITA%" rmdir /s /q "%USCITA%"

echo.
echo [3/4] Compilazione (richiede qualche minuto)...
REM --onedir: avvio immediato. Con --onefile si ottiene un singolo .exe, che va
REM pero' scompattato in una cartella temporanea a ogni avvio (molto piu' lento
REM con pandas).
REM --console: il programma dialoga con l'utente a prompt, la finestra serve.
REM Gli hidden-import coprono i moduli che pandas carica dinamicamente per
REM leggere/scrivere Excel: PyInstaller non riesce a dedurli dal codice.
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --onedir ^
    --console ^
    --name %NOME% ^
    --hidden-import openpyxl ^
    --hidden-import openpyxl.cell._writer ^
    --hidden-import xlrd ^
    "%PROGETTO%FindIndirizzoByCoordinate.py"
if errorlevel 1 (
    echo [ERRORE] Compilazione non riuscita.
    pause
    exit /b 1
)

echo.
echo [4/4] Copia dell'avviatore e delle istruzioni nel pacchetto...
copy /y "%PROGETTO%Avvia_Geocoding.bat" "%USCITA%\" >nul
if exist "%PROGETTO%LEGGIMI_pacchetto.txt" copy /y "%PROGETTO%LEGGIMI_pacchetto.txt" "%USCITA%\LEGGIMI.txt" >nul

echo.
echo ============================================================================
echo  Pacchetto pronto in: %USCITA%
echo.
echo  Per distribuirlo: copia (o comprimi in zip) l'INTERA cartella
echo  %NOME% e sul PC di destinazione lancia Avvia_Geocoding.bat
echo ============================================================================
echo.
pause
endlocal
