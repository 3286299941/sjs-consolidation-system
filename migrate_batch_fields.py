# -*- coding: utf-8 -*-
"""迁移脚本：为 batches 表添加 channel_type, delivery_channel, tracking_number 字段"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, 'D:/Lib/site-packages')

from app import app, db
from sqlalchemy import text

with app.app_context():
    with db.engine.connect() as conn:
        # 检查并添加 channel_type 字段
        try:
            conn.execute(text("ALTER TABLE batches ADD COLUMN channel_type VARCHAR(10) DEFAULT ''"))
            conn.commit()
            print("✅ channel_type 字段添加成功")
        except Exception as e:
            if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
                print("⏭️  channel_type 字段已存在，跳过")
            else:
                print(f"⚠️  添加 channel_type 失败: {e}")

        # 检查并添加 delivery_channel 字段
        try:
            conn.execute(text("ALTER TABLE batches ADD COLUMN delivery_channel VARCHAR(100) DEFAULT ''"))
            conn.commit()
            print("✅ delivery_channel 字段添加成功")
        except Exception as e:
            if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
                print("⏭️  delivery_channel 字段已存在，跳过")
            else:
                print(f"⚠️  添加 delivery_channel 失败: {e}")

        # 检查并添加 tracking_number 字段
        try:
            conn.execute(text("ALTER TABLE batches ADD COLUMN tracking_number VARCHAR(100) DEFAULT ''"))
            conn.commit()
            print("✅ tracking_number 字段添加成功")
        except Exception as e:
            if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
                print("⏭️  tracking_number 字段已存在，跳过")
            else:
                print(f"⚠️  添加 tracking_number 失败: {e}")

    print("\n🎉 数据库迁移完成！")
