"""VIP模块数据库迁移 - 本地运行一次即可"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app, db
import sqlite3

with app.app_context():
    base = os.path.dirname(os.path.abspath(__file__))
    db_files = [os.path.join(base, 'consolidation.db')]
    instance_db = os.path.join(base, 'instance', 'consolidation.db')
    if os.path.exists(instance_db):
        db_files.append(instance_db)

    for db_path in db_files:
        if not os.path.exists(db_path):
            continue
        print(f"Migrating: {db_path}")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        col_info = {"customers": ["is_vip", "phone", "invoice_contact", "invoice_phone", "invoice_address", "invoice_zip", "company_name", "country"],
                    "packed_packages": ["is_vip_solo"]}

        for table, cols in col_info.items():
            cur.execute(f"PRAGMA table_info({table})")
            existing = set(r[1] for r in cur.fetchall())
            for col in cols:
                if col not in existing:
                    if col == 'is_vip_solo':
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} BOOLEAN DEFAULT 0")
                    elif col == 'is_vip':
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} BOOLEAN DEFAULT 0")
                    else:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} VARCHAR(200) DEFAULT ''")
                    print(f"  ✓ {table}.{col} added")

        conn.commit()
        conn.close()
    print("\nMigration complete.")
