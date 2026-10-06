import os
import glob
import datetime  # AGGIUNTO: Necessario per formattare la data
import shutil    # AGGIUNTO: Necessario per spostare i file
import pandas as pd
from sqlalchemy import create_engine

# 1. Configurazione dei parametri di connessione al database
SERVER = '192.168.254.233'
DATABASE = 'GestCarburanti'
username = 'a.calabrese'
password = 'Cosmopol2022?!'

# STRINGA CORRETTA (Senza spazi iniziali e con il Driver ODBC anziché JDBC)
CONN_STR = f"mssql+pyodbc://{username}:{password}@{SERVER}/{DATABASE}?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no"

# 2. Specifica il path della cartella contenente i file CSV
FOLDER_PATH = r"C:\Users\a.calabrese\Downloads\Driver_code_IP_"
# Specificare qui il percorso dove si desidera archiviare i file processati
ARCHIVE_PATH = r"C:\Users\a.calabrese\Downloads\Driver_code_IP_\Elaborati"

def import_csv_to_sql():
    # Creazione dell'engine di connessione
    engine = create_engine(CONN_STR)
    
    # Assicurati che la cartella di archivio esista, altrimenti creala
    if not os.path.exists(ARCHIVE_PATH):
        os.makedirs(ARCHIVE_PATH)
        
    # Mappatura esatta tra colonne CSV e colonne della Tabella SQL
    column_mapping = {
        'id': 'id',
        'carta': 'carta',
        'numero_targa': 'numero_targa',
        'codice_autista': 'codice_autista',
        'descr_cod_autista': 'descr_cod_autista',
        'username_driver': 'username_driver',
        'nome_driver': 'nome_driver',
        'cognome_driver': 'cognome_driver'
    }

    # Cerca tutti i file .csv nella cartella
    csv_files = glob.glob(os.path.join(FOLDER_PATH, "*.csv"))
    
    if not csv_files:
        print(f"Nessun file CSV trovato nella cartella: {FOLDER_PATH}")
        return

    print(f"Trovati {len(csv_files)} file CSV da elaborare.\n")

    for file_path in csv_files:
        # Estrae il nome del file senza estensione per la colonna 'Azienda'
        file_base_name = os.path.basename(file_path)
        file_name, file_ext = os.path.splitext(file_base_name)
        print(f"Inizio elaborazione file: {file_base_name} (Azienda: {file_name})")
        
        try:
            # Rileva automaticamente se il separatore è ';' o ',' leggendo la prima riga
            with open(file_path, 'r', encoding='utf-8') as f:
                first_line = f.readline()
                sep = ';' if ';' in first_line else ','
            
            # Legge il CSV (specificando 'str' per preservare gli zeri iniziali di carte e codici)
            df = pd.read_csv(file_path, sep=sep, dtype=str)
            
            # Rimuove eventuali righe di istruzioni in coda al file (presenti nei file di template)
            if 'id' in df.columns:
                df = df[df['id'].notna() & df['id'].str.strip().str.isdigit()]
            
            if df.empty:
                print(f"-> File vuoto o contenente solo istruzioni. Saltato.")
                continue

            # Filtra e rinomina le colonne in base alla mappatura
            existing_columns = [col for col in column_mapping.keys() if col in df.columns]
            df_filtered = df[existing_columns].rename(columns=column_mapping)
            
            # Gestione della conversione del codice autista in numerico (int NULL)
            if 'codice_autista' in df_filtered.columns:
                df_filtered['codice_autista'] = pd.to_numeric(df_filtered['codice_autista'], errors='coerce')
            
            # Aggiunge la colonna 'Azienda' con il nome del file
            df_filtered['Azienda'] = file_name
            
            # Carica i dati nel database (appende i dati alla tabella esistente)
            df_filtered.to_sql(
                name='IP_CARD_ON_LINE',
                con=engine,
                schema='dbo',
                if_exists='append',
                index=False
            )
            
            print(f"-> Caricate con successo {len(df_filtered)} righe nel database.")
            
            # --- SEZIONE SPOSTAMENTO E RINOMINA FILE ---
            # Ottiene la data corrente nel formato dd_mm_yyyy
            current_date_str = datetime.datetime.now().strftime("%d_%m_%Y")
            
            # Costruisce il nuovo nome del file: NomeFile_dd_mm_yyyy.csv
            new_file_name = f"{file_name}_{current_date_str}{file_ext}"
            destination_path = os.path.join(ARCHIVE_PATH, new_file_name)
            
            # Rilascia eventuali lock e sposta il file fisico
            shutil.move(file_path, destination_path)
            print(f"📁 File archiviato con successo in: {destination_path}\n")
            
        except Exception as e:
            print(f"❌ Errore durante l'elaborazione del file {file_base_name}: {e}\n")

if __name__ == "__main__":
    import_csv_to_sql()
