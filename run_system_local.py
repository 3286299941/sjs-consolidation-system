# -*- coding: utf-8 -*-
"""本地演示启动器（系统侧）。
恢复自历史会话：集运管理系统/app.py 经本文件跑在 5100（http, admin/<DEV_ADMIN_PWD_HASH>），
与小程序测试台 run_local_dev.py(5005) 共用同一份 consolidation.db。
用法：python run_system_local.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app

if __name__ == '__main__':
    # 本地演示：关闭 debug 重载，单进程便于管理；端口 5100
    app.run(host='0.0.0.0', port=5100, debug=False)
