# -*- coding: utf-8 -*-
"""WSGI 入口文件 - 供 gunicorn 等生产级 WSGI 服务器使用"""
from app import app

if __name__ == '__main__':
    app.run()
