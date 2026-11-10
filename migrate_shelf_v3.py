"""迁移脚本：给 shelf_slots 加 address_label 字段"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'consolidation.db')
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# 检查列是否已存在
cur.execute("PRAGMA table_info(shelf_slots)")
cols = [r[1] for r in cur.fetchall()]
if 'address_label' not in cols:
    cur.execute("ALTER TABLE shelf_slots ADD COLUMN address_label VARCHAR(50)")
    print("✅ 已添加 address_label 列")
else:
    print("⏭ address_label 列已存在，跳过")

conn.commit()
conn.close()
print("迁移完成")
