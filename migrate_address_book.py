"""地址库 v2 迁移 - 改为普货/敏货双字段"""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app, db

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
        cur.execute("PRAGMA table_info(address_book)")
        existing = set(r[1] for r in cur.fetchall())
        # 新增普货/敏货字段
        new_cols = [
            ('puhuo_consignee', "VARCHAR(50) DEFAULT ''"),
            ('puhuo_phone', "VARCHAR(30) DEFAULT ''"),
            ('puhuo_address', "VARCHAR(200) DEFAULT ''"),
            ('minhuo_consignee', "VARCHAR(50) DEFAULT ''"),
            ('minhuo_phone', "VARCHAR(30) DEFAULT ''"),
            ('minhuo_address', "VARCHAR(200) DEFAULT ''"),
        ]
        for col, col_def in new_cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE address_book ADD COLUMN {col} {col_def}")
                print(f"  + {col}")

        # 把旧数据迁移到新字段
        if 'consignee' in existing:
            cur.execute("UPDATE address_book SET puhuo_consignee = consignee WHERE puhuo_consignee = '' AND consignee != ''")
            cur.execute("UPDATE address_book SET puhuo_phone = phone WHERE puhuo_phone = '' AND phone != ''")
            cur.execute("UPDATE address_book SET puhuo_address = address WHERE puhuo_address = '' AND address != ''")
            if 'cargo_type' in existing:
                cur.execute("UPDATE address_book SET minhuo_consignee = consignee WHERE minhuo_consignee = '' AND consignee != '' AND cargo_type = '敏货'")
                cur.execute("UPDATE address_book SET minhuo_phone = phone WHERE minhuo_phone = '' AND phone != '' AND cargo_type = '敏货'")
                cur.execute("UPDATE address_book SET minhuo_address = address WHERE minhuo_address = '' AND address != '' AND cargo_type = '敏货'")
            print("  migrated old data to puhuo_*/minhuo_*")
        conn.commit()
        conn.close()
    print("Migration complete.")
