import sqlite3
import json
import csv
import os
from pathlib import Path
from datetime import datetime

# Database file path
db_path = 'deribit_data.db'

# Create SQLite connection
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create table for JSON instrument data
cursor.execute('''
CREATE TABLE IF NOT EXISTS deribit_instruments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT,
    price_index TEXT,
    kind TEXT,
    instrument_name TEXT,
    maker_commission REAL,
    taker_commission REAL,
    instrument_type TEXT,
    expiration_timestamp INTEGER,
    creation_timestamp INTEGER,
    is_active INTEGER,
    tick_size REAL,
    contract_size REAL,
    strike REAL,
    instrument_id INTEGER,
    min_trade_amount REAL,
    option_type TEXT,
    block_trade_commission REAL,
    block_trade_min_trade_amount REAL,
    block_trade_tick_size REAL,
    settlement_currency TEXT,
    settlement_period TEXT,
    base_currency TEXT,
    counter_currency TEXT,
    quote_currency TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# Create table for CSV price data
cursor.execute('''
CREATE TABLE IF NOT EXISTS deribit_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT,
    strike REAL,
    expiry TEXT,
    bid_price REAL,
    ask_price REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# Function to load JSON files
def load_json_files():
    data_dir = Path('data_snapshots')
    json_files = list(data_dir.glob('deribit_options_instruments_*.json'))
    
    for json_file in json_files:
        print(f"Loading {json_file}...")
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            for instrument in data:
                cursor.execute('''
                    INSERT INTO deribit_instruments (
                        source_file, price_index, kind, instrument_name,
                        maker_commission, taker_commission, instrument_type,
                        expiration_timestamp, creation_timestamp, is_active,
                        tick_size, contract_size, strike, instrument_id,
                        min_trade_amount, option_type, block_trade_commission,
                        block_trade_min_trade_amount, block_trade_tick_size,
                        settlement_currency, settlement_period, base_currency,
                        counter_currency, quote_currency
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(json_file.name),
                    instrument.get('price_index'),
                    instrument.get('kind'),
                    instrument.get('instrument_name'),
                    instrument.get('maker_commission'),
                    instrument.get('taker_commission'),
                    instrument.get('instrument_type'),
                    instrument.get('expiration_timestamp'),
                    instrument.get('creation_timestamp'),
                    1 if instrument.get('is_active') else 0,
                    instrument.get('tick_size'),
                    instrument.get('contract_size'),
                    instrument.get('strike'),
                    instrument.get('instrument_id'),
                    instrument.get('min_trade_amount'),
                    instrument.get('option_type'),
                    instrument.get('block_trade_commission'),
                    instrument.get('block_trade_min_trade_amount'),
                    instrument.get('block_trade_tick_size'),
                    instrument.get('settlement_currency'),
                    instrument.get('settlement_period'),
                    instrument.get('base_currency'),
                    instrument.get('counter_currency'),
                    instrument.get('quote_currency')
                ))
        except Exception as e:
            print(f"Error loading {json_file}: {e}")

# Function to load CSV files
def load_csv_files():
    # Check both data_snapshots and deribit_option_prices directories
    csv_files = []
    csv_files.extend(Path('data_snapshots').glob('deribit_*.csv'))
    csv_files.extend(Path('deribit_option_prices').glob('deribit_*.csv'))
    
    for csv_file in csv_files:
        print(f"Loading {csv_file}...")
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cursor.execute('''
                        INSERT INTO deribit_prices (
                            source_file, strike, expiry, bid_price, ask_price
                        ) VALUES (?, ?, ?, ?, ?)
                    ''', (
                        str(csv_file.name),
                        float(row['Strike']) if row['Strike'] else None,
                        row['Expiry'],
                        float(row['Bid_Price']) if row['Bid_Price'] else None,
                        float(row['Ask_Price']) if row['Ask_Price'] else None
                    ))
        except Exception as e:
            print(f"Error loading {csv_file}: {e}")

# Load all files
print("Loading JSON instrument files...")
load_json_files()

print("\nLoading CSV price files...")
load_csv_files()

# Commit changes
conn.commit()

# Print summary
cursor.execute('SELECT COUNT(*) FROM deribit_instruments')
instrument_count = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(*) FROM deribit_prices')
price_count = cursor.fetchone()[0]

print(f"\nDatabase created successfully!")
print(f"Total instruments loaded: {instrument_count}")
print(f"Total price records loaded: {price_count}")

# Close connection
conn.close()