# -*- coding: utf-8 -*-
"""迁移：给 incoming_packages 表添加 batch_id 字段"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'consolidation.db')


def migrate():
    if not os.path.exists(DB_PATH):
        print(f'数据库不存在: {DB_PATH}')
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 检查列是否已存在
    cursor.execute("PRAGMA table_info(incoming_packages)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'batch_id' in columns:
        print('batch_id 列已存在，无需迁移')
        conn.close()
        return

    print('正在添加 batch_id 列...')
    cursor.execute('ALTER TABLE incoming_packages ADD COLUMN batch_id INTEGER REFERENCES batches(id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS ix_incoming_packages_batch_id ON incoming_packages(batch_id)')
    conn.commit()
    conn.close()
    print('迁移完成：incoming_packages 表已添加 batch_id 列')


if __name__ == '__main__':
    migrate()
