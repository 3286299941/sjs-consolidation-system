# -*- coding: utf-8 -*-
"""仓库货架V2迁移：每层分3区域 + 按客户分配"""
import sqlite3

import os
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'consolidation.db'))

with sqlite3.connect(DB_PATH) as conn:
    cur = conn.cursor()

    # 1. 检查是否需要迁移
    cur.execute("PRAGMA table_info(shelf_slots)")
    cols = [r[1] for r in cur.fetchall()]
    if 'zone' in cols and 'customer_id' in cols:
        print("已是最新版本，跳过迁移")
    else:
        print(f"当前列: {cols}")

        # 2. 清除所有包裹的货位关联（因为要删旧slot）
        cur.execute("UPDATE incoming_packages SET shelf_slot_id = NULL")
        print("已清除所有包裹的货位关联")

        # 3. 删除旧货位
        cur.execute("DELETE FROM shelf_slots")
        print("已删除旧货位")

        # 4. 添加新列
        if 'zone' not in cols:
            cur.execute("ALTER TABLE shelf_slots ADD COLUMN zone VARCHAR(5) NOT NULL DEFAULT 'L'")
        if 'customer_id' not in cols:
            cur.execute("ALTER TABLE shelf_slots ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
        print("已添加 zone 和 customer_id 列")

    conn.commit()

    # 5. 创建108个新货位
    rows = ['A', 'B', 'C']
    cols = [1, 2, 3]
    zones = ['L', 'ML', 'M', 'MR', 'R']

    count = 0
    for r in rows:
        for c in cols:
            shelf_code = f'{r}{c}'
            for floor in range(1, 5):
                for zone in zones:
                    slot_label = f'{shelf_code}-{floor}-{zone}'
                    cur.execute(
                        '''INSERT OR IGNORE INTO shelf_slots
                           (shelf_code, floor, zone, slot_label, row, col)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                        (shelf_code, floor, zone, slot_label, r, c)
                    )
                    if cur.rowcount:
                        count += 1
    conn.commit()
    print(f"已创建 {count} 个新货位区域（共180个）")

    # 验证
    cur.execute("SELECT COUNT(*) FROM shelf_slots")
    total = cur.fetchone()[0]
    print(f"当前货位总数: {total}")

    # 统计
    cur.execute("""
        SELECT c.name, COUNT(ip.id) as cnt
        FROM customers c
        JOIN incoming_packages ip ON ip.customer_id = c.id
        WHERE ip.status IN ('已入库', '待打包')
        GROUP BY c.id
        ORDER BY cnt DESC
    """)
    print("\n待分配客户统计:")
    for name, cnt in cur.fetchall():
        print(f"  {name}: {cnt} 件")

print("\n迁移完成！")
