import sqlite3

# Database file path
db_path = 'deribit_data.db'

# Create SQLite connection
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print("=" * 100)
print("DATABASE SCHEMA")
print("=" * 100)
print()

# Get all table names
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = cursor.fetchall()

# Print schema for each table
for table in tables:
    table_name = table[0]
    print(f"Table: {table_name}")
    print("-" * 100)
    
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    
    print(f"{'Column Name':<35} {'Type':<20} {'Nullable':<12} {'Default':<20}")
    print("-" * 100)
    for col in columns:
        col_name = col[1]
        col_type = col[2]
        not_null = "NOT NULL" if col[3] else "NULL"
        default = str(col[4]) if col[4] else ""
        print(f"{col_name:<35} {col_type:<20} {not_null:<12} {default:<20}")
    
    # Get row count
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]
    print(f"\nRow count: {count}")
    print()
    print("=" * 100)
    print()

print("\n" + "=" * 100)
print("DATABASE DATA")
print("=" * 100)
print()

# Print data from each table
for table in tables:
    table_name = table[0]
    
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]
    
    if count == 0:
        print(f"\nTable: {table_name} (empty)")
        print("-" * 100)
        continue
    
    print(f"\nTable: {table_name} ({count} rows)")
    print("-" * 100)
    
    # Get column names
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    column_names = [col[1] for col in columns]
    
    # Fetch all data
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    
    # Display first 10 rows
    display_count = min(10, count)
    for i, row in enumerate(rows[:display_count]):
        print(f"\nRow {i+1}:")
        for col_name in column_names:
            value = row[col_name]
            if isinstance(value, str) and len(value) > 80:
                value = value[:77] + "..."
            print(f"  {col_name:<30}: {value}")
    
    if count > display_count:
        print(f"\n... ({count - display_count} more rows not shown)")
    
    print()

# Close connection
conn.close()