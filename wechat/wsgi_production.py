"""
生产环境小程序后端入口（wsgi）。

与本地 run_local_dev.py 同构：连接生产 SQLite 库、注册 wechat 蓝图。
区别：
  - 不挂载 /dev/* 调试路由（生产环境要关掉）
  - DB 路径由环境变量 CONSOLIDATION_DB 指定，默认生产库
  - WX_* 环境变量必须在 import wechat_api 之前就位（由 systemd EnvironmentFile 注入）

部署位置：/opt/consolidation/wechat/wsgi_production.py
启动：gunicorn -w 2 -b 127.0.0.1:5005 wsgi_production:app

依赖：仅 Flask + SQLAlchemy（现有 venv 已有），不引入 flask_sqlalchemy，
避免新增依赖。wechat_api 只用 db.session.execute / commit / rollback。
"""
import os
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

import wechat_api  # noqa: E402  (模块在导入时读取 WX_APPID/SECRET 等)

DB_PATH = os.environ.get('CONSOLIDATION_DB', '/opt/consolidation/consolidation.db')
engine = create_engine(
    'sqlite:///' + DB_PATH,
    connect_args={'check_same_thread': False},
    pool_pre_ping=True,
)
Session = scoped_session(sessionmaker(bind=engine))


class _DB:
    """wechat_api 只需要 .session（execute/commit/rollback），用最小包装满足。"""

    def __init__(self, session):
        self.session = session


db = _DB(Session())

app = Flask(__name__)
app.register_blueprint(wechat_api.create_wechat_blueprint(db))


@app.teardown_appcontext
def _close_session(exc):
    Session.remove()


@app.route('/healthz')
def healthz():
    return {'status': 'ok'}, 200


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5005)
