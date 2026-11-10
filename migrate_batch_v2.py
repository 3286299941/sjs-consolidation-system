# -*- coding: utf-8 -*-
"""迁移脚本：为 batches 表添加邮编和普货/敏货双渠道字段"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'consolidation.db')

new_columns = [
    ('zip_code', 'TEXT', "''", '批次邮编'),
    ('normal_channel', 'TEXT', "''", '普货发货渠道'),
    ('normal_delivery', 'TEXT', "''", '普货派送渠道'),
    ('normal_tracking', 'TEXT', "''", '普货单号'),
    ('sensitive_channel', 'TEXT', "''", '敏货发货渠道'),
    ('sensitive_delivery', 'TEXT', "''", '敏货派送渠道'),
    ('sensitive_tracking', 'TEXT', "''", '敏货单号'),
]


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 检查是否已迁移
    cursor.execute("SELECT name FROM pragma_table_info('batches') WHERE name='zip_code'")
    if cursor.fetchone():
        print("zip_code 字段已存在，跳过迁移")
        conn.close()
        return

    for col_name, col_type, default_val, comment in new_columns:
        try:
            cursor.execute(f"ALTER TABLE batches ADD COLUMN {col_name} {col_type} DEFAULT {default_val}")
            print(f"  + 添加字段 {col_name} ({comment}) — 成功")
        except sqlite3.OperationalError as e:
            print(f"  ! 添加字段 {col_name} 失败: {e}")

    conn.commit()
    conn.close()
    print("\n迁移完成！")


if __name__ == '__main__':
    print(f"数据库路径: {DB_PATH}")
    print(f"数据库存在: {os.path.exists(DB_PATH)}")
    migrate()
