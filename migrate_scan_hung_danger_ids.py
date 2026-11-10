"""一次性迁移: 给 customers 表添加 scan_hung_danger_ids 列

背景: models.py 中 Customer 模型定义了 scan_hung_danger_ids 字段，
但 dev 环境的 consolidation.db 缺这一列，会导致 /vip/packed 页面 500 错误。
执行: python migrate_scan_hung_danger_ids.py
"""
import os
import sqlite3
import sys

db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'consolidation.db')
if not os.path.exists(db):
    print(f'❌ 数据库不存在: {db}')
    sys.exit(1)

conn = sqlite3.connect(db)
try:
    cur = conn.cursor()
    cur.execute('PRAGMA table_info(customers)')
    cols = [r[1] for r in cur.fetchall()]
    if 'scan_hung_danger_ids' in cols:
        print('✅ 列已存在，无需迁移')
    else:
        cur.execute('ALTER TABLE customers ADD COLUMN scan_hung_danger_ids TEXT')
        conn.commit()
        print('✅ 已添加 scan_hung_danger_ids 列')
finally:
    conn.close()
