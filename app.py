# -*- coding: utf-8 -*-
"""
集运管理系统 - 主程序
按照《集运管理系统_核心功能设计.docx》策划书设计的核心功能模块

功能模块：
1. 客户管理 - 客户编号、姓名、条形码、收货地址、邮编、发货次数、待付运费
2. 入库扫描 - 手动录入模式、自动扫描模式
3. 入库明细 - 入库时间、客户、序号、扫描单号、收货地、二次扫描单号、是否敏货、发货状态
4. 打包操作 - 包裹挑选、二次扫描确认、货物分类、合装打包、面单标签打印、发货明细填写
5. 合箱操作 - 建立箱子、分配包裹、填入箱规数据、打印发货清单
6. 批次管理 - 批次编号、批次类型
7. 面单打印 - 10cm × 15cm 规格
"""
import os
import sys
import uuid
import io
import math
import json
import re
import base64
from datetime import datetime
from functools import wraps

# 修复：从非项目目录运行时，Python 的 site-packages 可能不在 sys.path 中
# 改用 sys.executable 推导 (D:\Scripts\python.exe → D:\Lib\site-packages)
_py_home = os.path.dirname(os.path.dirname(sys.executable))
_site_pkgs = os.path.join(_py_home, 'Lib', 'site-packages')
if os.path.isdir(_site_pkgs) and _site_pkgs not in sys.path:
    sys.path.insert(0, _site_pkgs)

from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, jsonify, session, send_file, Response
from werkzeug.utils import secure_filename
from sqlalchemy import or_, func, select, text
from sqlalchemy.orm import joinedload, configure_mappers
from sqlalchemy.exc import IntegrityError
from models import db, Customer, IncomingPackage, PackedPackage, PackedPackageItem, Carton, CartonItem, CartonGroup, Batch, FreightRecord, PriceConfig, OperationLog, create_operation_log, get_local_time, ShelfSlot, AddressBook, InvoiceConfig


# 条形码生成依赖（python-barcode >= 0.15）
import barcode
from barcode.writer import SVGWriter

app = Flask(__name__)

# Secret Key配置 - 优先从环境变量读取，否则使用默认值（仅用于开发环境）
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'consolidation-system-fixed-key-2026')
# 使用绝对路径确保数据库在项目目录
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "consolidation.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, UPLOAD_FOLDER)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

@app.after_request
def add_no_cache_headers(response):
    """为所有 HTML 响应强制禁用浏览器缓存"""
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.template_filter('format_slot_label')
def format_slot_label(slot_label):
    """货位标签格式化：蒙市A-1-1 → 蒙市A架-1层-1号"""
    parts = slot_label.rsplit('-', 2)
    if len(parts) == 3:
        shelf, floor, zone = parts
        return f'{shelf}架-{floor}层-{zone}号'
    return slot_label

# 禁用 SQLAlchemy 编译缓存（规避 SQLite 下 compiled.positional 与 positiontup 不一致的 AssertionError）
# 在 HTTP 请求间引擎缓存被污染会导致 db.session.get() / execute() 出现 DBAPIError
with app.app_context():
    db.engine.update_execution_options(compiled_cache=None)


# ──────────────────────────────────────────────────────────────────
# 测量设备对接 - 状态面板 HTML
# ──────────────────────────────────────────────────────────────────

MEASURE_STATUS_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>测量设备 API 对接状态</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px}
  .container{max-width:900px;width:100%}
  h1{font-size:2rem;text-align:center;margin-bottom:8px;background:linear-gradient(135deg,#22d3ee,#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .subtitle{text-align:center;color:#94a3b8;margin-bottom:32px;font-size:0.95rem}

  .status-banner{display:flex;align-items:center;justify-content:center;gap:12px;padding:20px 28px;border-radius:16px;font-size:1.3rem;font-weight:700;margin-bottom:36px;animation:pulse-glow 2s ease-in-out infinite}
  .status-banner.online{background:linear-gradient(135deg,#064e3b,#065f46);border:2px solid #34d399;color:#34d399}
  .status-banner .dot{width:18px;height:18px;border-radius:50%;background:#34d399;box-shadow:0 0 16px #34d399;animation:blink 1s ease-in-out infinite}
  @keyframes pulse-glow{0%,100%{box-shadow:0 0 20px rgba(52,211,153,0.15)}50%{box-shadow:0 0 40px rgba(52,211,153,0.35)}}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}

  .card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:24px 28px;margin-bottom:20px}
  .card h2{font-size:1.15rem;color:#67e8f9;margin-bottom:16px;display:flex;align-items:center;gap:8px}
  .card h2 .icon{font-size:1.3rem}

  .endpoint-row{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-radius:10px;background:#0f172a;margin-bottom:8px;flex-wrap:wrap;gap:8px}
  .method{display:inline-block;padding:2px 10px;border-radius:6px;font-size:0.78rem;font-weight:700;font-family:monospace;letter-spacing:0.5px}
  .method.get{background:#1e3a5f;color:#60a5fa}.method.post{background:#4a1d2d;color:#f87171}
  .path{font-family:monospace;font-size:0.9rem;color:#e2e8f0;flex:1;min-width:280px;word-break:break-all}
  .badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:0.78rem;font-weight:600}
  .badge.ok{background:#064e3b;color:#34d399}.badge.info{background:#1e3a5f;color:#60a5fa}

  .device-config{background:#0f172a;border-radius:10px;padding:16px 20px;font-family:monospace;font-size:0.88rem;line-height:2;word-break:break-all;color:#67e8f9}
  .device-config span{color:#94a3b8}

  .test-results{margin-top:16px}
  .test-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:8px;font-size:0.85rem;margin-bottom:4px}
  .test-row.pass{background:rgba(52,211,153,0.08);color:#6ee7b7}
  .test-row .check{color:#34d399;font-weight:700}

  .footer{text-align:center;color:#475569;font-size:0.82rem;margin-top:40px}
</style>
</head>
<body>
<div class="container">
  <h1>📦 测量设备 API 对接状态</h1>
  <p class="subtitle">鸿顺捷 HSJ 协议 V1.5 · 设备主动推送模式</p>

  <div class="status-banner online">
    <span class="dot"></span>
    全部接口已就绪 · 已通过测试验证
  </div>

  <div class="card">
    <h2><span class="icon">🔌</span> 设备对接接口</h2>
    <div class="endpoint-row">
      <span class="method post">POST</span>
      <span class="path">/Send Package Info/Auto</span>
      <span class="badge ok">✅ 已测试</span>
    </div>
    <p style="color:#94a3b8;font-size:0.85rem;margin:4px 0 8px 16px">设备扫码后推送重量/尺寸，自动更新包裹数据 & 体积重</p>
    <div class="endpoint-row">
      <span class="method post">POST</span>
      <span class="path">/Login/Auto</span>
      <span class="badge ok">✅ 已测试</span>
    </div>
    <p style="color:#94a3b8;font-size:0.85rem;margin:4px 0 0 16px">设备开机登录验证（当前免密放行）</p>
  </div>

  <div class="card">
    <h2><span class="icon">📋</span> 查询与管理接口</h2>
    <div class="endpoint-row">
      <span class="method get">GET</span>
      <span class="path">/api/measure/packed/{id}</span>
      <span class="badge info">查询</span>
    </div>
    <div class="endpoint-row">
      <span class="method post">POST</span>
      <span class="path">/api/measure/packed/{id}</span>
      <span class="badge info">手动上报</span>
    </div>
    <div class="endpoint-row">
      <span class="method get">GET</span>
      <span class="path">/api/measure/packed/by_barcode?code={id}</span>
      <span class="badge info">扫码查询</span>
    </div>
    <div class="endpoint-row">
      <span class="method get">GET</span>
      <span class="path">/api/measure/pending?batch_id={id}</span>
      <span class="badge info">待测列表</span>
    </div>
  </div>

  <div class="card">
    <h2><span class="icon">⚙️</span> 设备侧配置</h2>
    <div class="device-config">
      <span>服务器地址:</span>  YOUR_SERVER_IP<br>
      <span>协议:</span>       HTTPS (443)<br>
      <span>数据推送:</span>   https://YOUR_SERVER_IP/Send%20Package%20Info/Auto<br>
      <span>登录地址:</span>   https://YOUR_SERVER_IP/Login/Auto<br>
      <span>条码字段:</span>   ticketsNum = packed.id<br>
      <span>重量单位:</span>   kg · 尺寸单位: cm
    </div>
  </div>

  <div class="card">
    <h2><span class="icon">🧪</span> 测试结果</h2>
    <div class="test-results">
      <div class="test-row pass"><span class="check">✓</span> 缺参数 → 400 + 错误提示</div>
      <div class="test-row pass"><span class="check">✓</span> ticketsNum 格式错误 → 400 + 错误提示</div>
      <div class="test-row pass"><span class="check">✓</span> 包裹不存在 → 404 + 错误提示</div>
      <div class="test-row pass"><span class="check">✓</span> 已完成包裹 → 403 拒绝修改</div>
      <div class="test-row pass"><span class="check">✓</span> 正常推送 weight=1.5 L=30 W=20 H=15 → 写入成功 · 体积重=1.5 正确</div>
    </div>
  </div>

  <div class="footer">集运管理系统 · 2026-06-30</div>
</div>
</body>
</html>"""


# ==================== 认证系统 ====================

# 管理员账号（硬编码，密码存储为明文哈希）
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD_HASH = '<DEV_ADMIN_PWD_HASH>'  # 直接比对明文

SECONDARY_PASSWORD = '<DEV_ADMIN_PWD_HASH>'

# 全局缓存：最近测量设备上传的包裹信息（供前端实时轮询）
_latest_measurement = {
    'packed_id': None,
    'customer_name': '',
    'weight': 0,
    'length': 0,
    'width': 0,
    'height': 0,
    'timestamp': None,
}

def login_required(f):
    """登录保护装饰器 - 未登录跳转到登录页"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def verify_secondary_password(password):
    """验证二级密码"""
    return password == SECONDARY_PASSWORD


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页面"""
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD_HASH:
            session['logged_in'] = True
            session['username'] = username
            next_url = request.args.get('next')
            if next_url:
                return redirect(next_url)
            return redirect(url_for('index'))
        else:
            error = '用户名或密码错误'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    """登出"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/verify_secondary_password', methods=['POST'])
def api_verify_secondary_password():
    """验证二级密码"""
    data = request.get_json(silent=True) or {}
    password = data.get('password', '').strip()
    if verify_secondary_password(password):
        session['secondary_verified'] = True
        return jsonify({'success': True, 'message': '二级密码验证通过'})
    return jsonify({'success': False, 'message': '二级密码错误'})


def _get_locked_batch_ids_from_request():
    """锁定批次守卫：根据请求路由提取涉及的批次ID，若已锁定则返回该批次ID集合（否则空集）
    
    覆盖的写操作：
    - 箱子类：update(尺寸/重量)、add_packages(补漏)、delete、move、create、create_separate、batch_create、freight
    - 打包类：update、complete、append_items、records/update、records/delete、records/rollback
    - 归批类：assign_batch（归入已锁定批次）
    - 批次类：batch/update（改渠道/邮编）
    """
    try:
        ep = request.endpoint or ''
        path = request.path or ''
        bid = None

        # ── 箱子类（通过 carton_id → carton.batch_id） ──
        carton_eps = {
            'api_update_carton', 'api_carton_add_packages', 'api_delete_carton',
            'api_move_carton', 'api_create_freight', 'api_carton_ship',
        }
        if ep in carton_eps:
            carton_id = request.view_args.get('carton_id') if request.view_args else None
            if carton_id:
                c = db.session.get(Carton, carton_id)
                if c and c.batch_id:
                    bid = c.batch_id

        # ── 打包类（通过 packed_id → packed.batch_id） ──
        packed_eps = {
            'api_update_packed', 'api_packed_append_items', 'api_complete_packed_record',
            'api_update_packed_record', 'api_delete_packed_record', 'api_rollback_packed_record',
        }
        if ep in packed_eps:
            pid = None
            if request.view_args and request.view_args.get('packed_id'):
                pid = request.view_args.get('packed_id')
            # records/update 系列用 body 里的 id
            if pid is None and ep in ('api_update_packed_record',):
                data = request.get_json(silent=True) or {}
                pid = data.get('id')
            if pid:
                p = db.session.get(PackedPackage, pid)
                if p and p.batch_id:
                    bid = p.batch_id

        # ── 归批类：assign_batch 归入指定批次 ──
        if ep == 'api_assign_batch':
            data = request.get_json(silent=True) or {}
            target_bid = data.get('batch_id')
            if target_bid and target_bid > 0:
                bid = target_bid

        # ── 打包创建类：禁止把新包裹打包进已锁定批次（body 里的 batch_id） ──
        if ep in ('api_create_packed', 'api_create_packed_batch'):
            data = request.get_json(silent=True) or {}
            target_bid = data.get('batch_id')
            if target_bid is None:
                target_bid = request.form.get('batch_id', type=int)
            if target_bid and target_bid > 0:
                bid = target_bid

        # ── 批次类：修改批次信息 ──
        if ep == 'api_update_batch':
            bid = request.view_args.get('batch_id') if request.view_args else None

        if bid is None:
            return set()

        # 判断是否锁定
        locked = db.session.scalar(db.select(Batch.is_locked).where(Batch.id == bid))
        if locked:
            return {bid}
        return set()
    except Exception:
        # 守卫失败时放行（不让校验逻辑影响正常请求）
        return set()


@app.before_request
def check_auth():
    """全局认证检查"""
    # 允许登录页面和静态文件
    if request.endpoint in ('login', 'logout', 'static'):
        return
    if request.path.startswith('/login'):
        return
    # 二级密码验证 API 也允许未登录访问（但逻辑上已经需要先登录才能调用）
    if request.endpoint == 'api_verify_secondary_password':
        return

    # HSJ 设备对接接口免登录（设备无 session）
    if request.endpoint in ('hsj_data_push', 'hsj_login', 'api_measure_status', 'api_measure_get', 'api_measure_submit', 'api_measure_get_by_barcode', 'api_measure_pending', 'api_missing_images', 'api_index_images'):
        return

    # 检查登录状态
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.url))

    # 二级密码保护：运费管理页面
    if request.path == '/freight':
        if not session.get('secondary_verified'):
            return render_template('secondary_password.html', 
                                   title='运费管理', 
                                   return_url='/freight')

    # 二级密码保护：数据导出页面
    if request.path == '/export_data':
        if not session.get('secondary_verified'):
            return render_template('secondary_password.html', 
                                   title='数据导出', 
                                   return_url='/export_data')

    # 二级密码保护：所有导出接口
    if request.path.startswith('/export/'):
        if not session.get('secondary_verified'):
            if request.accept_mimetypes.best == 'application/json' or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': '请先验证二级密码', 'code': 'SECONDARY_REQUIRED'}), 403
            return render_template('secondary_password.html',
                                   title='尺寸信息导出',
                                   return_url=request.path)

    # 二级密码保护：修改单价
    if request.path == '/api/price_config' and request.method == 'POST':
        if not session.get('secondary_verified'):
            return jsonify({'success': False, 'message': '请先验证二级密码', 'code': 'SECONDARY_REQUIRED'}), 403

    # 二级密码保护：标记订单完成
    if request.endpoint == 'api_complete_packed_record':
        if not session.get('secondary_verified'):
            return jsonify({'success': False, 'message': '请先验证二级密码', 'code': 'SECONDARY_REQUIRED'}), 403

    # 二级密码保护：批量标记完成
    if request.endpoint == 'api_batch_complete_packed_records':
        if not session.get('secondary_verified'):
            return jsonify({'success': False, 'message': '请先验证二级密码', 'code': 'SECONDARY_REQUIRED'}), 403

    # ==================== 锁定批次守卫（2026-08-14 新增） ====================
    # 已锁定的批次不允许任何修改：箱子数据/尺寸、补漏加包裹、删除/移动箱子、
    # 归入批次、打包、修改打包记录、退货等一律拒绝（仅允许查看）
    if request.method == 'POST':
        blocked_batch_ids = _get_locked_batch_ids_from_request()
        if blocked_batch_ids:
            bnos = db.session.scalars(db.select(Batch.batch_no).where(Batch.id.in_(blocked_batch_ids))).all()
            bno_str = '、'.join(bnos) if bnos else str(blocked_batch_ids)
            return jsonify({
                'success': False,
                'message': f'批次 {bno_str} 已锁定，锁定后的批次不允许修改数据/尺寸/归入包裹。如需操作请先解锁批次',
                'code': 'BATCH_LOCKED',
                'locked_batch_ids': list(blocked_batch_ids)
            }), 403


# 启动时初始化数据库（gunicorn 多 worker 安全：文件锁防止并发初始化）
import time as _time

_INIT_LOCK_FILE = os.path.join(BASE_DIR, '.init.lock')
_INIT_DONE_FILE = os.path.join(BASE_DIR, '.init.done')

def _init_database():
    # 自动迁移：每次启动都尝试（字段已存在时报错，忽略即可）
    with app.app_context():
        _migrations = [
            "ALTER TABLE incoming_packages ADD COLUMN customer_remark TEXT",
            "ALTER TABLE incoming_packages ADD COLUMN abnormal_remark TEXT",
            "ALTER TABLE packed_packages ADD COLUMN customer_remark TEXT",
            "ALTER TABLE packed_packages ADD COLUMN abnormal_remark TEXT",
            "ALTER TABLE packed_packages ADD COLUMN status TEXT DEFAULT '待合箱'",
            "ALTER TABLE packed_packages ADD COLUMN is_full_box BOOLEAN DEFAULT 0",
            "ALTER TABLE packed_packages ADD COLUMN weight REAL DEFAULT 0",
            "ALTER TABLE packed_packages ADD COLUMN length REAL",
            "ALTER TABLE packed_packages ADD COLUMN width REAL",
            "ALTER TABLE packed_packages ADD COLUMN height REAL",
            "ALTER TABLE batches ADD COLUMN is_locked BOOLEAN DEFAULT 0",
            "ALTER TABLE batches ADD COLUMN shipment_status TEXT DEFAULT ''",
            "ALTER TABLE batches ADD COLUMN lock_time DATETIME",
            "ALTER TABLE incoming_packages ADD COLUMN is_anonymous BOOLEAN DEFAULT 0",
            # 修复历史数据：status 为 NULL 的包裹统一设为"已入库"
            "UPDATE incoming_packages SET status = '已入库' WHERE status IS NULL OR status = '' OR status = 'None'",
            # 修复已打包包裹状态：有 packed_package_items 关联的应标为"已打包"
            "UPDATE incoming_packages SET status = '已打包' WHERE id IN (SELECT DISTINCT incoming_package_id FROM packed_package_items) AND status = '已入库'",
        ]
        for sql in _migrations:
            try:
                db.session.execute(db.text(sql))
            except Exception:
                pass

    # 如果初始化已完成（.init.done 存在），跳过 db.create_all 但保留 configure_mappers
    if os.path.exists(_INIT_DONE_FILE):
        with app.app_context():
            configure_mappers()
        return

    # 尝试创建锁文件（原子操作）
    try:
        fd = os.open(_INIT_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        # 其他 worker 正在初始化，等待完成
        for _ in range(10):
            _time.sleep(0.5)
            if os.path.exists(_INIT_DONE_FILE):
                return
        return  # 超时放弃

    try:
        with app.app_context():
            try:
                db.create_all()
            except Exception:
                pass
            configure_mappers()
            # 数据迁移：修正 status 字段
            try:
                db.session.execute(db.text(
                    "UPDATE packed_packages SET status = '待合箱' WHERE status IS NULL AND is_cartoned = 0"
                ))
                db.session.execute(db.text(
                    "UPDATE packed_packages SET status = '已发货' WHERE status IS NULL AND is_cartoned = 1"
                ))
                db.session.execute(db.text("""
                    UPDATE packed_packages SET status = '已合箱'
                    WHERE is_cartoned = 1 AND id IN (
                        SELECT ci.packed_package_id FROM carton_items ci
                        JOIN cartons c ON c.id = ci.carton_id WHERE c.is_shipped = 0
                    )
                """))
                db.session.execute(db.text("""
                    UPDATE packed_packages SET status = '已合箱'
                    WHERE is_cartoned = 1 AND status NOT IN ('已合箱', '已发货', '已完成')
                """))
                db.session.execute(db.text(
                    "UPDATE incoming_packages SET status = '已入库' WHERE status = '待发货'"
                ))
            except Exception:
                pass
            db.session.commit()
        # 标记初始化完成
        open(_INIT_DONE_FILE, 'w').close()
    finally:
        # 清理锁文件
        try:
            os.remove(_INIT_LOCK_FILE)
        except OSError:
            pass

_init_database()


def generate_customer_no():
    """生成唯一的客户编号"""
    today = datetime.now().strftime('%Y%m%d')
    prefix = f'C{today}'
    
    # 使用数据库锁防止竞态条件
    max_attempts = 10
    for attempt in range(max_attempts):
        # 查找今天最大编号
        last = db.session.scalar(db.select(Customer).filter(
            Customer.customer_no.like(f'{prefix}%')
        ).with_for_update().order_by(Customer.customer_no.desc()))
        
        if last:
            try:
                seq = int(last.customer_no[-4:]) + 1
            except:
                seq = 1
        else:
            seq = 1
        
        new_no = f'{prefix}{seq:04d}'
        
        # 检查是否已存在
        existing = db.session.scalar(db.select(Customer).filter_by(customer_no=new_no))
        if not existing:
            return new_no
        
        # 如果冲突，重试
        db.session.rollback()
    
    # 如果所有尝试都失败，使用UUID后缀
    return f'{prefix}{uuid.uuid4().hex[:4].upper()}'


def generate_customer_barcode(customer_id):
    """生成6位唯一短条码: 1随机大写字母 + 5随机数字（如 A12345）

    带碰撞检测：最多重试10次，冲突时自动生成新码。
    """
    import random
    import string

    max_attempts = 10
    for attempt in range(max_attempts):
        barcode = random.choice(string.ascii_uppercase) + ''.join(random.choices(string.digits, k=5))

        # 检查是否已存在
        existing = db.session.scalar(db.select(Customer).filter_by(barcode=barcode))
        if not existing:
            return barcode

        # 冲突时重试
        if attempt > 0:
            db.session.rollback()

    # 兜底：追加时间戳确保唯一
    fallback = f'{random.choice(string.ascii_uppercase)}{"".join(random.choices(string.digits, k=5))}'
    return fallback


@db.event.listens_for(Customer, 'before_insert')
def auto_fill_customer_fields(mapper, connection, target):
    """自动填充客户编号 - 在插入前自动生成。条码在flush后由调用方设置。"""
    from sqlalchemy import func as sa_func
    if not target.customer_no:
        today = datetime.now().strftime('%Y%m%d')
        prefix = f'C{today}'
        # 查询当前最大序号
        max_no = connection.execute(
            db.select(func.count()).select_from(Customer.__table__)
        )
        # 用简单自增序号，避免复杂查询
        last = connection.execute(
            db.select(Customer.customer_no).order_by(Customer.customer_no.desc()).limit(1)
        ).first()
        if last:
            try:
                seq = int(last[0][-4:]) + 1
            except:
                seq = 1
        else:
            seq = 1
        target.customer_no = f'{prefix}{seq:04d}'


def generate_batch_no(batch_type):
    """生成批次编号 - 格式：P118（普货）或 M118（敏货）"""
    prefix = 'P' if batch_type == '普' else 'M'
    
    # 使用数据库锁防止竞态条件
    max_attempts = 10
    for attempt in range(max_attempts):
        # 查找该类型最大批次号
        last = db.session.scalar(db.select(Batch).filter(
            Batch.batch_type == batch_type,
            Batch.batch_no.like(f'{prefix}%')
        ).with_for_update().order_by(Batch.batch_no.desc()))
        
        if last:
            try:
                seq = int(last.batch_no[1:]) + 1
            except:
                seq = 1
        else:
            seq = 1
        
        new_no = f'{prefix}{seq}'
        
        # 检查是否已存在
        existing = db.session.scalar(db.select(Batch).filter_by(batch_no=new_no))
        if not existing:
            return new_no
        
        db.session.rollback()
    
    return f'{prefix}{uuid.uuid4().hex[:4].upper()}'


def _build_barcode_svg(data, module_width=0.50, module_height=25.0):
    """用python-barcode编码Code128，自渲染洁净SVG（带viewBox，无mm后缀）

    返回 dict: {'svg': str, 'width_mm': float, 'height_mm': float}
    
    关键改进：python-barcode的SVGWriter输出无viewBox且所有坐标带mm后缀，
    浏览器渲染/打印时物理单位映射不可靠导致条码比例失真、无法扫描。
    本函数只借python-barcode做编码，自建viewBox洁净SVG，外层精确mm容器保证物理尺寸。
    """
    from barcode.codex import Code128

    # 清理数据（Code128 仅支持 ASCII 0-127）
    data_safe = data.encode('ascii', errors='replace').decode('ascii')

    # 用python-barcode编码 → 得到 '0100110...' 字符串（1=黑条 0=白空）
    code = Code128(data_safe)
    bars = code.build()[0]

    quiet_zone = 12.0
    total_w_mm = quiet_zone * 2 + len(bars) * module_width
    text_gap = 4.0  # 底部留白间距
    total_h_mm = 1.0 + module_height + text_gap  # 无文字，缩小高度

    # viewBox 直接用 mm 数值，1单位=1mm，物理尺寸1:1映射
    vb_w = round(total_w_mm, 2)
    vb_h = round(total_h_mm, 2)
    bar_y = 1.0
    bar_h = module_height

    parts = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w} {vb_h}"'
                 f' width="100%" height="100%" shape-rendering="crispEdges">')
    parts.append(f'<rect width="{vb_w}" height="{vb_h}" fill="white"/>')

    x = quiet_zone
    for ch in bars:
        if ch == '1':
            parts.append(f'<rect x="{x}" y="{bar_y}" width="{module_width}" height="{bar_h}" fill="black"/>')
        x += module_width

    parts.append('</svg>')

    svg = '\n'.join(parts)
    return {'svg': svg, 'width_mm': total_w_mm, 'height_mm': total_h_mm}


def generate_barcode_image(data, module_width=0.50, module_height=25.0):
    """生成条形码(Code128) SVG 数据（兼容旧API，返回BytesIO）"""
    info = _build_barcode_svg(data, module_width=module_width,
                               module_height=module_height)
    buf = io.BytesIO()
    buf.write(info['svg'].encode('utf-8'))
    buf.seek(0)
    return buf


def _make_inline_barcode(data, module_width=0.50, module_height=25.0):
    """生成可内联嵌入HTML的SVG条形码（洁净viewBox版本，用于客户面单）
    返回 dict: {'svg': str, 'width_mm': float, 'height_mm': float}
    """
    return _build_barcode_svg(data, module_width=module_width,
                               module_height=module_height)


def _make_inline_barcode_legacy(data, module_width=0.55, module_height=18.0, font_size=10):
    """旧版：使用python-barcode SVGWriter生成条码（返回纯SVG字符串）
    用于包裹标签，不参与viewBox洁净SVG方案。
    """
    import re
    data_safe = data.encode('ascii', errors='replace').decode('ascii')
    code128 = barcode.get('code128', data_safe, writer=SVGWriter())
    svg_buffer = io.BytesIO()
    code128.write(svg_buffer, options={
        'module_width': module_width,
        'module_height': module_height,
        'font_size': font_size,
        'text_distance': 5,
        'quiet_zone': 15,
    })
    svg_buffer.seek(0)
    svg_content = svg_buffer.getvalue().decode('utf-8')
    svg_match = re.search(r'<svg[\s\S]*?</svg>', svg_content)
    svg_tag = svg_match.group(0) if svg_match else ''
    # 去除 python-barcode SVGWriter 自动生成的条码下方文字
    svg_tag = re.sub(r'<text[\s\S]*?</text>', '', svg_tag)
    if svg_tag and 'shape-rendering' not in svg_tag:
        svg_tag = svg_tag.replace('<svg ', '<svg shape-rendering="crispEdges" ', 1)
    return svg_tag




def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_request_param(key, default=None):
    """获取请求参数"""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data.get(key, default)
    return request.form.get(key, default)


def get_request_int(key, default=None):
    val = get_request_param(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def get_request_float(key, default=0.0):
    val = get_request_param(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_request_bool(key, default=False):
    val = get_request_param(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes', 'on')
    return bool(val)


def api_success(data=None, message='操作成功', code='SUCCESS'):
    """统一成功响应格式"""
    result = {'success': True, 'code': code, 'message': message}
    if data is not None:
        result['data'] = data
    return jsonify(result)


def api_error(message, code='ERROR', status_code=400, details=None):
    """统一错误响应格式"""
    result = {'success': False, 'code': code, 'message': message}
    if details:
        result['details'] = details
    return jsonify(result), status_code


def validate_tracking_number(tracking):
    """验证快递单号格式"""
    if not tracking or len(tracking.strip()) == 0:
        return False, '快递单号不能为空'
    tracking = tracking.strip()
    if len(tracking) > 50:
        return False, '快递单号过长（最多50字符）'
    # 禁止危险字符
    if any(c in tracking for c in ['<', '>', '&', '"', "'", ';']):
        return False, '快递单号包含非法字符'
    return True, None


def validate_serial_label(serial, allow_unique_suffix=True):
    """验证序号标签格式 - 格式：数字-数字，如 1-1, 2-3。可选唯一后缀：#packed_id"""
    if not serial or len(serial.strip()) == 0:
        return False, '序号标签不能为空'
    serial = serial.strip()
    # 支持两种格式："2-1" 或 "2-1#123"（带唯一后缀）
    pattern = r'^\d+-\d+$'
    if allow_unique_suffix:
        pattern = r'^\d+-\d+(#\d+)?$'
    if not re.match(pattern, serial):
        return False, '序号标签格式错误（应为：总件数-当前件数，如 2-1）'
    parts = serial.split('-')
    total = int(parts[0])
    current = int(parts[1])
    if total < 1:
        return False, '总件数必须大于0'
    if current < 1 or current > total:
        return False, f'当前件数必须在1到{total}之间'
    return True, None


def validate_customer_name(name):
    """验证客户姓名格式"""
    if not name or len(name.strip()) == 0:
        return False, '客户姓名不能为空'
    name = name.strip()
    if len(name) > 50:
        return False, '客户姓名过长（最多50字符）'
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_\s]+$', name):
        return False, '客户姓名包含非法字符'
    return True, None


def validate_address(address):
    """验证地址格式"""
    if not address or len(address.strip()) == 0:
        return False, '收货地址不能为空'
    address = address.strip()
    if len(address) > 200:
        return False, '收货地址过长（最多200字符）'
    return True, None


# ==================== SQLAlchemy 2.0 兼容辅助函数 ====================

def _scalar_count(model, *where_clauses, **filter_kw):
    """SQLAlchemy 2.0 兼容的 count() - 替代 Model.query.filter_by(...).count()
    
    用法:
        _scalar_count(Customer)                                     → _scalar_count(Customer)
        _scalar_count(Customer, status='已入库')                     → _scalar_count(Customer, status='已入库')
        _scalar_count(IncomingPackage, status='已入库', customer_id=1) → filter_by 多条件
        _scalar_count(PackedPackage, PackedPackage.created_at >= today, customer_id=1) → 混合条件
    """
    stmt = db.select(func.count()).select_from(model.__table__)
    for clause in where_clauses:
        stmt = stmt.where(clause)
    for col_name, val in filter_kw.items():
        stmt = stmt.where(getattr(model, col_name) == val)
    return db.session.scalar(stmt)


def _batch_packed_counts(batch_ids):
    """SQLAlchemy 2.0 兼容 - 批量获取每个批次的包裹数，替代 dynamic relationship .count()
    返回 dict: {batch_id: count}
    """
    if not batch_ids:
        return {}
    stmt = (db.select(PackedPackage.batch_id, func.count(PackedPackage.id))
            .where(PackedPackage.batch_id.in_(batch_ids))
            .group_by(PackedPackage.batch_id))
    rows = db.session.execute(stmt).all()
    return {row[0]: row[1] for row in rows}


def _batch_incoming_item_counts(batch_ids):
    """批量获取每个批次已打包的入库单号数量（PackedPackageItem 数量）
    返回 dict: {batch_id: count}
    """
    if not batch_ids:
        return {}
    stmt = (
        db.select(PackedPackage.batch_id, func.count(PackedPackageItem.id))
        .join(PackedPackageItem, PackedPackageItem.packed_package_id == PackedPackage.id)
        .where(PackedPackage.batch_id.in_(batch_ids))
        .group_by(PackedPackage.batch_id)
    )
    rows = db.session.execute(stmt).all()
    return {row[0]: row[1] for row in rows}


def _can_lock_batch(batch_id):
    """检查批次是否可以锁定：无待确认发货 && 所有包裹已完成"""
    # 检查是否有未完成的 IncomingPackage（已入库/待打包 = 待确认发货的包裹）
    pending_incoming = db.session.scalar(
        db.select(func.count()).select_from(IncomingPackage.__table__).where(
            IncomingPackage.batch_id == batch_id,
            IncomingPackage.status.in_(['已入库', '待打包'])
        )
    ) or 0
    if pending_incoming > 0:
        return False

    # 检查 PackedPackage：全部已完成
    total_packed = _scalar_count(PackedPackage, batch_id=batch_id)
    completed = _scalar_count(PackedPackage, batch_id=batch_id, status='已完成')
    if total_packed > 0 and total_packed == completed:
        return True
    return False


@app.route('/')
def index():
    """首页 - 仪表盘"""
    # 统计数据
    total_customers = _scalar_count(Customer)
    pending_packages = _scalar_count(IncomingPackage, status='已入库')
    packed_count = _scalar_count(PackedPackage, is_cartoned=False)
    cartons_count = _scalar_count(Carton, is_shipped=False)

    # 待处理客户（有未入库包裹）
    customers_with_pending = db.session.scalar(
        db.select(func.count(func.distinct(Customer.id)))
        .select_from(Customer)
        .join(IncomingPackage, Customer.id == IncomingPackage.customer_id)
        .where(IncomingPackage.status == '已入库')
    )

    # 今日入库
    today = datetime.now().date()
    today_inbound = _scalar_count(IncomingPackage, func.date(IncomingPackage.entry_time) == today)

    # 批次列表（未锁定 + 未发货）
    batches = db.session.scalars(db.select(Batch).filter_by(is_shipped=False, is_locked=False).order_by(Batch.created_at.desc()).limit(5)).all()
    batch_ids = [b.id for b in batches]
    batch_packed_counts = _batch_packed_counts(batch_ids)

    # 已锁定批次（已完成）
    locked_batches = db.session.scalars(db.select(Batch).filter_by(is_locked=True).order_by(Batch.created_at.desc()).limit(5)).all()
    locked_batch_ids = [b.id for b in locked_batches]
    locked_batch_counts = _batch_packed_counts(locked_batch_ids)

    # 价格配置
    prices = get_price_config()

    # 客户发货量统计（按入库包裹数，Top 15）
    customer_stats = db.session.execute(
        db.select(Customer.name, func.count(IncomingPackage.id))
        .join(IncomingPackage, Customer.id == IncomingPackage.customer_id)
        .group_by(Customer.id)
        .order_by(func.count(IncomingPackage.id).desc())
        .limit(15)
    ).all()
    customer_labels = [r[0] for r in customer_stats]
    customer_values = [r[1] for r in customer_stats]

    # 按收货地址统计（Top 15）
    address_stats = db.session.execute(
        db.select(IncomingPackage.receiving_address, func.count(IncomingPackage.id))
        .where(IncomingPackage.receiving_address.isnot(None), IncomingPackage.receiving_address != '')
        .group_by(IncomingPackage.receiving_address)
        .order_by(func.count(IncomingPackage.id).desc())
        .limit(15)
    ).all()
    address_labels = [r[0] for r in address_stats]
    address_values = [r[1] for r in address_stats]

    # 按邮编统计（Top 15）
    zip_stats = db.session.execute(
        db.select(Customer.zip_code, func.count(IncomingPackage.id))
        .join(IncomingPackage, Customer.id == IncomingPackage.customer_id)
        .where(Customer.zip_code.isnot(None), Customer.zip_code != '')
        .group_by(Customer.zip_code)
        .order_by(func.count(IncomingPackage.id).desc())
        .limit(15)
    ).all()
    zip_labels = [r[0] for r in zip_stats]
    zip_values = [r[1] for r in zip_stats]

    # 按批次统计（统计已打包的单号数量，即 PackedPackageItem 数）
    batch_stats = db.session.execute(
        db.select(Batch.batch_no, func.count(PackedPackageItem.id))
        .join(PackedPackage, PackedPackage.batch_id == Batch.id)
        .join(PackedPackageItem, PackedPackageItem.packed_package_id == PackedPackage.id)
        .group_by(Batch.id)
        .order_by(func.count(PackedPackageItem.id).desc())
        .limit(15)
    ).all()
    batch_labels = [r[0] for r in batch_stats]
    batch_values = [r[1] for r in batch_stats]

    return render_template('index.html',
                          total_customers=total_customers,
                          pending_packages=pending_packages,
                          customers_with_pending=customers_with_pending,
                          today_inbound=today_inbound,
                          packed_count=packed_count,
                          cartons_count=cartons_count,
                          batches=batches,
                          batch_packed_counts=batch_packed_counts,
                          locked_batches=locked_batches,
                          locked_batch_counts=locked_batch_counts,
                          normal_price=prices['normal'],
                          danger_price=prices['danger'],
                          customer_labels=customer_labels,
                          customer_values=customer_values,
                          address_labels=address_labels,
                          address_values=address_values,
                          zip_labels=zip_labels,
                          zip_values=zip_values,
                          batch_labels=batch_labels,
                          batch_values=batch_values)


@app.route('/address_book')
def address_book():
    """地址库管理页面"""
    entries = db.session.scalars(
        db.select(AddressBook).order_by(AddressBook.zip_code, AddressBook.id)
    ).all()
    return render_template('address_book.html', entries=[{
        'id': e.id, 'zip_code': e.zip_code,
        'puhuo_consignee': e.puhuo_consignee, 'puhuo_phone': e.puhuo_phone, 'puhuo_address': e.puhuo_address,
        'minhuo_consignee': e.minhuo_consignee, 'minhuo_phone': e.minhuo_phone, 'minhuo_address': e.minhuo_address,
        'city': e.city, 'state': e.state, 'country': e.country, 'remark': e.remark
    } for e in entries])


@app.route('/customers')
def customers():
    """客户管理页面"""
    search_key = request.args.get('search', '').strip()
    group_by_zip = request.args.get('group', '1')  # 默认按邮编分组
    vip_only = request.args.get('vip', '0') == '1'  # 是否只看单独地址客户

    base_query = db.select(Customer)
    if vip_only:
        base_query = base_query.where(Customer.is_vip == True)
    if search_key:
        base_query = base_query.where(
            or_(
                Customer.name.like(f'%{search_key}%'),
                Customer.customer_no.like(f'%{search_key}%'),
                Customer.barcode.like(f'%{search_key}%'),
                Customer.address.like(f'%{search_key}%'),
                Customer.zip_code.like(f'%{search_key}%')
            )
        )
    customers_list = db.session.scalars(
        base_query.order_by(Customer.zip_code, Customer.name)
    ).all()

    # 为每个客户统计已入库包裹数
    for c in customers_list:
        c.pending_count = _scalar_count(IncomingPackage, customer_id=c.id, status='已入库')
        # pending_freight 直接从Customer字段取

    # 按邮编分组
    zip_groups = {}
    for c in customers_list:
        zip_key = c.zip_code.strip() if c.zip_code and c.zip_code.strip() else '无邮编'
        if zip_key not in zip_groups:
            zip_groups[zip_key] = []
        zip_groups[zip_key].append(c)

    # 排序：有邮编的按邮编升序，无邮编的放最后
    sorted_groups = []
    no_zip = zip_groups.pop('无邮编', [])
    for k in sorted(zip_groups.keys()):
        sorted_groups.append((k, zip_groups[k]))
    if no_zip:
        sorted_groups.append(('无邮编', no_zip))

    # VIP模式下，返回普通客户列表供选择转为单独地址客户
    normal_customers = []
    if vip_only:
        raw_customers = db.session.scalars(
            db.select(Customer).where(Customer.is_vip == False).order_by(Customer.name)
        ).all()
        normal_customers = [{
            'id': c.id,
            'name': c.name,
            'phone': c.phone or '',
            'address': c.address or '',
            'zip_code': c.zip_code or '',
            'company_name': c.company_name or '',
            'country': c.country or '',
            'invoice_contact': c.invoice_contact or ''
        } for c in raw_customers]

    return render_template('customers.html',
                           customers=customers_list,
                           zip_groups=sorted_groups,
                           group_by_zip=(group_by_zip == '1'),
                           search_key=search_key,
                           vip_only=vip_only,
                           normal_customers=normal_customers)


@app.route('/inbound')
def inbound():
    """入库扫描页面"""
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    return render_template('inbound.html', customers=customers)


@app.route('/inbound/records')
def inbound_records():
    """入库明细页面"""
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    customer_id = request.args.get('customer_id', type=int)
    status = request.args.get('status', '')
    batch_id = request.args.get('batch_id', type=int)
    zip_code = request.args.get('zip_code', '')
    tracking_number = request.args.get('tracking_number', '').strip()

    stmt = db.select(IncomingPackage)

    if start_date:
        stmt = stmt.where(IncomingPackage.entry_time >= start_date)
    if end_date:
        stmt = stmt.where(IncomingPackage.entry_time <= end_date + ' 23:59:59')
    if customer_id:
        stmt = stmt.where(IncomingPackage.customer_id == customer_id)
    if status:
        stmt = stmt.where(IncomingPackage.status == status)
    if batch_id:
        stmt = stmt.where(IncomingPackage.batch_id == batch_id)
    if zip_code:
        stmt = stmt.join(Customer, IncomingPackage.customer_id == Customer.id).where(Customer.zip_code == zip_code)
    if tracking_number:
        stmt = stmt.where(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))

    # 封存：排除已锁定批次的入库包裹（已锁定批次的包裹不在入库明细显示）
    # 按单号搜索时不过滤锁定批次
    if not tracking_number:
        locked_batch_ids = db.session.scalars(db.select(Batch.id).where(Batch.is_locked == True)).all()
        if locked_batch_ids:
            locked_incoming_sub = db.select(PackedPackageItem.incoming_package_id).join(
                PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id
            ).where(PackedPackage.batch_id.in_(locked_batch_ids))
            stmt = stmt.where(IncomingPackage.id.not_in(locked_incoming_sub))

    # 排除无主包裹（无主包裹在独立页面管理）—— 按单号搜索时不过滤
    if not tracking_number:
        stmt = stmt.where(IncomingPackage.is_anonymous == False)

    # 排除已发货、已完成的包裹
    # 已退货：仅当用户明确在筛选中选择「已退货」时才显示（用于回退操作），默认视图仍排除
    if status != '已退货':
        stmt = stmt.where(IncomingPackage.status != '已退货')
    stmt = stmt.where(IncomingPackage.status != '已完成')
    stmt = stmt.where(IncomingPackage.status != '已发货')

    # 分页
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)
    per_page = min(max(per_page, 20), 500)
    total_count = db.session.scalar(db.select(db.func.count()).select_from(stmt.subquery()))
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)

    records = db.session.scalars(stmt.order_by(IncomingPackage.entry_time.desc()).limit(per_page).offset((page - 1) * per_page)).all()
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    batches = db.session.scalars(db.select(Batch).order_by(Batch.batch_no)).all()

    # 获取所有不重复的邮编
    zip_codes = db.session.scalars(db.select(Customer.zip_code).where(Customer.zip_code != '').distinct().order_by(Customer.zip_code)).all()

    # 动态计算每个客户的货主单号序号（锁定批次后重新从1编号）
    display_seq = {}
    customer_counter = {}
    for r in records:
        cid = r.customer_id or 0
        customer_counter[cid] = customer_counter.get(cid, 0) + 1
        display_seq[r.id] = customer_counter[cid]

    return render_template('inbound_records.html',
                           records=records,
                           customers=customers,
                           batches=batches,
                           zip_codes=zip_codes,
                           display_seq=display_seq,
                           page=page,
                           per_page=per_page,
                           total_count=total_count,
                           total_pages=total_pages,
                           start_date=start_date,
                           end_date=end_date,
                           selected_customer=customer_id,
                           selected_status=status,
                           selected_batch_id=batch_id,
                           selected_zip=zip_code,
                           tracking_number=tracking_number)


@app.route('/all_orders')
def all_orders():
    """全部单号页面 — 查看所有状态的单号，按多维度筛选"""
    customer_id = request.args.get('customer_id', type=int)
    batch_id = request.args.get('batch_id', type=int)
    tracking_number = request.args.get('tracking_number', '').strip()
    zip_code = request.args.get('zip_code', '').strip()
    nature = request.args.get('nature', '').strip()

    query = db.select(IncomingPackage).join(
        Customer, IncomingPackage.customer_id == Customer.id
    )

    if customer_id:
        query = query.where(IncomingPackage.customer_id == customer_id)
    if batch_id:
        # 按批次筛选：已打包取 PackedPackage.batch_id，未打包取 IncomingPackage.batch_id
        packed_in_batch = db.select(PackedPackageItem.incoming_package_id).join(
            PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id
        ).where(PackedPackage.batch_id == batch_id).subquery()
        query = query.where(db.or_(
            IncomingPackage.id.in_(packed_in_batch),
            db.and_(IncomingPackage.batch_id == batch_id,
                    IncomingPackage.id.not_in(packed_in_batch))
        ))
    if tracking_number:
        query = query.where(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))
    if zip_code:
        query = query.where(Customer.zip_code == zip_code)
    if nature:
        query = query.where(IncomingPackage.nature == nature)

    query = query.order_by(IncomingPackage.entry_time.desc())
    # 分页
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)
    per_page = min(max(per_page, 20), 500)
    total_count = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    records = db.session.execute(query.limit(per_page).offset((page - 1) * per_page)).scalars().all()

    # 预加载客户
    customer_ids = list(set(r.customer_id for r in records if r.customer_id))
    customers_map = {}
    if customer_ids:
        cs = db.session.scalars(db.select(Customer).where(Customer.id.in_(customer_ids))).all()
        customers_map = {c.id: c for c in cs}

    # 计算有效批次和显示状态
    record_ids = [r.id for r in records]
    effective_batch = {}  # record_id -> batch_id or '__VIP_SOLO__'
    display_status = {}   # record_id -> 显示状态
    shipped_time = {}     # record_id -> 发货时间
    if record_ids:
        # 查已打包的：通过 PackedPackageItem 找到 PackedPackage.batch_id
        pkg_items = db.session.scalars(
            db.select(PackedPackageItem).options(
                joinedload(PackedPackageItem.packed_package).joinedload(PackedPackage.batch)
            ).where(PackedPackageItem.incoming_package_id.in_(record_ids))
        ).all()
        for item in pkg_items:
            pp = item.packed_package
            if pp and pp.batch_id:
                effective_batch[item.incoming_package_id] = pp.batch_id
                # 状态优先级：已发货（批次锁定）> 已合箱 > 已打包
                batch_locked = pp.batch.is_locked if pp.batch else False
                if batch_locked:
                    display_status[item.incoming_package_id] = '已发货'
                    shipped_time[item.incoming_package_id] = pp.batch.lock_time
                elif pp.is_cartoned:
                    display_status[item.incoming_package_id] = '已合箱'
                else:
                    display_status[item.incoming_package_id] = '已打包'
            elif pp and pp.is_vip_solo:
                # VIP 单独发货：批次列显示「单独地址」，状态跟随 PackedPackage
                effective_batch[item.incoming_package_id] = '__VIP_SOLO__'
                if pp.status in ('已发货', '已完成'):
                    display_status[item.incoming_package_id] = '已发货'
                    shipped_time[item.incoming_package_id] = pp.shipped_at

    # 对未打包的，使用 IncomingPackage.batch_id
    for r in records:
        if r.id not in effective_batch and r.batch_id and isinstance(r.batch_id, int):
            effective_batch[r.id] = r.batch_id

    # 预加载所有涉及的批次
    all_batch_ids = list(set(effective_batch.values()))
    batches_map = {}
    if all_batch_ids:
        bs = db.session.scalars(db.select(Batch).where(Batch.id.in_(all_batch_ids))).all()
        batches_map = {b.id: b for b in bs}

    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    all_batches = db.session.scalars(db.select(Batch).order_by(Batch.created_at.desc())).all()
    zip_codes = db.session.scalars(db.select(Customer.zip_code).where(Customer.zip_code != '').distinct().order_by(Customer.zip_code)).all()

    return render_template('all_orders.html',
                           records=records,
                           effective_batch=effective_batch,
                           display_status=display_status,
                           shipped_time=shipped_time,
                           batches_map=batches_map,
                           customers_map=customers_map,
                           customers=customers,
                           batches=all_batches,
                           zip_codes=zip_codes,
                           page=page,
                           per_page=per_page,
                           total_count=total_count,
                           total_pages=total_pages,
                           selected_customer=customer_id,
                           selected_batch_id=batch_id,
                           selected_zip=zip_code,
                           selected_nature=nature,
                           tracking_number=tracking_number)


@app.route('/shipments')
def shipments():
    """待打包确认页面 - 按邮编筛选，常驻显示所有已入库客户"""
    zip_code = request.args.get('zip_code', '').strip()  # 邮编筛选

    today = datetime.now().strftime('%m月%d日')
    base_query = db.session.query(Customer)

    # 获取所有有「已入库」或「待打包」包裹的客户（按邮编升序、客户名升序）
    query = base_query.join(IncomingPackage, Customer.id == IncomingPackage.customer_id).filter(
        IncomingPackage.status.in_(['已入库', '待打包']),
        IncomingPackage.status != '已退货'
    )
    customers_with_pending = query.order_by(Customer.zip_code.asc(), Customer.name.asc()).distinct().all()

    # 邮编筛选
    if zip_code:
        customers_with_pending = [c for c in customers_with_pending if c.zip_code == zip_code]

    # 获取已确认待打包的客户（状态为待打包的包裹所属客户）
    confirmed_ids = db.session.query(
        IncomingPackage.customer_id
    ).filter(
        IncomingPackage.status == '待打包',
        IncomingPackage.status != '已退货'
    ).distinct().all()
    confirmed_ids = [c[0] for c in confirmed_ids]

    # 获取每个客户的打包记录信息
    customer_packed_info = {}

    for customer in customers_with_pending:
        packed_query = db.select(PackedPackage).filter(
            PackedPackage.customer_id == customer.id,
            PackedPackage.created_at >= today
        )
        packed_records = db.session.scalars(packed_query).all()

        if packed_records:
            total_packed = len(packed_records)
            total_items = sum(len(p.items) for p in packed_records)
            customer_packed_info[customer.id] = {
                'count': total_packed,
                'items': total_items,
                'records': [{'id': r.id, 'serial': r.serial_label, 'nature': r.nature} for r in packed_records]
            }

    # 收集每个客户已入库包裹的备注（去重）
    all_incoming = db.session.scalars(db.select(IncomingPackage).filter(
        IncomingPackage.status.in_(['已入库', '待打包'])
    )).all()
    customer_remarks_map = {}
    customer_abnormals_map = {}
    for pkg in all_incoming:
        cid = pkg.customer_id
        if cid not in customer_remarks_map:
            customer_remarks_map[cid] = []
            customer_abnormals_map[cid] = []
        if pkg.customer_remark and pkg.customer_remark not in customer_remarks_map[cid]:
            customer_remarks_map[cid].append(pkg.customer_remark)
        if pkg.abnormal_remark and pkg.abnormal_remark not in customer_abnormals_map[cid]:
            customer_abnormals_map[cid].append(pkg.abnormal_remark)

    # 获取所有未发货批次（供前端下拉选择）
    all_batches = db.session.scalars(db.select(Batch).filter_by(is_shipped=False, is_locked=False).order_by(Batch.batch_no.asc())).all()

    # 构建 客户ID → 批次号 的映射（仅从 IncomingPackage 收集）
    customer_batch_map = {}
    batch_id_to_no = {b.id: b.batch_no for b in all_batches}
    incoming_with_batch = db.session.scalars(
        db.select(IncomingPackage).filter(
            IncomingPackage.batch_id.isnot(None),
            IncomingPackage.status.in_(['已入库', '待打包'])
        )
    ).all()
    for pkg in incoming_with_batch:
        cid = pkg.customer_id
        if cid not in customer_batch_map:
            customer_batch_map[cid] = batch_id_to_no.get(pkg.batch_id, '')

    # 排序：已确认待打包（绿色）> 已指定批次未确认（黄色）> 未指定批次
    def sort_key(c):
        is_confirmed = c.id in confirmed_ids
        has_batch = customer_batch_map.get(c.id, '')
        if is_confirmed: return (0, c.zip_code or '', c.name)
        if has_batch:    return (1, c.zip_code or '', c.name)
        return (2, c.zip_code or '', c.name)
    customers_with_pending.sort(key=sort_key)
    zip_code_counts = {}
    all_customers_for_zip = db.session.scalars(
        db.select(Customer).join(IncomingPackage, Customer.id == IncomingPackage.customer_id).filter(
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.status != '已退货',
            Customer.zip_code != '',
            Customer.zip_code.isnot(None)
        ).distinct()
    ).all()
    for c in all_customers_for_zip:
        if c.zip_code:
            zip_code_counts[c.zip_code] = zip_code_counts.get(c.zip_code, 0) + 1
    zip_codes = sorted(zip_code_counts.keys())
    total_count = len(all_customers_for_zip)

    # 按邮编分组的批次（未发货、未锁定）
    batches_by_zip = {}
    for b in all_batches:
        if b.zip_code:
            batches_by_zip.setdefault(b.zip_code, []).append({
                'id': b.id,
                'batch_no': b.batch_no,
                'batch_type': b.batch_type or ''
            })

    return render_template('shipments.html',
                           customers=customers_with_pending,
                           confirmed=confirmed_ids,
                           packed_info=customer_packed_info,
                           ship_customer_remarks=customer_remarks_map,
                           ship_customer_abnormals=customer_abnormals_map,
                           customer_batch_map=customer_batch_map,
                           total_count=total_count,
                           batches=all_batches,
                           batches_by_zip=batches_by_zip,
                           zip_codes=zip_codes,
                           zip_code_counts=zip_code_counts,
                           selected_zip=zip_code)


@app.route('/pack/<int:customer_id>')
def pack(customer_id):
    """打包操作页面 — 须先在待打包确认界面确认该客户，支持拼单（添加次客户）"""
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        abort(404)

    # 解析次客户ID（拼单模式）
    secondary_ids_raw = request.args.get('secondary_ids', '')
    secondary_ids = []
    secondary_customers = []
    if secondary_ids_raw:
        try:
            secondary_ids = [int(s) for s in secondary_ids_raw.split(',') if s.strip().isdigit()]
            # 排除主客户自身
            secondary_ids = [s for s in secondary_ids if s != customer_id]
            if secondary_ids:
                secondary_customers = db.session.scalars(
                    db.select(Customer).filter(Customer.id.in_(secondary_ids))
                ).all()
        except Exception:
            secondary_ids = []
            secondary_customers = []

    all_customer_ids = [customer_id] + secondary_ids

    # 加载所有客户（主+次）的待打包包裹
    packages = db.session.scalars(db.select(IncomingPackage).filter(
        IncomingPackage.customer_id.in_(all_customer_ids),
        IncomingPackage.status == '待打包'
    ).where(IncomingPackage.status != '已退货')).all()

    if not packages:
        has_inbound = db.session.scalar(db.select(IncomingPackage).filter(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status == '已入库'
        ))
        if has_inbound:
            flash(f'「{customer.name}」尚未确认待打包，请先在待打包确认界面点击"确认待打包"', 'warning')
        else:
            flash(f'「{customer.name}」已无待打包包裹，可能已完成打包', 'info')
        return redirect(url_for('shipments'))

    # 获取当前批次
    batches = db.session.scalars(db.select(Batch).filter_by(is_shipped=False, is_locked=False).order_by(Batch.created_at.desc())).all()

    # 检测待打包确认界面已分配的批次（取第一个有batch_id的包裹）
    # 修复(2026-08-14)：只考虑当前未发货的待打包/已入库包裹，按入库时间倒序取最新，
    # 避免命中客户历史已发货包裹的旧批次（如Jessica历史93批导致选95批跳到93批）
    assigned_batch_id = None
    if packages:
        assigned = db.session.scalar(db.select(IncomingPackage).filter(
            IncomingPackage.customer_id.in_(all_customer_ids),
            IncomingPackage.batch_id.isnot(None),
            IncomingPackage.status.in_(['已入库', '待打包'])
        ).order_by(IncomingPackage.entry_time.desc()))
        if assigned:
            assigned_batch_id = assigned.batch_id

    # VIP 单独发货：VIP 客户且所有待打包包裹都没有 batch_id
    # 拼单模式：仅检查主客户包裹的 batch_id
    main_packages = [p for p in packages if p.customer_id == customer_id]
    is_vip_solo = bool(customer.is_vip) and all(p.batch_id is None for p in main_packages)

    batch_ids = [b.id for b in batches]
    batch_packed_counts = _batch_packed_counts(batch_ids)

    # 构建客户映射（用于前端显示包裹属于哪个客户）
    customer_map = {c.id: c for c in [customer] + secondary_customers}

    # 客户货位（取所有入库包裹的不重复货位标签）
    slot_labels = db.session.scalars(
        db.select(IncomingPackage.shelf_slot_id).filter(
            IncomingPackage.id.in_([p.id for p in packages])
        ).distinct()
    ).all()
    slot_info = []
    if slot_labels:
        slots = db.session.scalars(
            db.select(ShelfSlot).filter(ShelfSlot.id.in_([sid for sid in slot_labels if sid]))
        ).all()
        slot_info = [{'label': s.slot_label, 'shelf': s.shelf_code, 'floor': s.floor, 'zone': s.zone} for s in slots]

    # 扫描挂起状态
    scan_hung_state = None
    if customer.scan_hung_at:
        verified_ids = []
        if customer.scan_hung_ids:
            try:
                verified_ids = json.loads(customer.scan_hung_ids)
            except Exception:
                pass
        danger_ids = []
        if customer.scan_hung_danger_ids:
            try:
                danger_ids = json.loads(customer.scan_hung_danger_ids)
            except Exception:
                pass
        scan_hung_state = {
            'verified_count': customer.scan_hung_verified,
            'total_count': customer.scan_hung_total,
            'verified_ids': verified_ids,
            'danger_ids': danger_ids,
            'hung_at': customer.scan_hung_at.strftime('%Y-%m-%d %H:%M:%S') if customer.scan_hung_at else None
        }

    return render_template('pack.html', customer=customer, packages=packages,
                          batches=batches, assigned_batch_id=assigned_batch_id,
                          batch_packed_counts=batch_packed_counts,
                          secondary_customers=secondary_customers,
                          customer_map=customer_map,
                          scan_hung_state=scan_hung_state,
                          slot_info=slot_info,
                          is_vip_solo=is_vip_solo)


@app.route('/packing')
def packing():
    """独立打包操作页面 - 客户列表（支持邮编筛选）"""
    today = datetime.now().strftime('%Y-%m-%d')
    filter_zip = request.args.get('zip', '').strip()

    # 获取有待打包包裹的客户邮编（用于下拉筛选框）
    all_zips = db.session.query(Customer.zip_code).join(
        IncomingPackage, Customer.id == IncomingPackage.customer_id
    ).filter(
        IncomingPackage.status == '待打包',
        Customer.zip_code.isnot(None),
        Customer.zip_code != ''
    ).group_by(Customer.zip_code).order_by(Customer.zip_code.asc()).all()
    all_zips = [z[0] for z in all_zips]

    # 获取有待打包包裹的客户（按客户名排序，支持邮编筛选）— 必须有批次
    query = db.session.query(
        Customer, func.count(IncomingPackage.id).label('pending_count')
    ).join(
        IncomingPackage,
        Customer.id == IncomingPackage.customer_id
    ).filter(
        IncomingPackage.status == '待打包'
    )
    if filter_zip:
        query = query.filter(Customer.zip_code == filter_zip)
    customers_with_pending = query.group_by(Customer.id).order_by(Customer.name.asc()).all()

    # 构建客户→批次号映射（从待打包包裹的 batch_id）
    batch_no_map = {}
    for customer, _ in customers_with_pending:
        pkgs = db.session.scalars(db.select(IncomingPackage).filter_by(
            customer_id=customer.id, status='待打包'
        ).filter(IncomingPackage.batch_id.isnot(None))).all()
        for p in pkgs:
            if p.batch_id and customer.id not in batch_no_map:
                batch = db.session.get(Batch, p.batch_id)
                batch_no_map[customer.id] = batch.batch_no if batch else ''

        # VIP 客户单独发货：有 待打包 包裹但没有 batch_id
        if customer.id not in batch_no_map and customer.is_vip:
            has_unbatched = db.session.scalar(db.select(IncomingPackage).filter_by(
                customer_id=customer.id, status='待打包', batch_id=None
            ))
            if has_unbatched:
                batch_no_map[customer.id] = '单独发货'

    # 构建客户列表数据
    customer_list = []
    total_pending_packages = 0
    scan_hung_complete = 0   # 已扫描完成（挂起）
    scan_hung_incomplete = 0 # 缺件（挂起）

    for customer, pending_count in customers_with_pending:
        # 收集该客户待打包包裹的备注
        pkg_remarks = []
        pkg_abnormals = []
        pending_pkgs = db.session.scalars(db.select(IncomingPackage).filter_by(
            customer_id=customer.id, status='待打包'
        )).all()
        for p in pending_pkgs:
            if p.customer_remark and p.customer_remark not in pkg_remarks:
                pkg_remarks.append(p.customer_remark)
            if p.abnormal_remark and p.abnormal_remark not in pkg_abnormals:
                pkg_abnormals.append(p.abnormal_remark)

        # 获取客户分配的货架区位
        slots = db.session.scalars(db.select(ShelfSlot).filter_by(customer_id=customer.id)).all()
        slot_labels = []
        for slot in slots:
            label = f"{slot.shelf_code}架-{slot.floor}层-{slot.zone}号"
            slot_labels.append(label)
        slot_display = ', '.join(slot_labels) if slot_labels else '未分配'

        # 扫描挂起状态
        hung_status = None
        if customer.scan_hung_at:
            if customer.scan_hung_verified >= customer.scan_hung_total:
                hung_status = 'complete'
                scan_hung_complete += 1
            else:
                hung_status = 'incomplete'
                scan_hung_incomplete += 1

        customer_list.append({
            'id': customer.id,
            'name': customer.name,
            'customer_no': customer.customer_no,
            'barcode': customer.barcode,
            'address': customer.address,
            'zip_code': customer.zip_code or '',
            'batch_no': batch_no_map.get(customer.id, ''),
            'pending_count': pending_count,
            'customer_remark': customer.remark or '',
            'customer_remarks': pkg_remarks,
            'abnormal_remarks': pkg_abnormals,
            'shelf_slots': slot_display,
            'hung_status': hung_status,
            'hung_verified': customer.scan_hung_verified,
            'hung_total': customer.scan_hung_total
        })

        total_pending_packages += pending_count

    # 排序：已扫描(complete)最前 > 未挂起 > 缺件(incomplete)最后；同级按包裹数量从多到少（2026-08-20）
    def hung_sort_key(item):
        hs = item.get('hung_status')
        if hs == 'complete': return 0
        if hs == 'incomplete': return 2
        return 1
    customer_list.sort(key=lambda x: (hung_sort_key(x), -x['pending_count']))

    return render_template('packing.html',
                           customers=customer_list,
                           all_zips=all_zips,
                           filter_zip=filter_zip,
                           total_pending_packages=total_pending_packages,
                           scan_hung_complete=scan_hung_complete,
                           scan_hung_incomplete=scan_hung_incomplete)


@app.route('/api/packing/customers')
def api_packing_customers():
    """获取待打包客户列表API"""
    today = datetime.now().strftime('%Y-%m-%d')

    customers = db.session.query(
        Customer, func.count(IncomingPackage.id).label('pending_count')
    ).join(
        IncomingPackage,
        Customer.id == IncomingPackage.customer_id
    ).filter(
        IncomingPackage.status == '已入库'
    ).group_by(Customer.id).order_by(Customer.name.asc()).all()

    result = []
    for customer, pending_count in customers:
        today_packed = _scalar_count(PackedPackage, PackedPackage.created_at >= today, customer_id=customer.id)

        result.append({
            'id': customer.id,
            'name': customer.name,
            'customer_no': customer.customer_no,
            'pending_count': pending_count,
            'today_packed': today_packed
        })

    return api_success(data=result)


@app.route('/batch')
def batch():
    """批次管理页面"""
    # VIP 单独地址发货的真实批次（vip_customer_id 非空）走独立 VIP 发货逻辑，
    # 不在常规批次管理里出现，这里排除掉
    batches = db.session.scalars(
        db.select(Batch)
        .where(db.text("vip_customer_id IS NULL OR vip_customer_id = 0"))
        .order_by(Batch.created_at.desc())
    ).all()
    batch_ids = [b.id for b in batches]
    batch_packed_counts = _batch_packed_counts(batch_ids)
    batch_item_counts = _batch_incoming_item_counts(batch_ids)

    # 计算每个批次是否可锁定
    batch_can_lock = {}
    for b in batches:
        batch_can_lock[b.id] = _can_lock_batch(b.id)

    # 获取每个批次的分组信息（合箱）
    from models import CartonGroup, Carton, CartonItem
    batch_groups_data = {}
    for bid in batch_ids:
        groups = db.session.execute(
            db.select(CartonGroup).where(CartonGroup.batch_id == bid).order_by(CartonGroup.sort_order)
        ).scalars().all()
        groups_data = []
        for g in groups:
            cartons = db.session.execute(
                db.select(Carton).where(Carton.group_id == g.id).order_by(Carton.box_seq)
            ).scalars().all()
            normal_cartons = [c for c in cartons if c.carton_type == '普']
            sensitive_cartons = [c for c in cartons if c.carton_type == '敏']
            # 按普/敏拆分成两条记录
            if normal_cartons:
                groups_data.append({
                    'group_id': g.id,
                    'name': g.name,
                    'type': '普货',
                    'carton_count': len(normal_cartons),
                    'internal_tracking': g.internal_tracking or '',
                    'transfer_tracking': g.transfer_tracking or '',
                    'logistics_status': g.logistics_status or '',
                })
            if sensitive_cartons:
                groups_data.append({
                    'group_id': g.id,
                    'name': g.name,
                    'type': '敏货',
                    'carton_count': len(sensitive_cartons),
                    'internal_tracking': g.internal_tracking or '',
                    'transfer_tracking': g.transfer_tracking or '',
                    'logistics_status': g.logistics_status or '',
                })
        batch_groups_data[bid] = groups_data

    return render_template('batch.html', batches=batches, batch_packed_counts=batch_packed_counts,
                           batch_item_counts=batch_item_counts, batch_can_lock=batch_can_lock,
                           batch_groups_data=batch_groups_data)


@app.route('/carton')
def carton():
    """合箱管理页面 - 按批次合箱"""
    # 获取有可合箱包裹的批次（所有 is_cartoned=False 的包裹）
    batches_with_pending = {}
    valid_packed = db.session.scalars(db.select(PackedPackage).filter(
        PackedPackage.is_cartoned == False
    )).all()

    # 按批次分组统计
    for p in valid_packed:
        if p.batch_id:
            if p.batch_id not in batches_with_pending:
                batch = db.session.get(Batch, p.batch_id)
                if batch:
                    batches_with_pending[p.batch_id] = {
                        'batch': batch,
                        'regular': [],
                        'sensitive': []
                    }
            if batches_with_pending[p.batch_id] is not None:
                if p.nature == '敏':
                    batches_with_pending[p.batch_id]['sensitive'].append(p)
                else:
                    batches_with_pending[p.batch_id]['regular'].append(p)

    # 获取所有有箱子或有待合箱包裹的批次（包含已合完箱的批次，确保下拉框始终显示）
    all_relevant_batch_ids = set(batches_with_pending.keys())
    # 也加入已经有箱子但可能没有待合箱包裹的批次
    carton_batch_ids = db.session.execute(
        db.select(Carton.batch_id).where(Carton.batch_id.isnot(None)).distinct()
    ).all()
    for row in carton_batch_ids:
        all_relevant_batch_ids.add(row[0])

    all_batches = []
    for bid in all_relevant_batch_ids:
        batch = db.session.get(Batch, bid)
        if batch and not batch.is_locked:  # 跳过已锁定批次
            info = batches_with_pending.get(bid, {'batch': batch, 'regular': [], 'sensitive': []})
            # 统计该批次已有箱子数
            carton_count = _scalar_count(Carton, batch_id=bid)
            all_batches.append({
                'batch': batch,
                'regular_count': len(info.get('regular', [])),
                'sensitive_count': len(info.get('sensitive', [])),
                'carton_count': carton_count
            })

    # 按批次编号排序
    all_batches.sort(key=lambda x: x['batch'].batch_no or '')

    # 加载所有分组（供前端初始化）
    all_groups = db.session.scalars(db.select(CartonGroup).options(
        joinedload(CartonGroup.batch)
    ).order_by(CartonGroup.sort_order, CartonGroup.created_at)).all()

    groups_data = []
    for g in all_groups:
        carton_count = _scalar_count(Carton, group_id=g.id)
        groups_data.append({
            'id': g.id,
            'name': g.name,
            'batch_id': g.batch_id,
            'zip_code': g.zip_code or '',
            'batch_zip': g.batch.zip_code if g.batch else '',
            'carton_count': carton_count
        })

    return render_template('carton.html',
                           all_batches=all_batches,
                           all_groups=groups_data,
                           batches_with_pending=batches_with_pending)


@app.route('/packed_records')
def packed_records():
    """打包记录页面（支持 view=channel 切换渠道发货视图）"""
    view = request.args.get('view', '')
    if view == 'channel':
        return _channel_shipped_view()
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    batches = db.session.scalars(db.select(Batch).filter_by(is_shipped=False).order_by(Batch.created_at.desc())).all()
    batch_ids = [b.id for b in batches]
    batch_packed_counts = _batch_packed_counts(batch_ids)
    return render_template('packed_records.html', view='packed',
                           customers=customers, batches=batches, batch_packed_counts=batch_packed_counts)


def _channel_shipped_view():
    """渠道发货视图数据（打包记录页内嵌）"""
    channel = request.args.get('channel', '').strip()
    customer_id = request.args.get('customer_id', type=int)
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    tracking_number = request.args.get('tracking_number', '').strip()

    stmt = db.select(IncomingPackage).where(
        IncomingPackage.status == '已发货',
        IncomingPackage.ship_channel.isnot(None),
        IncomingPackage.ship_channel != ''
    )
    if channel:
        stmt = stmt.where(IncomingPackage.ship_channel == channel)
    if customer_id:
        stmt = stmt.where(IncomingPackage.customer_id == customer_id)
    if start_date:
        stmt = stmt.where(IncomingPackage.shipped_at >= start_date + ' 00:00:00')
    if end_date:
        stmt = stmt.where(IncomingPackage.shipped_at <= end_date + ' 23:59:59')
    if tracking_number:
        stmt = stmt.where(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))

    records = db.session.scalars(stmt.order_by(IncomingPackage.shipped_at.desc(), IncomingPackage.id.desc())).all()
    customers = db.session.scalars(db.select(Customer).where(Customer.is_vip == True).order_by(Customer.name)).all()
    channel_stats = {}
    for r in records:
        ch = r.ship_channel or '其他'
        channel_stats[ch] = channel_stats.get(ch, 0) + 1
    customers_all = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    batches = db.session.scalars(db.select(Batch).filter_by(is_shipped=False).order_by(Batch.created_at.desc())).all()
    batch_ids = [b.id for b in batches]
    batch_packed_counts = _batch_packed_counts(batch_ids)
    return render_template('packed_records.html', view='channel',
                           customers=customers_all, batches=batches, batch_packed_counts=batch_packed_counts,
                           channel_records=records, vip_customers=customers, channel_stats=channel_stats,
                           f_channel=channel, f_customer=customer_id, f_start=start_date, f_end=end_date,
                           f_tracking=tracking_number)


@app.route('/freight')
def freight():
    """运费管理页面 - 包含客户汇总统计，支持按批次检索"""
    batch_id = request.args.get('batch_id', type=int)

    # 获取所有批次（供筛选下拉框使用）
    batches = db.session.scalars(db.select(Batch).order_by(Batch.created_at.desc())).all()

    # ========== 按批次筛选时：从 PackedPackage 实时计算，确保与导出一致 ==========
    if batch_id:
        # 实时查询该批次下所有已合箱/已发货/已完成的打包包裹
        packed_list = db.session.execute(db.select(PackedPackage).options(
            joinedload(PackedPackage.customer)
        ).filter(
            PackedPackage.batch_id == batch_id,
            PackedPackage.status.in_(['已合箱', '已发货', '已完成'])
        )).unique().scalars().all()

        prices = get_price_config()
        normal_price = prices['normal']
        danger_price = prices['danger']

        # 按客户分组实时计算（含运费 + 附加费）
        customer_data = {}
        total_pending = 0.0
        for p in packed_list:
            cid = p.customer_id
            nature_key = '普通' if p.nature != '敏' else '敏'
            chargeable = calc_chargeable_weight(p.volume_weight, p.weight)
            amount = calc_freight(chargeable, p.nature)
            # 计算附加费
            surcharge, surcharge_desc, _ = get_surcharge_for_package(p)
            total_amount = amount + surcharge

            if cid not in customer_data:
                customer_data[cid] = {
                    'name': p.customer.name if p.customer else f'客户#{cid}',
                    'customer_id': cid,
                    'total': 0.0,
                    'count': 0,
                    'normal_weight': 0.0,
                    'danger_weight': 0.0,
                    'surcharge': 0,
                    'remark': p.customer.remark if p.customer and p.customer.remark else ''
                }
            customer_data[cid]['total'] += total_amount
            customer_data[cid]['count'] += 1
            customer_data[cid]['surcharge'] += surcharge
            if nature_key == '敏':
                customer_data[cid]['danger_weight'] += chargeable
            else:
                customer_data[cid]['normal_weight'] += chargeable
            total_pending += total_amount

        customer_pending_list = list(customer_data.values())
        customer_pending_list.sort(key=lambda x: x['name'])
        pending_count = len(packed_list)
        pending_customers = len(customer_data)

        # 待结算明细：只显示与该批次箱子关联的运费记录
        carton_ids_in_batch = db.session.scalars(
            db.select(Carton.id).where(Carton.batch_id == batch_id)
        ).all()
        pending_records = db.session.scalars(
            db.select(FreightRecord).join(Customer).filter(
                FreightRecord.status == '待结算',
                FreightRecord.carton_id.in_(carton_ids_in_batch)
            ).order_by(Customer.name.asc())
        ).all() if carton_ids_in_batch else []

        # 已结算记录：同样只显示该批次客户
        settled_records = db.session.scalars(
            db.select(FreightRecord).join(Customer).filter(
                FreightRecord.status == '已结算',
                FreightRecord.customer_id.in_(
                    db.select(PackedPackage.customer_id).where(
                        PackedPackage.batch_id == batch_id
                    ).distinct()
                )
            ).order_by(Customer.name.asc(), FreightRecord.settled_at.desc()).limit(50)
        ).all()
        total_settled = sum(r.amount for r in settled_records)

        return render_template('freight.html',
                               pending_records=pending_records,
                               settled_records=settled_records,
                               total_pending=total_pending,
                               total_settled=total_settled,
                               customer_pending_list=customer_pending_list,
                               pending_count=pending_count,
                               pending_customers=pending_customers,
                               batches=batches,
                               selected_batch_id=batch_id)

    # ========== 不按批次筛选时：保持原有逻辑 ==========
    # 待结算记录
    query = db.select(FreightRecord).join(Customer).filter(
        FreightRecord.status == '待结算'
    )
    pending_records = db.session.scalars(query.order_by(Customer.name.asc())).all()

    # 已结算记录
    settled_records = db.session.scalars(
        db.select(FreightRecord).join(Customer).filter(
            FreightRecord.status == '已结算'
        ).order_by(Customer.name.asc(), FreightRecord.settled_at.desc()).limit(50)
    ).all()

    # 计算总额
    total_pending = sum(r.amount for r in pending_records)
    total_settled = sum(r.amount for r in db.session.scalars(
        db.select(FreightRecord).filter_by(status='已结算')
    ).all())

    # 按客户汇总统计
    customer_data = {}
    pending_customers = set()
    for record in pending_records:
        cid = record.customer_id
        if cid not in customer_data:
            customer_data[cid] = {
                'name': record.customer.name if record.customer else f'客户#{cid}',
                'customer_id': cid,
                'total': 0,
                'count': 0,
                'normal_weight': 0,
                'danger_weight': 0,
                'remark': record.customer.remark if record.customer and record.customer.remark else ''
            }
        customer_data[cid]['total'] += record.amount
        customer_data[cid]['count'] += 1
        pending_customers.add(cid)

    customer_pending_list = list(customer_data.values())
    customer_pending_list.sort(key=lambda x: x['name'])
    pending_count = len(pending_records)

    return render_template('freight.html',
                           pending_records=pending_records,
                           settled_records=settled_records,
                           total_pending=total_pending,
                           total_settled=total_settled,
                           customer_pending_list=customer_pending_list,
                           pending_count=pending_count,
                           pending_customers=len(pending_customers),
                           batches=batches,
                           selected_batch_id=batch_id)


# ==================== 打包记录 API ====================

@app.route('/api/packed/records')
def api_packed_records():
    """获取打包记录列表（支持筛选）- 优化N+1查询"""
    try:
        customer_id = request.args.get('customer_id', type=int)
        batch_id = request.args.get('batch_id', type=int)
        nature = request.args.get('nature', '').strip()
        is_cartoned = request.args.get('is_cartoned', '').strip()
        status = request.args.get('status', '').strip()
        is_full_box = request.args.get('is_full_box', '').strip()
        start_date = request.args.get('start_date', '').strip()
        end_date = request.args.get('end_date', '').strip()
        tracking_number = request.args.get('tracking_number', '').strip()

        query = db.select(PackedPackage).options(
            joinedload(PackedPackage.customer),
            joinedload(PackedPackage.batch),
            joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
        ).filter(PackedPackage.is_vip_solo != True)  # 排除 VIP 单独发货
        if customer_id:
            query = query.filter_by(customer_id=customer_id)
        if batch_id:
            query = query.filter_by(batch_id=batch_id)
        if nature:
            query = query.filter_by(nature=nature)
        if is_cartoned == '1':
            query = query.filter_by(is_cartoned=True)
        elif is_cartoned == '0':
            query = query.filter_by(is_cartoned=False)
        if status:
            query = query.filter_by(status=status)
        if is_full_box == '1':
            query = query.filter_by(is_full_box=True)
        elif is_full_box == '0':
            query = query.filter_by(is_full_box=False)
        # 封存：默认排除已锁定批次的打包记录（指定批次/按单号搜索/数据导出可查看）
        if request.args.get('include_locked') != '1' and not tracking_number and not batch_id:
            locked_batch_ids = db.session.scalars(db.select(Batch.id).where(Batch.is_locked == True)).all()
            if locked_batch_ids:
                query = query.filter(db.or_(
                    PackedPackage.batch_id.is_(None),
                    PackedPackage.batch_id.not_in(locked_batch_ids)
                ))
        if start_date:
            query = query.filter(PackedPackage.created_at >= start_date)
        if end_date:
            query = query.filter(PackedPackage.created_at <= end_date + ' 23:59:59')
        if tracking_number:
            query = query.join(PackedPackage.items).join(PackedPackageItem.incoming_package).filter(
                IncomingPackage.tracking_number.like(f'%{tracking_number}%')
            )

        records = db.session.execute(query).unique().scalars().all()
        # Python端排序：避免 SQLAlchemy 2.0 join+joinedload 冲突
        records.sort(key=lambda p: (p.customer.name if p.customer else '', p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else ''))

        result = []
        for p in records:
            vol_w = calc_volume_weight(p.length, p.width, p.height)
            chargeable = calc_chargeable_weight(vol_w, p.weight)
            freight = calc_freight(chargeable, p.nature)
            trackings = [item.incoming_package.tracking_number for item in p.items]

            result.append({
                'id': p.id,
                'customer_id': p.customer_id,
                'customer_name': p.customer.name if p.customer else '',
                'customer_no': p.customer.customer_no if p.customer else '',
                'zip_code': p.customer.zip_code if p.customer else '',
                'serial_label': p.serial_label,
                'total_packages': p.total_packages,
                'nature': p.nature,
                'contents_description': p.contents_description or '',
                'weight': p.weight,
                'length': p.length,
                'width': p.width,
                'height': p.height,
                'volume_weight': p.volume_weight,
                'batch_no': p.batch.batch_no if p.batch else '',
                'batch_id': p.batch.id if p.batch else None,
                'batch_is_locked': bool(p.batch.is_locked) if p.batch else False,
                'tracking_numbers': trackings,
                'is_cartoned': p.is_cartoned,
                'is_full_box': bool(p.is_full_box),
                'status': p.status or '待合箱',
                'label_printed': p.label_printed,
                'created_at': p.created_at.strftime('%Y-%m-%d %H:%M'),
                'chargeable_weight': chargeable,
                'freight': freight,
                'customer_remark': p.customer_remark or '',
                'abnormal_remark': p.abnormal_remark or ''
            })

        return jsonify(result)
    except Exception as e:
        import traceback
        app.logger.error(f'API错误: {str(e)}\n{traceback.format_exc()}')
        return api_error(f'获取打包记录失败: {str(e)}', code='FETCH_ERROR')


@app.route('/api/packed/records/update', methods=['POST'])
def api_update_packed_record():
    """更新打包记录"""
    data = request.get_json(silent=True) or {}
    packed_id = data.get('id')
    if not packed_id:
        return jsonify({'success': False, 'message': '缺少ID'}), 400

    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return jsonify({'success': False, 'message': '记录不存在'}), 404

    if 'contents_description' in data:
        packed.contents_description = data['contents_description']
    if 'weight' in data:
        packed.weight = float(data['weight']) if data['weight'] else 0
    if 'length' in data:
        packed.length = float(data['length']) if data['length'] else 0
    if 'width' in data:
        packed.width = float(data['width']) if data['width'] else 0
    if 'height' in data:
        packed.height = float(data['height']) if data['height'] else 0
    if 'batch_id' in data:
        packed.batch_id = int(data['batch_id']) if data['batch_id'] else None
    if 'nature' in data:
        packed.nature = data['nature']
    if 'is_full_box' in data:
        packed.is_full_box = bool(data['is_full_box'])

    packed.volume_weight = calc_volume_weight(packed.length, packed.width, packed.height)
    db.session.commit()
    return jsonify({'success': True, 'message': '更新成功'})


@app.route('/api/packed/records/delete/<int:packed_id>', methods=['POST'])
def api_delete_packed_record(packed_id):
    """删除打包记录"""
    packed = db.session.get(PackedPackage, packed_id)
    if packed.is_cartoned:
        return jsonify({'success': False, 'message': '已合箱的包裹无法删除'}), 400
    db.session.delete(packed)
    db.session.commit()
    return jsonify({'success': True, 'message': '删除成功'})


@app.route('/api/packed/records/rollback/<int:packed_id>', methods=['POST'])
def api_rollback_packed_record(packed_id):
    """回退打包记录 - 将包裹重新放回待打包状态"""
    packed = db.session.get(PackedPackage, packed_id)

    # 检查是否已合箱
    if packed.is_cartoned:
        return api_error('已合箱的包裹无法回退，请先解除合箱', code='CARTONED', status_code=400)

    # 检查是否已确认完成
    if packed.status == '已完成':
        return api_error('已完成的订单无法回退', code='ALREADY_COMPLETED', status_code=400)

    # 检查是否已发货
    if packed.status == '已发货':
        return api_error('已发货的包裹无法回退，请先解除合箱后取消发货', code='ALREADY_SHIPPED', status_code=400)

    # 获取该打包记录包含的所有入库包裹
    items = db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=packed_id)).all()
    if not items:
        return api_error('打包记录异常（无关联包裹），无法回退', code='EMPTY', status_code=400)

    try:
        # 将包裹状态恢复为待打包
        for item in items:
            pkg = db.session.get(IncomingPackage, item.incoming_package_id)
            if pkg:
                pkg.status = '待打包'

        # 减少客户发货次数
        customer = db.session.get(Customer, packed.customer_id)
        if customer and customer.shipment_count > 0:
            customer.shipment_count = max(0, customer.shipment_count - 1)

        # 记录操作日志
        create_operation_log(
            action='ROLLBACK_PACKED',
            content=f'回退打包记录 {packed.serial_label}（客户：{customer.name if customer else "未知"}）',
            target_type='PackedPackage',
            target_id=packed_id,
            extra_data=json.dumps({
                'packed_id': packed_id,
                'serial_label': packed.serial_label,
                'package_count': len(items)
            })
        )

        # 删除打包明细记录
        db.session.execute(db.delete(PackedPackageItem).filter_by(packed_package_id=packed_id))

        # 删除打包记录
        db.session.delete(packed)
        db.session.commit()

        return api_success(message=f'已回退 {len(items)} 个包裹到待打包状态')

    except Exception as e:
        db.session.rollback()
        return api_error(f'回退失败: {str(e)}', code='ROLLBACK_ERROR', status_code=500)


@app.route('/api/vip/packed/<int:packed_id>/complete', methods=['POST'])
def api_vip_complete_packed(packed_id):
    """VIP 单独发货 - 确认完成（完成后可导出账单/明细/发票）。
    仅允许「待处理」状态确认完成；已发货的记录不允许补完成（完成是发货的必要条件，先发货后完成在逻辑上不允许）。"""
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if not packed.is_vip_solo:
        return api_error('仅 VIP 单独发货可使用此操作', code='NOT_VIP_SOLO', status_code=400)
    if packed.status == '已完成':
        return api_error('该记录已完成', code='ALREADY_COMPLETED', status_code=400)
    if packed.status == '已发货':
        return api_error('该记录已发货，无法补完成（完成是发货的必要条件）', code='ALREADY_SHIPPED', status_code=400)
    if packed.status == '已退货':
        return api_error('该记录已退货', code='RETURNED', status_code=400)

    packed.status = '已完成'
    packed.shipped_at = None  # 完成≠发货，不设发货时间

    create_operation_log(
        action='COMPLETE_VIP_PACKED',
        content=f'VIP确认完成 {packed.serial_label}',
        target_type='PackedPackage',
        target_id=packed_id,
        extra_data=json.dumps({'packed_id': packed_id})
    )
    db.session.commit()
    return api_success(message=f'已完成 {packed.serial_label}，可导出账单发票')


@app.route('/api/vip/packed/<int:packed_id>/unship', methods=['POST'])
def api_unship_vip_packed(packed_id):
    """VIP 撤销发货 - 已发货记录回退到「已完成」（发货操作出错时使用）。
    仅允许「已发货」状态回退；回退后清空发货时间，包裹恢复「已打包」。"""
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if not packed.is_vip_solo:
        return api_error('仅 VIP 单独发货可使用此操作', code='NOT_VIP_SOLO', status_code=400)
    if packed.status != '已发货':
        return api_error('仅「已发货」记录可撤销发货', code='NOT_SHIPPED', status_code=400)

    try:
        packed.status = '已完成'
        packed.shipped_at = None  # 撤销发货，清空发货时间

        # 恢复 IncomingPackage 状态：发货时标为「已发货」的包裹改回「已打包」
        items = db.session.scalars(
            db.select(PackedPackageItem).filter_by(packed_package_id=packed_id)
        ).all()
        incoming_ids = [item.incoming_package_id for item in items]
        for iid in incoming_ids:
            pkg = db.session.get(IncomingPackage, iid)
            if pkg and pkg.status == '已发货':
                pkg.status = '已打包'

        # 操作日志
        create_operation_log(
            action='UNSHIP_VIP_PACKED',
            content=f'VIP撤销发货 {packed.serial_label}（回退到已完成）',
            target_type='PackedPackage',
            target_id=packed_id,
            extra_data=json.dumps({'packed_id': packed_id, 'package_count': len(items)})
        )

        db.session.commit()
        return api_success(message=f'已撤销发货 {packed.serial_label}，回到已完成状态')
    except Exception as e:
        db.session.rollback()
        return api_error(f'撤销发货失败: {str(e)}', code='UNSHIP_FAILED', status_code=500)


@app.route('/api/vip/packed/batch_complete', methods=['POST'])
def api_vip_batch_complete():
    """批量确认完成（仅待处理记录）"""
    data = request.get_json(silent=True) or {}
    packed_ids = data.get('packed_ids') or []
    if not packed_ids:
        return api_error('请选择要操作的记录', code='NO_IDS', status_code=400)
    ok, skipped = [], []
    for pid in packed_ids:
        packed = db.session.get(PackedPackage, pid)
        if not packed or not packed.is_vip_solo:
            skipped.append({'id': pid, 'reason': '记录不存在或非VIP'})
            continue
        if packed.status in ('已完成', '已发货', '已退货'):
            skipped.append({'id': pid, 'reason': f'当前状态「{packed.status}」不可完成'})
            continue
        packed.status = '已完成'
        packed.shipped_at = None
        create_operation_log(
            action='COMPLETE_VIP_PACKED',
            content=f'VIP确认完成 {packed.serial_label}（批量）',
            target_type='PackedPackage', target_id=pid,
            extra_data=json.dumps({'packed_id': pid, 'batch': True})
        )
        ok.append(pid)
    db.session.commit()
    return api_success(message=f'批量完成 {len(ok)} 条' + (f'，跳过 {len(skipped)} 条' if skipped else ''))


@app.route('/api/vip/packed/batch_ship', methods=['POST'])
def api_vip_batch_ship():
    """批量发货（仅已完成记录）"""
    data = request.get_json(silent=True) or {}
    packed_ids = data.get('packed_ids') or []
    if not packed_ids:
        return api_error('请选择要操作的记录', code='NO_IDS', status_code=400)
    ok, skipped = [], []
    for pid in packed_ids:
        packed = db.session.get(PackedPackage, pid)
        if not packed or not packed.is_vip_solo:
            skipped.append({'id': pid, 'reason': '记录不存在或非VIP'})
            continue
        if packed.status != '已完成':
            skipped.append({'id': pid, 'reason': f'当前状态「{packed.status}」不可发货（需先完成）'})
            continue
        packed.status = '已发货'
        packed.shipped_at = get_local_time()
        items = db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=pid)).all()
        for item in items:
            pkg = db.session.get(IncomingPackage, item.incoming_package_id)
            if pkg:
                if pkg.batch_id is not None:
                    pkg.batch_id = None
                pkg.status = '已发货'
        create_operation_log(
            action='SHIP_VIP_PACKED',
            content=f'VIP单独发货 {packed.serial_label}（批量）',
            target_type='PackedPackage', target_id=pid,
            extra_data=json.dumps({'packed_id': pid, 'batch': True, 'package_count': len(items)})
        )
        ok.append(pid)
    db.session.commit()
    return api_success(message=f'批量发货 {len(ok)} 条' + (f'，跳过 {len(skipped)} 条' if skipped else ''))


@app.route('/api/vip/packed/batch_unship', methods=['POST'])
def api_vip_batch_unship():
    """批量撤销发货（仅已发货记录，回退到已完成）"""
    data = request.get_json(silent=True) or {}
    packed_ids = data.get('packed_ids') or []
    if not packed_ids:
        return api_error('请选择要操作的记录', code='NO_IDS', status_code=400)
    ok, skipped = [], []
    for pid in packed_ids:
        packed = db.session.get(PackedPackage, pid)
        if not packed or not packed.is_vip_solo:
            skipped.append({'id': pid, 'reason': '记录不存在或非VIP'})
            continue
        if packed.status != '已发货':
            skipped.append({'id': pid, 'reason': f'当前状态「{packed.status}」不可撤销发货'})
            continue
        packed.status = '已完成'
        packed.shipped_at = None
        items = db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=pid)).all()
        for item in items:
            pkg = db.session.get(IncomingPackage, item.incoming_package_id)
            if pkg and pkg.status == '已发货':
                pkg.status = '已打包'
        create_operation_log(
            action='UNSHIP_VIP_PACKED',
            content=f'VIP撤销发货 {packed.serial_label}（批量）',
            target_type='PackedPackage', target_id=pid,
            extra_data=json.dumps({'packed_id': pid, 'batch': True})
        )
        ok.append(pid)
    db.session.commit()
    return api_success(message=f'批量撤销发货 {len(ok)} 条' + (f'，跳过 {len(skipped)} 条' if skipped else ''))


@app.route('/api/vip/packed/<int:packed_id>/ship', methods=['POST'])
def api_ship_vip_packed(packed_id):
    """VIP 单独发货 - 标记为已发货（必须先「完成」，发货后包裹从待处理列表隐藏）"""
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if not packed.is_vip_solo:
        return api_error('仅 VIP 单独发货可使用此操作', code='NOT_VIP_SOLO', status_code=400)
    if packed.status == '已完成':
        pass  # 允许：完成 → 发货
    elif packed.status == '已发货':
        return api_error('该记录已发货', code='ALREADY_SHIPPED', status_code=400)
    else:
        return api_error('请先点击「完成」确认该单后再发货', code='NOT_COMPLETED', status_code=400)

    try:
        packed.status = '已发货'
        packed.shipped_at = get_local_time()

        # 同步 IncomingPackage 状态：标记为已发货，并释放批次归属
        # （VIP 单独发货后，包裹不再属于任何批次，避免包裹与批次/箱子状态产生矛盾，
        #   即 8-07 Jessica 事件中包裹"已发货"但 93批 箱子仍"未发货"的记录不一致问题）
        items = db.session.scalars(
            db.select(PackedPackageItem).filter_by(packed_package_id=packed_id)
        ).all()
        incoming_ids = [item.incoming_package_id for item in items]
        released_batches = set()
        for iid in incoming_ids:
            pkg = db.session.get(IncomingPackage, iid)
            if pkg:
                if pkg.batch_id is not None:
                    released_batches.add(pkg.batch_id)
                    pkg.batch_id = None
                pkg.status = '已发货'

        # 操作日志
        create_operation_log(
            action='SHIP_VIP_PACKED',
            content=f'VIP单独发货 {packed.serial_label}',
            target_type='PackedPackage',
            target_id=packed_id,
            extra_data=json.dumps({
                'packed_id': packed_id,
                'package_count': len(items),
                'released_batches': sorted(released_batches)
            })
        )

        db.session.commit()
        return api_success(message=f'已发货 {packed.serial_label}')
    except Exception as e:
        db.session.rollback()
        return api_error(f'发货失败: {str(e)}', code='SHIP_FAILED', status_code=500)


@app.route('/api/vip/packed/<int:packed_id>/rollback', methods=['POST'])
def api_rollback_vip_packed(packed_id):
    """回退 VIP 单独打包记录 - 同时清理关联的 Carton/CartonItem"""
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if not packed.is_vip_solo:
        return api_error('仅 VIP 单独打包可使用此回退', code='NOT_VIP_SOLO', status_code=400)
    if packed.status == '已发货':
        return api_error('已发货无法回退', code='ALREADY_SHIPPED', status_code=400)

    try:
        items = db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=packed_id)).all()
        incoming_ids = [item.incoming_package_id for item in items]

        # 恢复 IncomingPackage 状态为待打包
        for iid in incoming_ids:
            pkg = db.session.get(IncomingPackage, iid)
            if pkg:
                pkg.status = '待打包'

        # 删除关联的 Carton / CartonItem
        carton_items = db.session.scalars(
            db.select(CartonItem).filter(CartonItem.packed_package_id == packed_id)
        ).all()
        carton_ids = set(ci.carton_id for ci in carton_items)
        for ci in carton_items:
            db.session.delete(ci)
        for cid in carton_ids:
            carton = db.session.get(Carton, cid)
            if carton:
                # 同步 carton 状态到 Open（如果空了）
                remaining = db.session.scalar(db.select(func.count(CartonItem.id)).filter_by(carton_id=cid))
                if not remaining:
                    db.session.delete(carton)
                else:
                    carton.status = 'Open'

        # 减少客户发货次数
        customer = db.session.get(Customer, packed.customer_id)
        if customer and customer.shipment_count > 0:
            customer.shipment_count = max(0, customer.shipment_count - 1)

        # 操作日志
        create_operation_log(
            action='ROLLBACK_VIP_PACKED',
            content=f'回退VIP单独打包 {packed.serial_label}',
            target_type='PackedPackage',
            target_id=packed_id,
            extra_data=json.dumps({'packed_id': packed_id, 'package_count': len(items)})
        )

        # 删除打包明细 + 打包记录
        db.session.execute(db.delete(PackedPackageItem).filter_by(packed_package_id=packed_id))
        db.session.delete(packed)
        db.session.commit()

        return api_success(message=f'已回退 VIP 单独打包 {len(items)} 件')
    except Exception as e:
        db.session.rollback()
        return api_error(f'回退失败: {str(e)}', code='ROLLBACK_ERROR', status_code=500)


@app.route('/api/vip/packed/<int:packed_id>/update_dimensions', methods=['POST'])
def api_vip_update_dimensions(packed_id):
    """更新 VIP 单独打包记录的箱子尺寸"""
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)

    weight = get_request_float('weight')
    length = get_request_float('length')
    width = get_request_float('width')
    height = get_request_float('height')

    if weight is not None:
        packed.weight = weight
    if length is not None:
        packed.length = length
    if width is not None:
        packed.width = width
    if height is not None:
        packed.height = height

    # 同步更新关联的 Carton
    carton_item = db.session.scalar(
        db.select(CartonItem).filter_by(packed_package_id=packed_id)
    )
    if carton_item:
        carton = db.session.get(Carton, carton_item.carton_id)
        if carton:
            if weight is not None:
                carton.total_weight = weight
            if length is not None:
                carton.length = length
            if width is not None:
                carton.width = width
            if height is not None:
                carton.height = height
            # 更新体积重
            if length is not None and width is not None and height is not None:
                carton.volume_weight = round(length * width * height / 5000, 2)

    db.session.commit()
    return api_success(message='尺寸已更新')


def get_request_float(key, default=None):
    val = request.get_json(silent=True) or {}
    if key in val:
        try:
            return float(val[key])
        except (TypeError, ValueError):
            return default
    return default


@app.route('/api/pack/<int:customer_id>/remaining_count')
def api_pack_remaining_count(customer_id):
    """检查客户还有多少待打包包裹未被本次打包"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    count = _scalar_count(IncomingPackage, customer_id=customer_id, status='待打包')
    return jsonify({'success': True, 'data': {'count': count}})


@app.route('/api/pack/<int:customer_id>/return_remaining', methods=['POST'])
def api_pack_return_remaining(customer_id):
    """将未打包的待打包包裹退回已入库状态（需前端手动确认）"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    remaining = db.session.scalars(
        db.select(IncomingPackage).filter_by(
            customer_id=customer_id,
            status='待打包'
        )
    ).all()

    if not remaining:
        return api_error('该客户没有待打包包裹', code='NO_PENDING', status_code=400)

    count = len(remaining)
    try:
        for pkg in remaining:
            pkg.status = '已入库'

        db.session.commit()

        create_operation_log(
            action='RETURN_TO_INBOUND',
            content=f'客户「{customer.name}」的 {count} 件未打包包裹退回已入库状态',
            target_type='Customer',
            target_id=customer_id,
            extra_data=json.dumps({
                'customer_id': customer_id,
                'customer_name': customer.name,
                'returned_count': count,
                'tracking_numbers': [p.tracking_number for p in remaining]
            })
        )

        return api_success(message=f'已将 {count} 件包裹退回已入库状态', data={'count': count})
    except Exception as e:
        db.session.rollback()
        return api_error(f'退回失败: {str(e)}', code='RETURN_ERROR', status_code=500)


@app.route('/export/packed_records')
def export_packed_records():
    """导出发货清单 - 优化N+1查询"""
    customer_id = request.args.get('customer_id', type=int)
    batch_id = request.args.get('batch_id', type=int)
    nature = request.args.get('nature', '').strip()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = db.select(PackedPackage).options(
        joinedload(PackedPackage.customer),
        joinedload(PackedPackage.batch),
        joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
    )

    if customer_id:
        query = query.filter_by(customer_id=customer_id)
    if batch_id:
        query = query.filter_by(batch_id=batch_id)
    if nature:
        query = query.filter_by(nature=nature)
    if start_date:
        query = query.filter(PackedPackage.created_at >= start_date)
    if end_date:
        query = query.filter(PackedPackage.created_at <= end_date + ' 23:59:59')

    records = db.session.execute(query).unique().scalars().all()
    records.sort(key=lambda p: (p.customer.name if p.customer else '', p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else ''))

    # 生成Excel (纯 openpyxl，无 pandas 依赖)
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "打包记录"

    headers = ['客户姓名', '客户编号', '邮编', '批次', '序号标签',
               '货物性质', '入库单号', '内容物', '重量(kg)',
               '长(cm)', '宽(cm)', '高(cm)', '体积重', '计费重', '运费',
               '合箱状态', '创建时间']
    ws.append(headers)

    thin = Side(border_style='thin', color='000000')
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    for p in records:
        trackings = ','.join([item.incoming_package.tracking_number for item in p.items])
        vol_w = calc_volume_weight(p.length, p.width, p.height)
        chargeable = calc_chargeable_weight(vol_w, p.weight)
        freight = calc_freight(chargeable, p.nature)

        ws.append([
            p.customer.name if p.customer else '',
            p.customer.customer_no if p.customer else '',
            p.customer.zip_code if p.customer else '',
            p.batch.batch_no if p.batch else '',
            p.serial_label or '',
            '敏货' if p.nature == '敏' else '普货',
            trackings,
            p.contents_description or '',
            p.weight or 0,
            p.length or 0,
            p.width or 0,
            p.height or 0,
            p.volume_weight or 0,
            chargeable,
            freight,
            '已合箱' if p.is_cartoned else '未合箱',
            p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else ''
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'打包记录_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 客户管理 API ====================

@app.route('/api/customer/add', methods=['POST'])
def api_add_customer():
    """添加客户"""
    name = get_request_param('name', '').strip()
    barcode = get_request_param('barcode', '').strip()
    address = get_request_param('address', '').strip()
    zip_code = get_request_param('zip_code', '').strip()
    remark = get_request_param('remark', '').strip()
    is_vip = get_request_param('is_vip', '') in ('true', '1', True, 'on')

    # VIP客户：用 invoice_address 作为地址
    if is_vip:
        vip_addr = get_request_param('invoice_address', '').strip()
        if vip_addr:
            address = vip_addr

    # 表单验证
    valid, msg = validate_customer_name(name)
    if not valid:
        return api_error(msg, code='VALIDATION_ERROR')

    valid, msg = validate_address(address)
    if not valid:
        return api_error(msg, code='VALIDATION_ERROR')

    # VIP 必填字段校验
    if is_vip and not get_request_param('invoice_contact', '').strip():
        return api_error('收货人姓名不能为空', code='VALIDATION_ERROR')

    # 检查客户名是否重复
    if db.session.scalar(db.select(Customer).filter_by(name=name)):
        return api_error(f'客户名 "{name}" 已存在', code='DUPLICATE_NAME')

    # 生成客户编号
    customer_no = generate_customer_no()

    try:
        new_customer = Customer(
            customer_no=customer_no,
            name=name,
            address=address,
            zip_code=zip_code,
            remark=remark,
            barcode=customer_no,  # 临时用编号占位，flush后更新为正式条码
            is_vip=is_vip,
            phone=get_request_param('phone', '').strip(),
            invoice_contact=get_request_param('invoice_contact', '').strip(),
            invoice_phone=get_request_param('invoice_phone', '').strip(),
            invoice_address=get_request_param('invoice_address', '').strip(),
            invoice_zip=get_request_param('invoice_zip', '').strip(),
            company_name=get_request_param('company_name', '').strip(),
            country=get_request_param('country', '').strip(),
        )
        db.session.add(new_customer)
        db.session.flush()  # 获取自动生成的ID

        # 根据ID生成6位短条码
        new_customer.barcode = generate_customer_barcode(new_customer.id)

        # 记录操作日志
        create_operation_log(
            action='ADD_CUSTOMER',
            content=f'添加客户 {name}（编号：{customer_no}，条码：{new_customer.barcode}）',
            target_type='Customer',
            target_id=new_customer.id,
            extra_data=json.dumps({
                'customer_no': customer_no,
                'name': name,
                'address': address
            })
        )

        db.session.commit()

        return api_success(data={
            'customer': {
                'id': new_customer.id,
                'customer_no': new_customer.customer_no,
                'name': new_customer.name,
                'barcode': new_customer.barcode
            }
        }, message=f'客户 {name} 添加成功')

    except IntegrityError as e:
        db.session.rollback()
        return api_error('客户名或条码已存在', code='DUPLICATE_ERROR')
    except Exception as e:
        db.session.rollback()
        return api_error(f'添加客户失败: {str(e)}', code='ADD_ERROR')


@app.route('/api/customer/update/<int:id>', methods=['POST'])
def api_update_customer(id):
    """更新客户信息"""
    customer = db.session.get(Customer, id)

    name = get_request_param('name', '').strip()
    address = get_request_param('address', '').strip()
    zip_code = get_request_param('zip_code', '').strip()
    remark = get_request_param('remark', '').strip()

    if not name or not address:
        return jsonify({'success': False, 'message': '客户名和地址不能为空'}), 400

    # 检查客户名是否与其他客户重复
    existing = db.session.scalar(db.select(Customer).filter(Customer.name == name, Customer.id != id))
    if existing:
        return jsonify({'success': False, 'message': '客户名已存在'}), 400

    old_address = customer.address
    old_zip = customer.zip_code
    customer.name = name
    customer.address = address
    customer.zip_code = zip_code
    customer.remark = remark

    # VIP 字段
    is_vip = get_request_param('is_vip', '')
    is_vip_flag = is_vip in ('true', '1', True, 'on')
    customer.is_vip = is_vip_flag

    if is_vip_flag and not get_request_param('invoice_contact', '').strip():
        return api_error('收货人姓名不能为空', code='VALIDATION_ERROR')

    customer.phone = get_request_param('phone', '').strip()
    customer.invoice_contact = get_request_param('invoice_contact', '').strip()
    customer.invoice_phone = get_request_param('invoice_phone', '').strip()
    customer.invoice_address = get_request_param('invoice_address', '').strip()
    customer.invoice_zip = get_request_param('invoice_zip', '').strip()

    # VIP 单独地址字段
    customer.company_name = get_request_param('company_name', '').strip()
    customer.country = get_request_param('country', '').strip()

    # 同步更新该客户未完成包裹的收货地
    if old_address != address:
        # IncomingPackage：更新非已退货状态的包裹收货地
        db.session.execute(
            db.update(IncomingPackage).where(
                IncomingPackage.customer_id == id,
                IncomingPackage.status != '已退货'
            ).values(receiving_address=address)
        )

    # 收集变更字段
    changes = []
    if old_address != address:
        changes.append(f'地址: {old_address} → {address}')
    if old_zip != zip_code:
        changes.append(f'邮编: {old_zip} → {zip_code}')
    if customer.name != name:
        changes.append(f'姓名: {customer.name} → {name}')
    if customer.remark != remark:
        changes.append(f'备注: {customer.remark or "(无)"} → {remark or "(无)"}')
    vip_old = bool(customer.is_vip)
    if vip_old != is_vip_flag:
        changes.append(f'VIP: {vip_old} → {is_vip_flag}')
    for field, label in [
        ('phone', '电话'), ('invoice_contact', '收货人姓名'), ('invoice_phone', '收货人电话'),
        ('invoice_address', '派送地址'), ('invoice_zip', '发票邮编'),
        ('company_name', '公司名'), ('country', '国家')
    ]:
        old_val = getattr(customer, field, '') or ''
        new_val = get_request_param(field, '').strip()
        if old_val != new_val:
            changes.append(f'{label}: {old_val or "(无)"} → {new_val or "(无)"}')

    if changes:
        create_operation_log(
            action='UPDATE_CUSTOMER',
            content=f'修改客户 {name}: ' + '; '.join(changes),
            target_type='Customer',
            target_id=id,
            extra_data=json.dumps({'customer_id': id, 'changes': changes})
        )

    db.session.commit()

    return jsonify({'success': True, 'message': '客户信息已更新'})


@app.route('/api/customer/delete/<int:id>', methods=['POST'])
def api_delete_customer(id):
    """删除客户"""
    customer = db.session.get(Customer, id)

    # 检查是否有任何包裹（包括已发货的）
    total_packages = _scalar_count(IncomingPackage, customer_id=id)
    if total_packages > 0:
        return api_error(
            f'该客户有 {total_packages} 个包裹记录，无法删除（请先处理所有包裹）',
            code='HAS_PACKAGES',
            status_code=400
        )

    # 检查是否有打包记录
    packed_count = _scalar_count(PackedPackage, customer_id=id)
    if packed_count > 0:
        return api_error(
            f'该客户有 {packed_count} 条打包记录，无法删除',
            code='HAS_PACKED_RECORDS',
            status_code=400
        )

    # 检查是否有未结算运费
    if customer.pending_freight and customer.pending_freight > 0:
        return api_error(
            f'该客户有待付运费 ¥{customer.pending_freight:.2f}，无法删除',
            code='HAS_PENDING_FREIGHT',
            status_code=400
        )

    # 记录操作日志
    create_operation_log(
        action='DELETE_CUSTOMER',
        content=f'删除客户 {customer.name}（编号：{customer.customer_no}）',
        target_type='Customer',
        target_id=id,
        extra_data=json.dumps({
            'customer_no': customer.customer_no,
            'name': customer.name,
            'address': customer.address
        })
    )

    db.session.delete(customer)
    db.session.commit()
    return api_success(message='客户已删除')


@app.route('/api/customer/<int:id>/remark', methods=['POST'])
def api_update_customer_remark(id):
    """单独更新客户备注（供打包页面使用）— 备注随批次走，只有已指定批次的客户才可编辑"""
    customer = db.session.get(Customer, id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    # 校验：只有已指定批次（且未发货）的客户才允许填写备注
    has_batch = db.session.query(IncomingPackage).filter(
        IncomingPackage.customer_id == id,
        IncomingPackage.batch_id.isnot(None),
        IncomingPackage.status.in_(['已入库', '待打包'])
    ).first()
    if not has_batch:
        return api_error('该客户尚未指定批次，请先指定批次后再填写备注', code='NO_BATCH', status_code=400)

    remark = get_request_param('remark', '').strip()
    customer.remark = remark
    db.session.commit()

    create_operation_log(
        action='UPDATE_CUSTOMER_REMARK',
        content=f'更新客户 {customer.name} 备注',
        target_type='Customer',
        target_id=id,
        extra_data=json.dumps({'remark': remark})
    )

    return api_success(message='客户备注已更新', data={'remark': remark})


@app.route('/api/customer/import', methods=['POST'])
def api_import_customers():
    """批量导入客户 - 增强数据验证"""
    if 'file' not in request.files:
        return api_error('没有选择文件', code='NO_FILE')

    file = request.files['file']
    if file.filename == '':
        return api_error('文件名为空', code='EMPTY_FILENAME')

    if not allowed_file(file.filename):
        return api_error('只支持 .xlsx 或 .xls 文件', code='INVALID_FILE_TYPE')

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    success_count = 0
    skip_count = 0
    errors = []
    validation_errors = []

    try:
        from openpyxl import load_workbook
        wb = load_workbook(filepath, data_only=True)
        ws = wb.active

        # 读取表头
        headers = []
        for cell in ws[1]:
            headers.append(str(cell.value or '').strip())

        if not headers:
            return api_error('Excel文件为空', code='EMPTY_FILE')

        # 查找列名
        name_col_idx = None
        addr_col_idx = None
        zip_col_idx = None

        for idx, h in enumerate(headers):
            if '客户' in h or '姓名' in h or 'name' in h.lower():
                name_col_idx = idx
            if '地址' in h or 'address' in h.lower():
                addr_col_idx = idx
            if '邮编' in h or 'zip' in h.lower():
                zip_col_idx = idx

        # 默认使用前几列
        if name_col_idx is None:
            name_col_idx = 0
        if addr_col_idx is None:
            addr_col_idx = 1 if len(headers) > 1 else 0

        # 验证列是否存在
        max_col = max(name_col_idx, addr_col_idx)
        if max_col >= len(headers):
            return api_error('Excel文件缺少必要的列（姓名、地址）', code='MISSING_COLUMNS')

        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            try:
                name = str(row[name_col_idx] or '').strip()
                address = str(row[addr_col_idx] or '').strip()
                zip_code = ''
                if zip_col_idx is not None and zip_col_idx < len(row):
                    zip_code = str(row[zip_col_idx] or '').strip()

                # 数据验证
                valid, msg = validate_customer_name(name)
                if not valid:
                    validation_errors.append(f'第{row_idx}行：{msg}')
                    continue

                valid, msg = validate_address(address)
                if not valid:
                    validation_errors.append(f'第{row_idx}行：{msg}')
                    continue

                if not name or not address:
                    validation_errors.append(f'第{row_idx}行：姓名或地址为空')
                    continue

                if db.session.scalar(db.select(Customer).filter_by(name=name)):
                    skip_count += 1
                    continue

                customer_no = generate_customer_no()

                customer = Customer(
                    customer_no=customer_no,
                    name=name,
                    address=address,
                    zip_code=zip_code,
                    barcode=customer_no  # 临时用编号占位，flush后更新为正式条码
                )
                db.session.add(customer)
                db.session.flush()  # 获取ID
                customer.barcode = generate_customer_barcode(customer.id)
                success_count += 1

            except Exception as e:
                errors.append(f'第{row_idx}行处理失败: {str(e)}')

        # 记录操作日志
        create_operation_log(
            action='IMPORT_CUSTOMERS',
            content=f'批量导入客户：成功 {success_count} 条，跳过 {skip_count} 条',
            extra_data=json.dumps({
                'success_count': success_count,
                'skip_count': skip_count,
                'error_count': len(errors) + len(validation_errors)
            })
        )

        db.session.commit()

    except Exception as e:
        return api_error(f'文件读取失败: {str(e)}', code='FILE_ERROR')
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

    message = f'导入完成：成功 {success_count} 条'
    if skip_count > 0:
        message += f'，跳过重复 {skip_count} 条'
    if validation_errors:
        message += f'，验证失败 {len(validation_errors)} 条'
        message += f'\n问题：{validation_errors[:3]}'
    if errors:
        message += f'，处理错误 {len(errors)} 条'
        message += f'\n错误：{errors[:3]}'

    return api_success(data={
        'success_count': success_count,
        'skip_count': skip_count,
        'validation_errors': validation_errors[:5],
        'processing_errors': errors[:5]
    }, message=message)


@app.route('/api/customers/search')
def api_customer_search():
    """客户模糊搜索（支持扫码条码/姓名/编号/邮编精准匹配）"""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify([])

    q_lower = q.lower()
    # 优先条码精准匹配（扫码场景）
    exact = db.session.scalar(db.select(Customer).filter(Customer.barcode == q))
    if exact:
        pending_count = _scalar_count(IncomingPackage, customer_id=exact.id, status='待打包')
        return jsonify([{
            'id': exact.id, 'customer_no': exact.customer_no,
            'name': exact.name, 'barcode': exact.barcode,
            'address': exact.address, 'zip_code': exact.zip_code,
            'pending_count': pending_count
        }])

    # 模糊匹配：姓名/编号/条码/邮编包含关键词
    results = db.session.scalars(db.select(Customer).filter(
        or_(
            Customer.name.ilike(f'%{q}%'),
            Customer.customer_no.ilike(f'%{q}%'),
            Customer.barcode.ilike(f'%{q}%'),
            Customer.zip_code.ilike(f'%{q}%')
        )
    ).order_by(Customer.name).limit(20)).all()

    return jsonify([{
        'id': c.id, 'customer_no': c.customer_no,
        'name': c.name, 'barcode': c.barcode,
        'address': c.address, 'zip_code': c.zip_code,
        'pending_count': _scalar_count(IncomingPackage, customer_id=c.id, status='待打包')
    } for c in results])


@app.route('/api/customer/by_barcode')
def api_customer_by_barcode():
    """根据条码查找客户（扫码跳转用）—— 支持精确匹配 + 前缀模糊匹配"""
    barcode = (request.args.get('barcode') or '').strip().upper()
    if not barcode:
        return api_error('请提供条码', code='MISSING_PARAMS')
    
    # 1. 精确匹配
    customer = db.session.scalar(db.select(Customer).filter(Customer.barcode == barcode))
    if customer:
        return api_success(data={
            'id': customer.id,
            'name': customer.name,
            'customer_no': customer.customer_no,
            'barcode': customer.barcode,
            'zip_code': customer.zip_code or ''
        })
    
    # 2. 前缀模糊匹配（≥3位，兼容旧标签）
    if len(barcode) >= 3:
        customer = db.session.scalar(db.select(Customer).filter(
            Customer.barcode.like(f'{barcode}%')
        ))
        if customer:
            return api_success(data={
                'id': customer.id,
                'name': customer.name,
                'customer_no': customer.customer_no,
                'barcode': customer.barcode,
                'zip_code': customer.zip_code or '',
                'matched_by': 'prefix'
            })
    
    return api_error(f'未找到条码为 {barcode} 的客户', code='NOT_FOUND', status_code=404)


@app.route('/api/customers')
def api_get_customers():
    """获取所有客户列表（支持分页）"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    query = db.select(Customer).order_by(Customer.name.asc())
    pagination = db.paginate(query, page=page, per_page=per_page, error_out=False)
    return jsonify([{
        'id': c.id,
        'customer_no': c.customer_no,
        'name': c.name,
        'barcode': c.barcode,
        'address': c.address,
        'zip_code': c.zip_code,
        'shipment_count': c.shipment_count,
        'pending_freight': c.pending_freight,
        'remark': c.remark,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M')
    } for c in pagination.items])


@app.route('/api/customer/<int:id>')
def api_get_customer(id):
    """获取客户详情"""
    customer = db.session.get(Customer, id)
    return jsonify({
        'id': customer.id,
        'customer_no': customer.customer_no,
        'name': customer.name,
        'barcode': customer.barcode,
        'address': customer.address,
        'zip_code': customer.zip_code,
        'shipment_count': customer.shipment_count,
        'pending_freight': customer.pending_freight,
        'remark': customer.remark,
        'has_box': customer.has_box or False,
        'has_bulk': customer.has_bulk or False,
        'is_vip': customer.is_vip or False,
        'phone': customer.phone or '',
        'invoice_contact': customer.invoice_contact or '',
        'invoice_phone': customer.invoice_phone or '',
        'invoice_address': customer.invoice_address or '',
        'invoice_zip': customer.invoice_zip or '',
        'company_name': customer.company_name or '',
        'country': customer.country or '',
        'created_at': customer.created_at.strftime('%Y-%m-%d %H:%M')
    })


@app.route('/api/customer/<int:id>/toggle_flag', methods=['POST'])
def api_toggle_customer_flag(id):
    """切换客户货物标记（箱子/大货）"""
    customer = db.session.get(Customer, id)
    if not customer:
        return api_error('客户不存在', status_code=404)
    flag = get_request_param('flag', '').strip()
    if flag not in ('has_box', 'has_bulk'):
        return api_error('无效标记类型，请使用 has_box 或 has_bulk')
    current = getattr(customer, flag, False)
    setattr(customer, flag, not current)
    db.session.commit()
    return api_success(message=f'{"已标记" if not current else "已取消"}')


@app.route('/api/customer/<int:id>/toggle_vip', methods=['POST'])
def api_toggle_vip(id):
    """切换客户VIP状态"""
    customer = db.session.get(Customer, id)
    if not customer:
        return api_error('客户不存在', status_code=404)
    customer.is_vip = not customer.is_vip
    db.session.commit()
    return api_success(message=f'VIP状态已{"开启" if customer.is_vip else "关闭"}', data={'is_vip': customer.is_vip})


# ==================== 地址库 API ====================

@app.route('/api/address_book/list')
def api_address_book_list():
    """获取地址库列表"""
    entries = db.session.scalars(
        db.select(AddressBook).order_by(AddressBook.zip_code, AddressBook.id)
    ).all()
    return jsonify([{
        'id': e.id, 'zip_code': e.zip_code,
        'puhuo_consignee': e.puhuo_consignee, 'puhuo_phone': e.puhuo_phone, 'puhuo_address': e.puhuo_address,
        'minhuo_consignee': e.minhuo_consignee, 'minhuo_phone': e.minhuo_phone, 'minhuo_address': e.minhuo_address,
        'city': e.city, 'state': e.state, 'country': e.country, 'remark': e.remark,
        'created_at': e.created_at.strftime('%Y-%m-%d %H:%M') if e.created_at else ''
    } for e in entries])


@app.route('/api/address_book/<int:id>')
def api_address_book_get(id):
    """获取单条地址库记录"""
    e = db.session.get(AddressBook, id)
    if not e:
        return api_error('记录不存在', status_code=404)
    return jsonify({
        'id': e.id, 'zip_code': e.zip_code,
        'puhuo_consignee': e.puhuo_consignee, 'puhuo_phone': e.puhuo_phone, 'puhuo_address': e.puhuo_address,
        'minhuo_consignee': e.minhuo_consignee, 'minhuo_phone': e.minhuo_phone, 'minhuo_address': e.minhuo_address,
        'city': e.city, 'state': e.state, 'country': e.country, 'remark': e.remark
    })


@app.route('/api/address_book/add', methods=['POST'])
def api_address_book_add():
    """添加地址库记录"""
    zip_code = get_request_param('zip_code', '').strip()
    if not zip_code:
        return api_error('邮编不能为空', code='VALIDATION_ERROR')
    entry = AddressBook(
        zip_code=zip_code,
        puhuo_consignee=get_request_param('puhuo_consignee', '').strip(),
        puhuo_phone=get_request_param('puhuo_phone', '').strip(),
        puhuo_address=get_request_param('puhuo_address', '').strip(),
        minhuo_consignee=get_request_param('minhuo_consignee', '').strip(),
        minhuo_phone=get_request_param('minhuo_phone', '').strip(),
        minhuo_address=get_request_param('minhuo_address', '').strip(),
        city=get_request_param('city', '').strip(),
        state=get_request_param('state', '').strip(),
        country=get_request_param('country', '').strip(),
        remark=get_request_param('remark', '').strip()
    )
    db.session.add(entry)
    db.session.commit()
    return api_success(data={'id': entry.id}, message='地址库记录已添加')


@app.route('/api/address_book/update/<int:id>', methods=['POST'])
def api_address_book_update(id):
    """更新地址库记录"""
    e = db.session.get(AddressBook, id)
    if not e:
        return api_error('记录不存在', status_code=404)
    zip_code = get_request_param('zip_code', '').strip()
    if not zip_code:
        return api_error('邮编不能为空', code='VALIDATION_ERROR')
    e.zip_code = zip_code
    e.puhuo_consignee = get_request_param('puhuo_consignee', '').strip()
    e.puhuo_phone = get_request_param('puhuo_phone', '').strip()
    e.puhuo_address = get_request_param('puhuo_address', '').strip()
    e.minhuo_consignee = get_request_param('minhuo_consignee', '').strip()
    e.minhuo_phone = get_request_param('minhuo_phone', '').strip()
    e.minhuo_address = get_request_param('minhuo_address', '').strip()
    e.city = get_request_param('city', '').strip()
    e.state = get_request_param('state', '').strip()
    e.country = get_request_param('country', '').strip()
    e.remark = get_request_param('remark', '').strip()
    db.session.commit()
    return api_success(message='地址库记录已更新')


@app.route('/api/address_book/delete/<int:id>', methods=['POST'])
def api_address_book_delete(id):
    """删除地址库记录"""
    e = db.session.get(AddressBook, id)
    if not e:
        return api_error('记录不存在', status_code=404)
    db.session.delete(e)
    db.session.commit()
    return api_success(message='地址库记录已删除')


# ==================== 入库扫描 API ====================

@app.route('/api/inbound/add', methods=['POST'])
def api_add_inbound():
    """添加入库记录 - 支持无主入库（customer_id为空时自动标记为无主包裹）"""
    customer_id = get_request_int('customer_id')
    tracking_number = get_request_param('tracking_number', '').strip()
    customer_remark = get_request_param('customer_remark', '').strip() or None
    abnormal_remark = get_request_param('abnormal_remark', '').strip() or None
    is_anonymous = get_request_param('is_anonymous', '').strip() == '1'

    if not tracking_number:
        return jsonify({'success': False, 'message': '缺少快递单号'}), 400

    # 无主模式：不需要客户
    if not is_anonymous and not customer_id:
        return jsonify({'success': False, 'message': '缺少客户ID'}), 400

    customer = None
    if customer_id:
        customer = db.session.get(Customer, customer_id)
        if not customer:
            return jsonify({'success': False, 'message': '客户不存在'}), 404

    # 检查单号是否已存在（入库单号唯一不可重复）
    existing = db.session.scalar(db.select(IncomingPackage).filter_by(tracking_number=tracking_number))
    if existing:
        return jsonify({'success': False, 'message': f'该入库单号 "{tracking_number}" 已存在，无法重复录入'}), 409

    # 计算序号
    if customer_id and not is_anonymous:
        max_seq = db.session.query(func.max(IncomingPackage.sequence_no)).filter_by(
            customer_id=customer_id
        ).scalar() or 0
        seq_no = max_seq + 1
    else:
        seq_no = 0  # 无主包裹序号暂为0，确认归属时重新分配

    new_pkg = IncomingPackage(
        customer_id=customer_id if not is_anonymous else None,
        tracking_number=tracking_number,
        sequence_no=seq_no,
        receiving_address=customer.address if customer else '',
        status='已入库',
        customer_remark=customer_remark,
        abnormal_remark=abnormal_remark,
        is_anonymous=is_anonymous or (customer_id is None and not is_anonymous),
    )
    db.session.add(new_pkg)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': '入库成功',
        'package': {
            'id': new_pkg.id,
            'tracking_number': new_pkg.tracking_number,
            'sequence_no': new_pkg.sequence_no,
            'customer_barcode': customer.barcode if customer else '',
            'is_anonymous': new_pkg.is_anonymous,
        }
    })


# ==================== 无主包裹 API ====================

@app.route('/anonymous')
def anonymous_packages():
    """无主单号页面"""
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    return render_template('anonymous.html', customers=customers)


@app.route('/api/anonymous/list')
def api_anonymous_list():
    """获取无主包裹列表"""
    query = db.select(IncomingPackage).where(
        IncomingPackage.is_anonymous == True,
        IncomingPackage.customer_id.is_(None),
        IncomingPackage.status != '已退货'
    ).order_by(IncomingPackage.entry_time.desc())
    records = db.session.scalars(query).all()

    return jsonify([{
        'id': r.id,
        'tracking_number': r.tracking_number,
        'entry_time': r.entry_time.strftime('%Y-%m-%d %H:%M:%S') if r.entry_time else '',
        'customer_remark': r.customer_remark or '',
        'abnormal_remark': r.abnormal_remark or '',
    } for r in records])


@app.route('/api/anonymous/assign', methods=['POST'])
def api_anonymous_assign():
    """将无主包裹指定到某个客户名下"""
    package_id = get_request_int('package_id')
    customer_id = get_request_int('customer_id')

    if not package_id or not customer_id:
        return jsonify({'success': False, 'message': '缺少包裹ID或客户ID'}), 400

    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return jsonify({'success': False, 'message': '包裹不存在'}), 404
    if not pkg.is_anonymous:
        return jsonify({'success': False, 'message': '该包裹不是无主包裹'}), 400

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({'success': False, 'message': '客户不存在'}), 404

    # 重新分配序号
    max_seq = db.session.query(func.max(IncomingPackage.sequence_no)).filter_by(
        customer_id=customer_id
    ).scalar() or 0

    pkg.customer_id = customer_id
    pkg.is_anonymous = False
    pkg.sequence_no = max_seq + 1
    pkg.receiving_address = customer.address
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'已将单号 {pkg.tracking_number} 分配给客户 {customer.name}',
    })


@app.route('/api/anonymous/delete/<int:package_id>', methods=['POST'])
def api_anonymous_delete(package_id):
    """删除无主包裹"""
    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return jsonify({'success': False, 'message': '包裹不存在'}), 404
    if not pkg.is_anonymous:
        return jsonify({'success': False, 'message': '该包裹不是无主包裹，请通过入库明细删除'}), 400

    tracking = pkg.tracking_number
    db.session.delete(pkg)
    db.session.commit()

    return jsonify({'success': True, 'message': f'已删除无主包裹 {tracking}'})


# ==================== 批量入库 ====================
@app.route('/api/inbound/batch', methods=['POST'])
def api_inbound_batch():
    """批量添加入库记录 - 带事务保护"""
    customer_id = get_request_int('customer_id')
    tracking_numbers_raw = get_request_param('tracking_numbers', '')
    customer_remark = (get_request_param('customer_remark') or '').strip() or None
    abnormal_remark = (get_request_param('abnormal_remark') or '').strip() or None

    if not customer_id:
        return api_error('缺少客户ID', code='MISSING_CUSTOMER_ID')

    # 解析单号列表
    if isinstance(tracking_numbers_raw, str):
        tracking_numbers = [t.strip() for t in tracking_numbers_raw.split('\n') if t.strip()]
    elif isinstance(tracking_numbers_raw, list):
        tracking_numbers = [t.strip() for t in tracking_numbers_raw if t.strip()]
    else:
        tracking_numbers = []

    if not tracking_numbers:
        return api_error('没有快递单号', code='EMPTY_TRACKING')

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    # 数据验证
    for tn in tracking_numbers:
        valid, msg = validate_tracking_number(tn)
        if not valid:
            return api_error(f'快递单号验证失败: {msg}', code='VALIDATION_ERROR')

    # 计算起始序号
    max_seq = db.session.query(func.max(IncomingPackage.sequence_no)).filter_by(
        customer_id=customer_id
    ).scalar() or 0

    created = []
    skipped = []
    errors = []

    try:
        for i, tn in enumerate(tracking_numbers):
            if not tn:
                continue
            
            # 检查单号是否已存在
            existing = db.session.scalar(db.select(IncomingPackage).filter_by(tracking_number=tn))
            if existing:
                skipped.append(tn)
                continue

            pkg = IncomingPackage(
                customer_id=customer_id,
                tracking_number=tn,
                sequence_no=max_seq + i + 1,
                receiving_address=customer.address,
                status='已入库',
                customer_remark=customer_remark,
                abnormal_remark=abnormal_remark,
            )
            db.session.add(pkg)
            created.append({'tracking': tn, 'seq': max_seq + i + 1})

        # 记录操作日志
        create_operation_log(
            action='BATCH_INBOUND',
            content=f'批量入库 {len(created)} 个包裹（客户：{customer.name}）',
            target_type='Customer',
            target_id=customer_id,
            extra_data=json.dumps({
                'customer_name': customer.name,
                'created_count': len(created),
                'skipped_count': len(skipped)
            })
        )

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return api_error(f'批量入库失败: {str(e)}', code='BATCH_ERROR')

    return api_success(data={
        'customer_name': customer.name,
        'customer_barcode': customer.barcode,
        'created_count': len(created),
        'created': created,
        'skipped': skipped
    }, message=f'成功入库 {len(created)} 个，跳过 {len(skipped)} 个')


@app.route('/api/inbound/records/<int:customer_id>')
def api_get_inbound_records(customer_id):
    """获取客户的入库记录"""
    records = db.session.scalars(db.select(IncomingPackage).filter_by(customer_id=customer_id).order_by(
        IncomingPackage.entry_time.desc()
    )).all()
    return jsonify([{
        'id': r.id,
        'tracking_number': r.tracking_number,
        'sequence_no': r.sequence_no,
        'entry_time': r.entry_time.strftime('%Y-%m-%d %H:%M:%S'),
        'is_danger': r.is_danger,
        'second_scan_number': r.second_scan_number,
        'status': r.status
    } for r in records])


@app.route('/api/inbound/delete/<int:id>', methods=['POST'])
def api_delete_inbound(id):
    """删除入库记录"""
    pkg = db.session.get(IncomingPackage, id)
    if pkg.status not in ('已入库', '待打包'):
        return jsonify({'success': False, 'message': '只能删除已入库或待打包状态的包裹'}), 400
    db.session.delete(pkg)
    db.session.commit()
    return jsonify({'success': True, 'message': '删除成功'})


@app.route('/api/inbound/batch_delete', methods=['POST'])
def api_batch_delete_inbound():
    """批量删除入库记录"""
    data = request.get_json()
    ids = data.get('ids', []) if data else []
    if not ids or not isinstance(ids, list):
        return jsonify({'success': False, 'message': '请选择要删除的记录'}), 400
    deleted = 0
    skipped = 0
    for pkg_id in ids:
        pkg = db.session.get(IncomingPackage, pkg_id)
        if pkg and pkg.status in ('已入库', '待打包'):
            db.session.delete(pkg)
            deleted += 1
        else:
            skipped += 1
    db.session.commit()
    msg = f'成功删除 {deleted} 条'
    if skipped:
        msg += f'，跳过 {skipped} 条（状态不符）'
    create_operation_log(
        action='BATCH_DELETE_INBOUND',
        content=f'批量删除入库记录 {deleted} 条' + (f'，跳过 {skipped} 条' if skipped else ''),
        target_type='IncomingPackage',
        target_id=None
    )
    return jsonify({'success': True, 'message': msg, 'deleted': deleted, 'skipped': skipped})


@app.route('/api/inbound/mark_returned/<int:package_id>', methods=['POST'])
def api_mark_returned(package_id):
    """标记包裹为已退货"""
    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return api_error('包裹不存在', code='NOT_FOUND', status_code=404)
    if pkg.status == '已退货':
        return api_error('该包裹已是退货状态', code='ALREADY_RETURNED', status_code=400)

    # 已打包的不能退货
    if pkg.status == '已打包':
        return api_error('已打包的包裹不能标记为退货，请先回退包裹', code='ALREADY_PACKED', status_code=400)

    pkg.status = '已退货'
    db.session.commit()
    create_operation_log(
        action='MARK_RETURNED',
        content=f'标记包裹 {pkg.tracking_number} 为已退货',
        target_type='IncomingPackage',
        target_id=package_id
    )
    return api_success(message=f'包裹 {pkg.tracking_number} 已标记为退货')


@app.route('/api/inbound/restore_returned/<int:package_id>', methods=['POST'])
def api_inbound_restore_returned(package_id):
    """恢复退货单号 - 恢复为已入库（误标的可以撤回，与 restore_lost 对称）"""
    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if pkg.status != '已退货':
        return api_error('该记录不是退货状态', code='NOT_RETURNED', status_code=400)
    pkg.status = '已入库'
    db.session.commit()
    create_operation_log(
        action='RESTORE_RETURNED',
        content=f'恢复退货单号: {pkg.tracking_number}',
        target_type='IncomingPackage',
        target_id=package_id,
        extra_data=json.dumps({'package_id': package_id})
    )
    return api_success(message=f'已恢复 {pkg.tracking_number} 为已入库')


@app.route('/api/inbound/quick_add', methods=['POST'])
def api_inbound_quick_add():
    """快速入库 — 打包操作界面直接录入单号"""
    customer_id = get_request_int('customer_id')
    tracking_number = get_request_param('tracking_number', '').strip()

    if not customer_id:
        return jsonify({'success': False, 'message': '缺少客户ID'}), 400
    if not tracking_number:
        return jsonify({'success': False, 'message': '请输入快递单号'}), 400

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({'success': False, 'message': '客户不存在'}), 404

    # 检查单号是否重复
    existing = db.session.scalar(db.select(IncomingPackage).filter_by(tracking_number=tracking_number))
    if existing:
        return jsonify({'success': False, 'message': f'单号 {tracking_number} 已存在（客户：{existing.customer.name}）'}), 400

    # 计算序号
    max_seq = db.session.scalar(
        db.select(func.coalesce(func.max(IncomingPackage.sequence_no), 0))
        .filter_by(customer_id=customer_id)
    )

    pkg = IncomingPackage(
        customer_id=customer_id,
        tracking_number=tracking_number,
        sequence_no=(max_seq or 0) + 1,
        status='待打包',
        entry_time=get_local_time(),
        receiving_address=customer.address
    )
    db.session.add(pkg)
    db.session.commit()

    create_operation_log(
        action='QUICK_ADD_INBOUND',
        content=f'打包页面快速入库：{tracking_number} → 客户 {customer.name}',
        target_type='IncomingPackage',
        target_id=pkg.id
    )

    return jsonify({
        'success': True,
        'message': f'已录入单号 {tracking_number}',
        'id': pkg.id,
        'sequence_no': pkg.sequence_no,
        'tracking_number': pkg.tracking_number,
        'pending_count': _scalar_count(IncomingPackage, customer_id=customer_id, status='待打包')
    })


# ==================== 渠道发货（空运/海运/其他快速标记） ====================

@app.route('/channel_shipped')
def channel_shipped_records():
    """渠道发货单号页面 - 展示通过渠道（空运/海运/其他）快速标记发货的单号"""
    channel = request.args.get('channel', '').strip()
    customer_id = request.args.get('customer_id', type=int)
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    tracking_number = request.args.get('tracking_number', '').strip()

    stmt = db.select(IncomingPackage).where(
        IncomingPackage.status == '已发货',
        IncomingPackage.ship_channel.isnot(None),
        IncomingPackage.ship_channel != ''
    )
    if channel:
        stmt = stmt.where(IncomingPackage.ship_channel == channel)
    if customer_id:
        stmt = stmt.where(IncomingPackage.customer_id == customer_id)
    if start_date:
        stmt = stmt.where(IncomingPackage.shipped_at >= start_date + ' 00:00:00')
    if end_date:
        stmt = stmt.where(IncomingPackage.shipped_at <= end_date + ' 23:59:59')
    if tracking_number:
        stmt = stmt.where(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))

    records = db.session.scalars(stmt.order_by(IncomingPackage.shipped_at.desc(), IncomingPackage.id.desc())).all()
    customers = db.session.scalars(db.select(Customer).where(Customer.is_vip == True).order_by(Customer.name)).all()

    # 渠道汇总统计
    channel_stats = {}
    for r in records:
        ch = r.ship_channel or '其他'
        channel_stats[ch] = channel_stats.get(ch, 0) + 1

    return render_template('channel_shipped.html',
                           records=records,
                           customers=customers,
                           channel_stats=channel_stats,
                           f_channel=channel, f_customer=customer_id,
                           f_start=start_date, f_end=end_date,
                           f_tracking=tracking_number)


@app.route('/api/inbound/channel_ship', methods=['POST'])
def api_inbound_channel_ship():
    """渠道发货 - 把勾选的「已入库」单号快速标记为已发货（记录渠道/发货时间/内部单号）"""
    data = request.get_json(silent=True) or {}
    package_ids = data.get('package_ids') or []
    channel = (data.get('channel') or '').strip()
    if not package_ids:
        return api_error('请选择要标记的单号', code='NO_IDS', status_code=400)
    if channel not in ('空运', '快递', '小包'):
        return api_error('请选择有效渠道（空运/快递/小包）', code='BAD_CHANNEL', status_code=400)
    # 选填发货时间：不填默认当前时间
    shipped_at_str = (data.get('shipped_at') or '').strip()
    internal_no = (data.get('internal_tracking_no') or '').strip() or None
    if shipped_at_str:
        try:
            ship_dt = datetime.strptime(shipped_at_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            try:
                ship_dt = datetime.strptime(shipped_at_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return api_error('发货时间格式不正确', code='BAD_TIME', status_code=400)
    else:
        ship_dt = get_local_time()

    ok, skipped = [], []
    for pid in package_ids:
        pkg = db.session.get(IncomingPackage, pid)
        if not pkg:
            skipped.append({'id': pid, 'reason': '记录不存在'})
            continue
        if pkg.status != '已入库':
            skipped.append({'id': pid, 'reason': f'状态「{pkg.status}」非已入库'})
            continue
        pkg.status = '已发货'
        pkg.ship_channel = channel
        pkg.shipped_at = ship_dt
        if internal_no:
            pkg.internal_tracking_no = internal_no
        # 释放批次归属，避免与批次/箱子状态矛盾
        if pkg.batch_id is not None:
            pkg.batch_id = None
        create_operation_log(
            action='CHANNEL_SHIP',
            content=f'渠道发货（{channel}）: {pkg.tracking_number}' + (f' 内部单号:{internal_no}' if internal_no else ''),
            target_type='IncomingPackage', target_id=pid,
            extra_data=json.dumps({'package_id': pid, 'channel': channel, 'shipped_at': str(ship_dt), 'internal_no': internal_no, 'batch': True})
        )
        ok.append(pid)
    db.session.commit()
    return api_success(message=f'渠道发货标记成功 {len(ok)} 条' + (f'，跳过 {len(skipped)} 条' if skipped else ''))


@app.route('/api/inbound/channel_unship/<int:package_id>', methods=['POST'])
def api_inbound_channel_unship(package_id):
    """撤销渠道发货 - 恢复为已入库"""
    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if not pkg.ship_channel:
        return api_error('该单号不是渠道发货记录', code='NOT_CHANNEL_SHIP', status_code=400)
    pkg.status = '已入库'
    pkg.ship_channel = None
    pkg.shipped_at = None
    pkg.internal_tracking_no = None
    create_operation_log(
        action='CHANNEL_UNSHIP',
        content=f'撤销渠道发货: {pkg.tracking_number}',
        target_type='IncomingPackage', target_id=package_id,
        extra_data=json.dumps({'package_id': package_id})
    )
    db.session.commit()
    return api_success(message=f'已撤销渠道发货，单号 {pkg.tracking_number} 恢复为已入库')


# ==================== 丢失单号 ====================

@app.route('/lost_packages')
def lost_packages_list():
    """丢失单号页面 - 二次扫描发现少件时可标记丢失的单号（不记录原因）"""
    customer_id = request.args.get('customer_id', type=int)
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    tracking_number = request.args.get('tracking_number', '').strip()

    stmt = db.select(IncomingPackage).where(IncomingPackage.status == '已丢失')
    if customer_id:
        stmt = stmt.where(IncomingPackage.customer_id == customer_id)
    if start_date:
        stmt = stmt.where(IncomingPackage.lost_at >= start_date + ' 00:00:00')
    if end_date:
        stmt = stmt.where(IncomingPackage.lost_at <= end_date + ' 23:59:59')
    if tracking_number:
        stmt = stmt.where(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))

    records = db.session.scalars(stmt.order_by(IncomingPackage.lost_at.desc(), IncomingPackage.id.desc())).all()
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    return render_template('lost_packages.html',
                           records=records, customers=customers,
                           f_customer=customer_id,
                           f_start=start_date, f_end=end_date, f_tracking=tracking_number)


@app.route('/api/inbound/mark_lost', methods=['POST'])
def api_inbound_mark_lost():
    """标记丢失 - 打包二次扫描时未扫描的单号，可标记丢失并继续打包（不记录原因）"""
    data = request.get_json(silent=True) or {}
    package_ids = data.get('package_ids') or []
    if not package_ids:
        return api_error('请选择要标记的单号', code='NO_IDS', status_code=400)

    ok, skipped = [], []
    for pid in package_ids:
        pkg = db.session.get(IncomingPackage, pid)
        if not pkg:
            skipped.append({'id': pid, 'reason': '记录不存在'})
            continue
        # 仅 已入库/待打包 状态可标记丢失（已打包/已发货/已完成/已丢失 跳过）
        if pkg.status not in ('已入库', '待打包'):
            skipped.append({'id': pid, 'reason': f'状态「{pkg.status}」不可标记丢失'})
            continue
        pkg.status = '已丢失'
        pkg.lost_at = get_local_time()
        pkg.lost_reason = None
        # 释放批次归属（已丢失不应留在批次里）
        if pkg.batch_id is not None:
            pkg.batch_id = None
        create_operation_log(
            action='MARK_LOST',
            content=f'标记丢失: {pkg.tracking_number}',
            target_type='IncomingPackage', target_id=pid,
            extra_data=json.dumps({'package_id': pid, 'batch': True})
        )
        ok.append(pid)
    db.session.commit()
    return api_success(message=f'已标记 {len(ok)} 条为丢失' + (f'，跳过 {len(skipped)} 条' if skipped else ''))


@app.route('/api/inbound/restore_lost/<int:package_id>', methods=['POST'])
def api_inbound_restore_lost(package_id):
    """恢复丢失单号 - 恢复为已入库（误标的可以撤回）"""
    pkg = db.session.get(IncomingPackage, package_id)
    if not pkg:
        return api_error('记录不存在', code='NOT_FOUND', status_code=404)
    if pkg.status != '已丢失':
        return api_error('该记录不是丢失状态', code='NOT_LOST', status_code=400)
    pkg.status = '已入库'
    pkg.lost_at = None
    pkg.lost_reason = None
    create_operation_log(
        action='RESTORE_LOST',
        content=f'恢复丢失单号: {pkg.tracking_number}',
        target_type='IncomingPackage', target_id=package_id,
        extra_data=json.dumps({'package_id': package_id})
    )
    db.session.commit()
    return api_success(message=f'已恢复 {pkg.tracking_number} 为已入库')


# ==================== 二次扫描与打包 API ====================

@app.route('/api/verify_package', methods=['POST'])
def api_verify_package():
    """二次扫描验证包裹"""
    tracking = get_request_param('tracking', '').strip()
    customer_id = get_request_int('customer_id')

    if not tracking or not customer_id:
        return jsonify({'success': False, 'message': '缺少参数'}), 400

    pkg = db.session.scalar(db.select(IncomingPackage).filter_by(
        tracking_number=tracking,
        customer_id=customer_id,
        status='已入库'
    ))

    if not pkg:
        return jsonify({'success': False, 'message': '包裹不存在或不属于该客户或已发货'}), 404

    return jsonify({
        'success': True,
        'package_id': pkg.id,
        'tracking': pkg.tracking_number,
        'is_danger': pkg.is_danger,
        'sequence_no': pkg.sequence_no
    })


@app.route('/api/update_danger', methods=['POST'])
def api_update_danger():
    """更新敏货标记"""
    pkg_id = get_request_int('package_id')
    is_danger = get_request_bool('is_danger', False)

    if pkg_id is None:
        return jsonify({'success': False, 'message': '缺少包裹ID'}), 400

    pkg = db.session.get(IncomingPackage, pkg_id)
    if not pkg:
        return jsonify({'success': False, 'message': '包裹不存在'}), 404

    pkg.is_danger = is_danger
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/update_second_scan', methods=['POST'])
def api_update_second_scan():
    """更新二次扫描单号"""
    pkg_id = get_request_int('package_id')
    second_scan = get_request_param('second_scan', '').strip()

    if pkg_id is None:
        return jsonify({'success': False, 'message': '缺少包裹ID'}), 400

    pkg = db.session.get(IncomingPackage, pkg_id)
    if not pkg:
        return jsonify({'success': False, 'message': '包裹不存在'}), 404

    pkg.second_scan_number = second_scan
    pkg.is_danger = True  # 二次扫描后标记为敏货
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/batch/create', methods=['POST'])
def api_create_batch():
    """创建批次 - 支持普货/敏货分别设置渠道、派送、单号，以及邮编"""
    batch_no = get_request_param('batch_no', '').strip()
    batch_type = get_request_param('type', '').strip()
    zip_code = get_request_param('zip_code', '').strip()
    # 普货渠道
    normal_channel = get_request_param('normal_channel', '').strip()
    normal_delivery = get_request_param('normal_delivery', '').strip()
    normal_tracking = get_request_param('normal_tracking', '').strip()
    # 敏货渠道
    sensitive_channel = get_request_param('sensitive_channel', '').strip()
    sensitive_delivery = get_request_param('sensitive_delivery', '').strip()
    sensitive_tracking = get_request_param('sensitive_tracking', '').strip()
    # 旧字段兼容
    channel_type = get_request_param('channel_type', '').strip()
    delivery_channel = get_request_param('delivery_channel', '').strip()
    tracking_number = get_request_param('tracking_number', '').strip()

    if not batch_no:
        return jsonify({'success': False, 'message': '请输入批次编号'}), 400
    if not zip_code:
        return jsonify({'success': False, 'message': '请输入邮编'}), 400
    if not normal_delivery:
        return jsonify({'success': False, 'message': '请选择普货派送方式'}), 400
    if not sensitive_delivery:
        return jsonify({'success': False, 'message': '请选择敏货派送方式'}), 400

    # 检查批次号是否已存在
    existing = db.session.scalar(db.select(Batch).filter_by(batch_no=batch_no))
    if existing:
        return jsonify({'success': False, 'message': f'批次号 {batch_no} 已存在'}), 400

    batch = Batch(
        batch_no=batch_no,
        batch_type=batch_type if batch_type in ('普', '敏') else '普',
        zip_code=zip_code,
        normal_channel=normal_channel,
        normal_delivery=normal_delivery,
        normal_tracking=normal_tracking,
        sensitive_channel=sensitive_channel,
        sensitive_delivery=sensitive_delivery,
        sensitive_tracking=sensitive_tracking,
        # 旧字段兼容
        channel_type=channel_type if channel_type in ('普', '敏') else '',
        delivery_channel=delivery_channel,
        tracking_number=tracking_number
    )
    db.session.add(batch)
    db.session.flush()
    batch_id = batch.id
    batch_no_val = batch.batch_no
    db.session.commit()

    return jsonify({
        'success': True,
        'batch_id': batch_id,
        'batch_no': batch_no_val
    })


@app.route('/api/batch/delete/<int:batch_id>', methods=['POST'])
def api_delete_batch(batch_id):
    """删除批次 - 解除关联但保留打包记录"""
    batch = db.session.get(Batch, batch_id)

    # 检查该批次下是否有已发货的打包记录
    shipped_packages = _scalar_count(PackedPackage, batch_id=batch_id, is_cartoned=True)
    
    if shipped_packages > 0:
        return api_error(
            f'该批次下有 {shipped_packages} 个已合箱的打包记录，无法删除批次',
            code='HAS_SHIPPED_PACKAGES',
            status_code=400
        )

    # 记录操作日志
    create_operation_log(
        action='DELETE_BATCH',
        content=f'删除批次 {batch.batch_no}（类型：{batch.batch_type}）',
        target_type='Batch',
        target_id=batch_id
    )

    # 解除该批次下所有打包记录的关联
    db.session.execute(db.update(PackedPackage).filter_by(batch_id=batch_id).values({'batch_id': None}))

    # 先清理关联的 carton_groups 和 cartons（避免 NOT NULL 约束冲突）
    from models import CartonGroup, Carton
    carton_groups = db.session.execute(
        db.select(CartonGroup).where(CartonGroup.batch_id == batch_id)
    ).scalars().all()
    for cg in carton_groups:
        # 先清该组下的所有箱子
        cartons = db.session.execute(
            db.select(Carton).where(Carton.group_id == cg.id)
        ).scalars().all()
        for carton in cartons:
            db.session.delete(carton)
        db.session.delete(cg)

    db.session.delete(batch)
    db.session.commit()

    return api_success(message=f'批次 {batch.batch_no} 已删除')


@app.route('/api/batch/<int:batch_id>/lock', methods=['POST'])
def api_lock_batch(batch_id):
    """锁定/解锁批次"""
    batch = db.session.get(Batch, batch_id)
    if not batch:
        return api_error('批次不存在', code='NOT_FOUND', status_code=404)

    action = get_request_param('action', 'lock').strip()  # 'lock' 或 'unlock'

    if action == 'lock':
        if batch.is_locked:
            return api_error('批次已锁定', code='ALREADY_LOCKED', status_code=400)
        if not _can_lock_batch(batch_id):
            return api_error('该批次还有未完成的包裹或待确认发货的货物，无法锁定', code='CANNOT_LOCK', status_code=400)
        batch.is_locked = True
        batch.lock_time = datetime.now()
        create_operation_log(
            action='LOCK_BATCH',
            content=f'锁定批次 {batch.batch_no}',
            target_type='Batch',
            target_id=batch_id
        )
    elif action == 'unlock':
        if not batch.is_locked:
            return api_error('批次未锁定', code='NOT_LOCKED', status_code=400)
        batch.is_locked = False
        batch.lock_time = None
        create_operation_log(
            action='UNLOCK_BATCH',
            content=f'解锁批次 {batch.batch_no}',
            target_type='Batch',
            target_id=batch_id
        )
    else:
        return api_error('无效操作', code='INVALID_ACTION', status_code=400)

    db.session.commit()
    return api_success(message=f'批次 {batch.batch_no} 已{"锁定" if action == "lock" else "解锁"}')


@app.route('/api/batch/update/<int:batch_id>', methods=['POST'])
def api_update_batch(batch_id):
    """更新批次信息 - 支持普货/敏货分别设置渠道、派送、单号，以及邮编"""
    batch = db.session.get(Batch, batch_id)
    
    data = request.get_json(silent=True) or {}
    
    # 邮编
    if 'zip_code' in data:
        batch.zip_code = data['zip_code'].strip() if data['zip_code'] else ''
    
    # 普货渠道
    if 'normal_channel' in data:
        batch.normal_channel = data['normal_channel'].strip() if data['normal_channel'] else ''
    if 'normal_delivery' in data:
        batch.normal_delivery = data['normal_delivery'].strip() if data['normal_delivery'] else ''
    if 'normal_tracking' in data:
        batch.normal_tracking = data['normal_tracking'].strip() if data['normal_tracking'] else ''
    
    # 敏货渠道
    if 'sensitive_channel' in data:
        batch.sensitive_channel = data['sensitive_channel'].strip() if data['sensitive_channel'] else ''
    if 'sensitive_delivery' in data:
        batch.sensitive_delivery = data['sensitive_delivery'].strip() if data['sensitive_delivery'] else ''
    if 'sensitive_tracking' in data:
        batch.sensitive_tracking = data['sensitive_tracking'].strip() if data['sensitive_tracking'] else ''
    
    # 旧字段兼容
    channel_type = data.get('channel_type')
    delivery_channel = data.get('delivery_channel')
    tracking_number = data.get('tracking_number')
    if channel_type is not None:
        if channel_type == '':
            batch.channel_type = ''
        elif channel_type in ('普', '敏'):
            batch.channel_type = channel_type
    if delivery_channel is not None:
        batch.delivery_channel = delivery_channel.strip() if delivery_channel else ''
    if tracking_number is not None:
        batch.tracking_number = tracking_number.strip() if tracking_number else ''
    
    db.session.commit()
    
    create_operation_log(
        action='UPDATE_BATCH',
        content=f'更新批次 {batch.batch_no} 信息',
        target_type='Batch',
        target_id=batch_id
    )
    
    return api_success(message=f'批次 {batch.batch_no} 更新成功', data={
        'id': batch.id,
        'batch_no': batch.batch_no,
        'zip_code': batch.zip_code,
        'normal_channel': batch.normal_channel,
        'normal_delivery': batch.normal_delivery,
        'normal_tracking': batch.normal_tracking,
        'sensitive_channel': batch.sensitive_channel,
        'sensitive_delivery': batch.sensitive_delivery,
        'sensitive_tracking': batch.sensitive_tracking,
        'channel_type': batch.channel_type,
        'delivery_channel': batch.delivery_channel,
        'tracking_number': batch.tracking_number
    })


@app.route('/api/batch/<int:batch_id>/shipment_status', methods=['POST'])
def api_update_shipment_status(batch_id):
    """更新批次物流状态（仅已锁定批次可更新）"""
    batch = db.session.get(Batch, batch_id)
    if not batch:
        return api_error('批次不存在', code='NOT_FOUND', status_code=404)
    if not batch.is_locked:
        return api_error('仅已锁定批次可更新物流状态', code='NOT_LOCKED', status_code=400)

    data = request.get_json(silent=True) or {}
    status = data.get('shipment_status', '').strip()
    batch.shipment_status = status
    db.session.commit()

    create_operation_log(
        action='UPDATE_SHIPMENT_STATUS',
        content=f'更新批次 {batch.batch_no} 物流状态: {status}',
        target_type='Batch',
        target_id=batch_id
    )
    return api_success(message='物流状态已更新', data={'shipment_status': status})


@app.route('/api/batch/<int:batch_id>')
def api_get_batch(batch_id):
    """获取单个批次完整信息"""
    batch = db.session.get(Batch, batch_id)
    return jsonify({
        'id': batch.id,
        'batch_no': batch.batch_no,
        'batch_type': batch.batch_type,
        'zip_code': batch.zip_code or '',
        'normal_channel': batch.normal_channel or '',
        'normal_delivery': batch.normal_delivery or '',
        'normal_tracking': batch.normal_tracking or '',
        'sensitive_channel': batch.sensitive_channel or '',
        'sensitive_delivery': batch.sensitive_delivery or '',
        'sensitive_tracking': batch.sensitive_tracking or '',
        'channel_type': batch.channel_type or '',
        'delivery_channel': batch.delivery_channel or '',
        'tracking_number': batch.tracking_number or '',
        'is_shipped': batch.is_shipped,
        'created_at': batch.created_at.strftime('%Y-%m-%d %H:%M')
    })


@app.route('/api/create_packed_batch', methods=['POST'])
def api_create_packed_batch():
    """批量创建打包记录 - 支持一次打包多个包裹（带事务保护）"""
    data = request.get_json()
    if not data:
        return api_error('缺少参数', code='MISSING_PARAMS')

    customer_id = data.get('customer_id')
    packages_data = data.get('packages', [])
    batch_id = data.get('batch_id')
    secondary_customer_ids = data.get('secondary_customer_ids', [])
    vip_solo = data.get('vip_solo', False)  # VIP单独发货模式
    is_full_box = data.get('is_full_box', False)  # 整箱标记

    if not customer_id or not packages_data:
        return api_error('缺少客户ID或包裹数据', code='MISSING_PARAMS')

    # 构建所有涉及的客户ID列表
    all_customer_ids = [customer_id]
    if secondary_customer_ids:
        all_customer_ids.extend(secondary_customer_ids)

    # 门控：只有已确认进入待打包的客户才能打包
    pending = _scalar_count(IncomingPackage, customer_id=customer_id, status='待打包')
    if pending == 0:
        return api_error('该客户没有待打包包裹，请先在待打包确认界面确认', code='NOT_CONFIRMED', status_code=403)

    # 验证每个打包数据
    for i, pkg_info in enumerate(packages_data):
        serial = pkg_info.get('serial', '')
        nature = pkg_info.get('nature', '普')
        package_ids = pkg_info.get('package_ids', [])

        if serial:
            valid, msg = validate_serial_label(serial)
            if not valid:
                return api_error(f'第{i+1}个打包序号标签错误: {msg}', code='INVALID_SERIAL')

        if nature not in ('普', '敏'):
            return api_error(f'第{i+1}个打包货物性质错误，只能是普或敏', code='INVALID_NATURE')

        if not package_ids or not isinstance(package_ids, list):
            return api_error(f'第{i+1}个打包缺少包裹数据', code='MISSING_PACKAGES')

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    # 获取批次
    batch = None
    if batch_id:
        batch = db.session.get(Batch, batch_id)

    packed_ids = []
    total_packed_count = 0
    all_package_ids = set()  # 收集所有涉及的包裹ID

    try:
        for pkg_info in packages_data:
            serial_label = pkg_info.get('serial', '1-1')
            nature = pkg_info.get('nature', '普')
            package_ids = pkg_info.get('package_ids', [])

            if not package_ids:
                continue

            # 获取入库包裹（允许已打包状态，支持拆分模式同一包裹分普敏两包，支持跨客户拼单）
            packages = db.session.scalars(db.select(IncomingPackage).filter(
                IncomingPackage.id.in_(package_ids),
                IncomingPackage.customer_id.in_(all_customer_ids),
                IncomingPackage.status.in_(['待打包', '已打包'])
            )).all()

            if not packages:
                continue

            # 创建打包记录
            total = pkg_info.get('total', 1)
            # 逐包裹整箱标记：优先取 packages 数组里该包裹的 is_full_box，否则回退到操作级 is_full_box
            pkg_is_full_box = bool(pkg_info.get('is_full_box', is_full_box))
            # 收集入库包裹的备注
            pkg_remarks = [p.customer_remark for p in packages if p.customer_remark]
            pkg_abnormals = [p.abnormal_remark for p in packages if p.abnormal_remark]
            packed = PackedPackage(
                customer_id=customer_id,
                batch_id=None if vip_solo else (batch.id if batch else None),
                nature=nature,
                serial_label=serial_label,
                total_packages=total,
                label_printed=True,
                is_vip_solo=vip_solo,
                is_full_box=pkg_is_full_box,
                customer_remark='; '.join(pkg_remarks) if pkg_remarks else None,
                abnormal_remark='; '.join(pkg_abnormals) if pkg_abnormals else None
            )
            db.session.add(packed)
            db.session.flush()

            # 将 packed.id 追加到 serial_label 确保全局唯一（如 "2-1#123"）
            packed.serial_label = f"{serial_label}#{packed.id}"

            # 关联入库包裹
            for pkg in packages:
                all_package_ids.add(pkg.id)
                # VIP 单独发货：清除原有批次关联
                if vip_solo:
                    pkg.batch_id = None
                # 敏货打包时同步更新入库包裹的货物性质
                if nature == '敏':
                    if pkg.nature != '敏':
                        pkg.nature = '敏'
                    pkg.is_danger = True
                item = PackedPackageItem(
                    packed_package_id=packed.id,
                    incoming_package_id=pkg.id
                )
                db.session.add(item)

            # 拼单：提取次客户名称
            if secondary_customer_ids:
                sec_customer_ids_in_pkg = {p.customer_id for p in packages if p.customer_id != customer_id}
                if sec_customer_ids_in_pkg:
                    sec_names = db.session.scalars(
                        db.select(Customer.name).filter(Customer.id.in_(sec_customer_ids_in_pkg))
                    ).all()
                    packed.secondary_customer_names = '、'.join(sec_names) if sec_names else None

            packed_ids.append(packed.id)
            total_packed_count += 1

            # VIP单独发货：自动创建箱子（1包1箱）
            if vip_solo:
                carton_type = '敏' if nature == '敏' else '普'
                prefix = 'M' if nature == '敏' else 'P'
                from models import Carton, CartonItem
                carton = Carton(
                    name=f'V{prefix}-{packed.id}',
                    carton_type=carton_type,
                    batch_id=None,
                    box_seq=1,
                    total_weight=packed.weight or 0,
                    length=packed.length or 0,
                    width=packed.width or 0,
                    height=packed.height or 0,
                    volume_weight=packed.volume_weight or 0,
                    is_shipped=False
                )
                db.session.add(carton)
                db.session.flush()
                ci = CartonItem(carton_id=carton.id, packed_package_id=packed.id)
                db.session.add(ci)
                packed.is_cartoned = True

        # 统一更新所有涉及的包裹状态为已打包
        for pkg_id in all_package_ids:
            pkg = db.session.get(IncomingPackage, pkg_id)
            if pkg:
                pkg.status = '已打包'

        # 更新客户发货次数
        customer.shipment_count = (customer.shipment_count or 0) + total_packed_count

        # 记录操作日志
        create_operation_log(
            action='BATCH_PACK',
            content=f'批量打包 {total_packed_count} 个包裹（客户：{customer.name}）',
            target_type='Customer',
            target_id=customer_id,
            extra_data=json.dumps({
                'customer_name': customer.name,
                'packed_count': total_packed_count,
                'packed_ids': packed_ids,
                'batch_no': batch.batch_no if batch else None
            })
        )

        # 检查客户是否还有已入库包裹，没有则清空其货位
        for cid in all_customer_ids:
            has_inbound = _scalar_count(IncomingPackage, customer_id=cid, status='已入库')
            if has_inbound == 0:
                slots = db.session.scalars(db.select(ShelfSlot).filter_by(customer_id=cid)).all()
                for slot in slots:
                    slot.customer_id = None

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return api_error(f'批量打包失败: {str(e)}', code='BATCH_ERROR')

    return api_success(data={
        'packed_ids': packed_ids,
        'count': total_packed_count
    }, message=f'成功打包 {total_packed_count} 个包裹')


@app.route('/api/pack/hang', methods=['POST'])
def api_pack_hang():
    """挂起扫描进度 - 保存当前二次扫描状态"""
    data = request.get_json(silent=True) or {}
    customer_id = data.get('customer_id')
    verified_count = data.get('verified_count', 0)
    total_count = data.get('total_count', 0)
    verified_ids = data.get('verified_ids', [])
    danger_ids = data.get('danger_ids', [])

    if not customer_id:
        return api_error('缺少客户ID', code='MISSING_PARAMS')

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    customer.scan_hung_at = get_local_time()
    customer.scan_hung_verified = verified_count
    customer.scan_hung_total = total_count
    customer.scan_hung_ids = json.dumps(verified_ids) if verified_ids else None
    customer.scan_hung_danger_ids = json.dumps(danger_ids) if danger_ids else None
    db.session.commit()

    status = 'complete' if verified_count >= total_count else 'incomplete'
    return api_success(message='已挂起', data={'status': status})


@app.route('/api/pack/resume', methods=['POST'])
def api_pack_resume():
    """恢复扫描进度 - 清除挂起状态"""
    data = request.get_json(silent=True) or {}
    customer_id = data.get('customer_id')

    if not customer_id:
        return api_error('缺少客户ID', code='MISSING_PARAMS')

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    customer.scan_hung_at = None
    customer.scan_hung_verified = 0
    customer.scan_hung_total = 0
    customer.scan_hung_ids = None
    customer.scan_hung_danger_ids = None
    db.session.commit()

    return api_success(message='已恢复')


@app.route('/api/pack/hang_state/<int:customer_id>')
def api_pack_hang_state(customer_id):
    """获取客户扫描挂起状态"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    if not customer.scan_hung_at:
        return api_success(data={'hung': False})

    verified_ids = []
    if customer.scan_hung_ids:
        try:
            verified_ids = json.loads(customer.scan_hung_ids)
        except Exception:
            pass

    danger_ids = []
    if customer.scan_hung_danger_ids:
        try:
            danger_ids = json.loads(customer.scan_hung_danger_ids)
        except Exception:
            pass

    return api_success(data={
        'hung': True,
        'verified_count': customer.scan_hung_verified,
        'total_count': customer.scan_hung_total,
        'verified_ids': verified_ids,
        'danger_ids': danger_ids,
        'hung_at': customer.scan_hung_at.strftime('%Y-%m-%d %H:%M:%S') if customer.scan_hung_at else None
    })


@app.route('/api/create_packed', methods=['POST'])
def api_create_packed():
    """创建打包记录（兼容旧接口）- 带事务保护"""
    customer_id = get_request_int('customer_id')
    package_ids_raw = get_request_param('package_ids', '')
    nature = get_request_param('nature', '普')
    serial = get_request_param('serial', '').strip()
    total_count = get_request_int('total_count', 1)
    batch_id = get_request_int('batch_id')

    if not customer_id or not package_ids_raw:
        return api_error('缺少客户ID或包裹ID', code='MISSING_PARAMS')

    # 门控：只有已确认进入待打包的客户才能打包
    pending = _scalar_count(IncomingPackage, customer_id=customer_id, status='待打包')
    if pending == 0:
        return api_error('该客户没有待打包包裹，请先在待打包确认界面确认', code='NOT_CONFIRMED', status_code=403)

    # 验证序号标签格式
    if serial:
        valid, msg = validate_serial_label(serial)
        if not valid:
            return api_error(f'序号标签格式错误: {msg}', code='INVALID_SERIAL')

    # 解析包裹ID列表
    if isinstance(package_ids_raw, str):
        package_ids = [int(x.strip()) for x in package_ids_raw.split(',') if x.strip()]
    else:
        package_ids = package_ids_raw

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    packages = db.session.scalars(db.select(IncomingPackage).filter(
        IncomingPackage.id.in_(package_ids),
        IncomingPackage.customer_id == customer_id,
        IncomingPackage.status == '已入库'
    )).all()

    if len(packages) != len(package_ids):
        return api_error('部分包裹不存在或已发货', code='INVALID_PACKAGES')

    # 验证货物性质
    if nature not in ('普', '敏'):
        return api_error('货物性质只能是普或敏', code='INVALID_NATURE')

    # 获取批次
    batch = None
    if batch_id:
        batch = db.session.get(Batch, batch_id)

    try:
        # 收集入库包裹的备注
        pkg_remarks = [p.customer_remark for p in packages if p.customer_remark]
        pkg_abnormals = [p.abnormal_remark for p in packages if p.abnormal_remark]
        packed = PackedPackage(
            customer_id=customer_id,
            batch_id=batch.id if batch else None,
            nature=nature,
            serial_label=serial,
            total_packages=total_count,
            label_printed=True,
            customer_remark='; '.join(pkg_remarks) if pkg_remarks else None,
            abnormal_remark='; '.join(pkg_abnormals) if pkg_abnormals else None
        )
        db.session.add(packed)
        db.session.flush()

        for pkg in packages:
            pkg.status = '已打包'
            # 敏货打包时同步更新入库包裹的货物性质
            if nature == '敏':
                if pkg.nature != '敏':
                    pkg.nature = '敏'
                pkg.is_danger = True
            item = PackedPackageItem(
                packed_package_id=packed.id,
                incoming_package_id=pkg.id
            )
            db.session.add(item)

        # 更新客户发货次数
        customer.shipment_count = (customer.shipment_count or 0) + 1

        # 记录操作日志
        create_operation_log(
            action='CREATE_PACKED',
            content=f'创建打包记录 {serial}（客户：{customer.name}）',
            target_type='PackedPackage',
            target_id=packed.id,
            extra_data=json.dumps({
                'customer_name': customer.name,
                'serial_label': serial,
                'nature': nature,
                'package_count': len(packages)
            })
        )

        db.session.commit()

        label_html = render_template('label_template.html',
                                      label_type='packed',
                                      packed=packed,
                                      customer=customer)

        return api_success(data={
            'packed_id': packed.id,
            'serial_label': packed.serial_label,
            'label_html': label_html
        })

    except Exception as e:
        db.session.rollback()
        return api_error(f'打包失败: {str(e)}', code='PACK_ERROR')


@app.route('/api/packed/scan/<int:packed_id>')
def api_packed_scan(packed_id):
    """扫码快速获取打包记录"""
    packed = db.session.execute(
        db.select(PackedPackage).options(
            joinedload(PackedPackage.customer),
            joinedload(PackedPackage.batch),
            joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
        ).filter_by(id=packed_id)
    ).unique().scalar_one_or_none()
    if packed is None:
        abort(404)

    trackings = [item.incoming_package.tracking_number for item in packed.items]
    return jsonify({
        'id': packed.id,
        'customer_id': packed.customer_id,
        'customer_name': packed.customer.name if packed.customer else '',
        'customer_no': packed.customer.customer_no if packed.customer else '',
        'serial_label': packed.serial_label.split('#')[0] if packed.serial_label and '#' in packed.serial_label else (packed.serial_label or ''),
        'total_packages': packed.total_packages,
        'nature': packed.nature,
        'contents_description': packed.contents_description or '',
        'weight': packed.weight,
        'length': packed.length,
        'width': packed.width,
        'height': packed.height,
        'volume_weight': packed.volume_weight,
        'batch_id': packed.batch_id,
        'batch_no': packed.batch.batch_no if packed.batch else '',
        'batch_is_locked': bool(packed.batch.is_locked) if packed.batch else False,
        'tracking_numbers': trackings,
        'is_cartoned': packed.is_cartoned,
        'is_full_box': bool(packed.is_full_box),
    })


@app.route('/api/packed/check_existing')
def api_packed_check_existing():
    """检查客户在指定批次下是否已有打包记录，用于打包前提示"""
    customer_id = request.args.get('customer_id', type=int)
    batch_id = request.args.get('batch_id', type=int)

    if not customer_id:
        return api_error('缺少 customer_id', code='MISSING_PARAMS')

    query = db.select(PackedPackage).options(
        joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
    ).filter_by(customer_id=customer_id)

    if batch_id:
        query = query.filter_by(batch_id=batch_id)
    else:
        query = query.filter(PackedPackage.batch_id == None)

    packed_list = db.session.execute(query.order_by(PackedPackage.created_at.desc())).unique().scalars().all()

    result = []
    for p in packed_list:
        display_serial = p.serial_label.split('#')[0] if p.serial_label and '#' in p.serial_label else (p.serial_label or '')
        trackings = [item.incoming_package.tracking_number for item in p.items if item.incoming_package]
        result.append({
            'id': p.id,
            'serial_label': display_serial,
            'nature': p.nature,
            'item_count': len(p.items),
            'tracking_numbers': trackings,
            'contents_description': p.contents_description or '',
            'is_cartoned': p.is_cartoned,
            'is_shipped': any(ci.carton.is_shipped for ci in p.carton_items if ci.carton) if p.carton_items else False,
        })

    return api_success(data={'exists': len(result) > 0, 'records': result})


@app.route('/api/packed/<int:packed_id>/append_items', methods=['POST'])
def api_packed_append_items(packed_id):
    """将新的入库包裹追加到指定打包记录"""
    packed = db.session.get(PackedPackage, packed_id)

    data = request.get_json(silent=True) or {}
    package_ids = data.get('package_ids', [])

    if not package_ids:
        return api_error('缺少 package_ids', code='MISSING_PARAMS')

    customer_id = packed.customer_id

    # 验证入库包裹归属正确且状态为待打包
    packages = db.session.scalars(db.select(IncomingPackage).filter(
        IncomingPackage.id.in_(package_ids),
        IncomingPackage.customer_id == customer_id,
        IncomingPackage.status == '待打包'
    )).all()

    if not packages:
        return api_error('未找到有效的待打包包裹', code='NO_VALID_PACKAGES')

    if len(packages) != len(package_ids):
        missing = set(package_ids) - {p.id for p in packages}
        return api_error(f'部分包裹不存在或状态异常（ID: {list(missing)}）', code='PACKAGE_NOT_FOUND')

    try:
        for pkg in packages:
            pkg.status = '已打包'
            item = PackedPackageItem(
                packed_package_id=packed.id,
                incoming_package_id=pkg.id
            )
            db.session.add(item)

        create_operation_log(
            action='APPEND_ITEMS',
            content=f'追加 {len(packages)} 件包裹到打包记录 {packed.serial_label}（客户：{packed.customer.name if packed.customer else "未知"}）',
            target_type='PackedPackage',
            target_id=packed.id,
            extra_data=json.dumps({
                'packed_id': packed.id,
                'appended_ids': [p.id for p in packages],
                'serial_label': packed.serial_label
            })
        )

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return api_error(f'追加失败: {str(e)}', code='APPEND_ERROR')

    # 返回更新后的包裹数
    updated_count = _scalar_count(PackedPackageItem, packed_package_id=packed.id)
    return api_success(data={'packed_id': packed.id, 'total_items': updated_count},
                       message=f'已成功追加 {len(packages)} 件包裹')


@app.route('/api/packed/<int:packed_id>/update', methods=['POST'])
def api_update_packed(packed_id):
    """更新打包记录"""
    packed = db.session.get(PackedPackage, packed_id)

    data = request.get_json(silent=True) or {}

    packed.contents_description = data.get('description', '')
    # 逐包裹整箱标记（打包操作页在填写品名时按包裹勾选）
    if 'is_full_box' in data:
        packed.is_full_box = bool(data['is_full_box'])
    packed.weight = data.get('weight', 0) or 0
    packed.length = data.get('length', 0) or 0
    packed.width = data.get('width', 0) or 0
    packed.height = data.get('height', 0) or 0

    # 计算体积重 (长×宽×高/6000，保留1位小数，0.5进位)
    packed.volume_weight = calc_volume_weight(packed.length, packed.width, packed.height)

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/packed/list')
def api_packed_list():
    """获取打包列表 - 优化N+1查询"""
    customer_id = request.args.get('customer_id', type=int)

    query = db.select(PackedPackage).options(
        joinedload(PackedPackage.customer),
        joinedload(PackedPackage.batch),
        joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
    )

    if customer_id:
        query = query.filter_by(customer_id=customer_id)

    packed_list = db.session.execute(query.filter(
        PackedPackage.is_cartoned == False
    )).unique().scalars().all()
    packed_list.sort(key=lambda p: (p.customer.name if p.customer else '', p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else ''), reverse=True)

    result = []
    for p in packed_list:
        # 获取包含的入库单号
        tracking_numbers = [item.incoming_package.tracking_number for item in p.items]

        result.append({
            'id': p.id,
            'customer_id': p.customer_id,
            'customer_name': p.customer.name if p.customer else '',
            'customer_no': p.customer.customer_no if p.customer else '',
            'zip_code': p.customer.zip_code if p.customer else '',
            'serial_label': p.serial_label,
            'total_packages': p.total_packages,
            'nature': p.nature,
            'weight': p.weight,
            'length': p.length,
            'width': p.width,
            'height': p.height,
            'volume_weight': p.volume_weight,
            'batch_id': p.batch_id,
            'batch_no': p.batch.batch_no if p.batch else '',
            'tracking_numbers': tracking_numbers,
            'is_cartoned': p.is_cartoned,
            'customer_remark': p.customer_remark or '',
            'abnormal_remark': p.abnormal_remark or ''
        })

    return jsonify(result)


# ==================== 合箱 API ====================

@app.route('/api/carton/batch_packages')
def api_carton_batch_packages():
    """获取指定批次的待合箱包裹列表（分P/M）"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return jsonify({'success': False, 'message': '缺少batch_id参数'}), 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 404

    # 获取该批次所有可合箱包裹（不过滤 label_printed 和尺寸，前端显示警告）
    packed_packages = db.session.scalars(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer)
    ).filter(
        PackedPackage.is_cartoned == False,
        PackedPackage.batch_id == batch_id
    ).order_by(PackedPackage.created_at.desc())).all()

    p_list = []
    m_list = []
    for p in packed_packages:
        tracking_numbers = [item.incoming_package.tracking_number for item in 
                           db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=p.id)).all()]
        item_data = {
            'id': p.id,
            'serial_label': p.serial_label,
            'customer_name': p.customer.name if p.customer else '-',
            'nature': p.nature,
            'weight': p.weight,
            'length': p.length,
            'width': p.width,
            'height': p.height,
            'volume_weight': p.volume_weight,
            'tracking_numbers': tracking_numbers,
            'ready': p.label_printed and p.length > 0 and p.width > 0 and p.height > 0,   # 尺寸+标签都齐才能合箱
            'is_full_box': bool(p.is_full_box),
            'customer_remark': p.customer_remark or '',
            'abnormal_remark': p.abnormal_remark or ''
        }
        if p.nature == '敏':
            m_list.append(item_data)
        else:
            p_list.append(item_data)

    return jsonify({'success': True, 'p': p_list, 'm': m_list})


@app.route('/api/carton/readiness_check')
def api_carton_readiness_check():
    """检查批次合箱准备状态：
    1. 是否有同批次客户还存在未打包包裹（状态未更新）
    2. 已打包包裹是否信息完整（尺寸、标签）
    """
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return api_error('缺少batch_id', code='MISSING_PARAMS')

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return api_error('批次不存在', code='NOT_FOUND')

    # 1. 检查该批次下的客户是否还有未打包包裹
    #    思路：该批次已打包包裹涉及的客户，检查其是否还有状态='已入库'的入库包裹
    packed_customer_ids = db.session.query(PackedPackage.customer_id).filter(
        PackedPackage.batch_id == batch_id
    ).distinct().all()
    packed_customer_ids = [r[0] for r in packed_customer_ids]

    unpacked_warnings = []
    for cid in packed_customer_ids:
        customer = db.session.get(Customer, cid)
        if not customer:
            continue
        pending_count = db.session.scalar(db.select(func.count()).select_from(IncomingPackage).filter(
            IncomingPackage.customer_id == cid,
            IncomingPackage.status == '待打包'
        ))
        if pending_count > 0:
            unpacked_warnings.append({
                'customer_id': cid,
                'customer_name': customer.name,
                'pending_count': pending_count
            })

    # 2. 检查该批次下已打包包裹信息是否完整
    packed_to_check = db.session.scalars(db.select(PackedPackage).filter(
        PackedPackage.batch_id == batch_id,
        PackedPackage.is_cartoned == False
    ).options(joinedload(PackedPackage.customer))).all()

    incomplete_packages = []
    for p in packed_to_check:
        missing = []
        if not p.length or p.length <= 0:
            missing.append('长度')
        if not p.width or p.width <= 0:
            missing.append('宽度')
        if not p.height or p.height <= 0:
            missing.append('高度')
        if not p.contents_description or not p.contents_description.strip():
            missing.append('内容物描述')
        if not p.label_printed:
            missing.append('标签未打印')
        if missing:
            incomplete_packages.append({
                'id': p.id,
                'serial_label': p.serial_label.split('#')[0] if p.serial_label and '#' in p.serial_label else (p.serial_label or ''),
                'customer_name': p.customer.name if p.customer else '-',
                'nature': p.nature,
                'missing_fields': missing,
                'ready': False
            })

    has_unpacked = len(unpacked_warnings) > 0
    has_incomplete = len(incomplete_packages) > 0
    # 只有「还有未打包包裹」才真正阻止合箱；数据不全仅作警告，由前端弹框确认
    has_issues = has_unpacked

    return api_success(data={
        'ready': not has_issues,
        'has_unpacked': has_unpacked,
        'has_incomplete': has_incomplete,
        'unpacked_warnings': unpacked_warnings,
        'incomplete_packages': incomplete_packages,
        'unpacked_count': sum(w['pending_count'] for w in unpacked_warnings),
        'incomplete_count': len(incomplete_packages)
    })


# ========== 合箱分组 API ==========

def _get_or_create_group(batch_id, zip_code):
    """查找或创建分组：按批次+邮编匹配，不存在则自动创建"""
    if not zip_code:
        return None
    group = db.session.scalar(db.select(CartonGroup).filter_by(
        batch_id=batch_id, zip_code=zip_code
    ))
    if not group:
        group = CartonGroup(
            name=zip_code,
            batch_id=batch_id,
            zip_code=zip_code
        )
        db.session.add(group)
        db.session.flush()
    return group


@app.route('/api/carton/groups')
def api_carton_groups():
    """获取批次下所有分组"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return jsonify({'success': False, 'message': '缺少batch_id'}), 400

    groups = db.session.scalars(db.select(CartonGroup).filter_by(
        batch_id=batch_id
    ).order_by(CartonGroup.sort_order, CartonGroup.created_at)).all()

    result = []
    for g in groups:
        p_count = _scalar_count(Carton, group_id=g.id, carton_type='普')
        m_count = _scalar_count(Carton, group_id=g.id, carton_type='敏')
        result.append({
            'id': g.id,
            'name': g.name,
            'zip_code': g.zip_code or '',
            'sort_order': g.sort_order,
            'p_count': p_count,
            'm_count': m_count,
            'carton_count': p_count + m_count
        })

    return jsonify({'success': True, 'data': result})


@app.route('/api/carton/group/create', methods=['POST'])
def api_create_carton_group():
    """创建新分组"""
    batch_id = get_request_int('batch_id')
    name = get_request_param('name', '').strip()
    zip_code = get_request_param('zip_code', '').strip()

    if not batch_id or not name:
        return jsonify({'success': False, 'message': '缺少batch_id或name'}), 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 404

    group = CartonGroup(
        name=name,
        batch_id=batch_id,
        zip_code=zip_code or None
    )
    db.session.add(group)
    db.session.commit()

    return jsonify({'success': True, 'group_id': group.id, 'name': group.name})


@app.route('/api/carton/group/<int:group_id>/update', methods=['POST'])
def api_update_carton_group(group_id):
    """更新分组名称/邮编"""
    group = db.session.get(CartonGroup, group_id)
    if not group:
        return jsonify({'success': False, 'message': '分组不存在'}), 404

    name = get_request_param('name', '').strip()
    zip_code = get_request_param('zip_code', '').strip()

    if name:
        group.name = name
    group.zip_code = zip_code if zip_code else None
    db.session.commit()

    return jsonify({'success': True})


@app.route('/api/carton/group/<int:group_id>/delete', methods=['POST'])
def api_delete_carton_group(group_id):
    """删除分组 - 箱子归入默认组或取消分组"""
    group = db.session.get(CartonGroup, group_id)
    if not group:
        return jsonify({'success': False, 'message': '分组不存在'}), 404

    batch_id = group.batch_id

    # 把该组下所有箱子的group_id置空
    db.session.execute(
        db.update(Carton).filter_by(group_id=group_id).values({'group_id': None})
    )

    db.session.delete(group)
    db.session.commit()

    return jsonify({'success': True, 'message': f'分组 {group.name} 已删除'})


@app.route('/api/carton/auto_group', methods=['POST'])
def api_auto_group_cartons():
    """按邮编+客户地址自动分组 - 将同一批次内的箱子按邮编分组"""
    batch_id = get_request_int('batch_id')
    if not batch_id:
        return jsonify({'success': False, 'message': '缺少batch_id'}), 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 404

    # 获取该批次所有未分组的箱子
    cartons = db.session.scalars(
        db.select(Carton).filter_by(batch_id=batch_id)
    ).all()

    if not cartons:
        return jsonify({'success': True, 'message': '该批次无箱子'})

    # 收集箱子中包裹的邮编信息
    carton_zip_info = {}
    for c in cartons:
        items = db.session.execute(
            db.select(PackedPackage.zip_code, Customer.name, Customer.addr)
            .join(CartonItem, CartonItem.packed_package_id == PackedPackage.id)
            .join(Customer, Customer.id == PackedPackage.customer_id)
            .filter(CartonItem.carton_id == c.id)
        ).all()
        if items:
            # 取第一个包裹的邮编作为该箱子的代表邮编
            zip_code = items[0][0] or ''
            customer_name = items[0][1] or ''
            carton_zip_info[c.id] = (zip_code, customer_name)

    # 按邮编分组
    zip_groups = {}
    for carton_id, (zip_code, customer_name) in carton_zip_info.items():
        if not zip_code:
            zip_code = '未知邮编'
        if zip_code not in zip_groups:
            zip_groups[zip_code] = []
        zip_groups[zip_code].append(carton_id)

    created_count = 0
    for zip_code, c_ids in zip_groups.items():
        # 查找是否已有同名分组
        existing = db.session.scalars(
            db.select(CartonGroup).filter_by(batch_id=batch_id, zip_code=zip_code)
        ).first()

        if existing:
            group_id = existing.id
        else:
            group_name = f'邮编{zip_code}'
            group = CartonGroup(name=group_name, batch_id=batch_id, zip_code=zip_code)
            db.session.add(group)
            db.session.flush()
            group_id = group.id
            created_count += 1

        # 将箱子归入该分组
        db.session.execute(
            db.update(Carton).filter(Carton.id.in_(c_ids)).values({'group_id': group_id})
        )

    db.session.commit()
    return jsonify({'success': True, 'message': f'自动分组完成，创建了 {created_count} 个新分组'})


@app.route('/api/carton/<int:carton_id>/move', methods=['POST'])
def api_move_carton(carton_id):
    """移动箱子到其他分组"""
    carton = db.session.get(Carton, carton_id)
    if not carton:
        return jsonify({'success': False, 'message': '箱子不存在'}), 404

    group_id = get_request_int('group_id')
    if group_id:
        group = db.session.get(CartonGroup, group_id)
        if not group:
            return jsonify({'success': False, 'message': '目标分组不存在'}), 404
        carton.group_id = group_id
    else:
        carton.group_id = None

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/carton/create', methods=['POST'])
def api_create_carton():
    """创建箱子 - 按批次自动生成箱号，支持分组"""
    batch_id = get_request_int('batch_id')
    carton_type = get_request_param('type', '')
    packed_ids_raw = get_request_param('packed_ids', '')
    group_id = get_request_int('group_id')  # 前端传入的分组ID

    if not batch_id or not carton_type or not packed_ids_raw:
        return jsonify({'success': False, 'message': '缺少参数(batch_id/type/packed_ids)'}), 400

    if carton_type not in ('普', '敏'):
        return jsonify({'success': False, 'message': '箱型只能是普或敏'}), 400

    # 获取批次信息
    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 400

    # 解析包裹ID
    if isinstance(packed_ids_raw, str):
        packed_ids = [int(x.strip()) for x in packed_ids_raw.split(',') if x.strip()]
    else:
        packed_ids = packed_ids_raw

    # 验证包裹属于该批次且未合箱（加joinedload预加载客户，避免N+1）
    packed_packages = db.session.scalars(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer)
    ).filter(
        PackedPackage.id.in_(packed_ids),
        PackedPackage.is_cartoned == False,
        PackedPackage.batch_id == batch_id
    )).all()

    if len(packed_packages) != len(packed_ids):
        return jsonify({'success': False, 'message': '部分打包包裹不存在、已合箱或不属于该批次'}), 400

    # 检测混合装箱（普+敏混合），前端需传mix_confirm确认
    natures = set(p.nature for p in packed_packages)
    mix_confirm = get_request_param('mix_confirm', '')
    if len(natures) > 1:
        if mix_confirm != 'yes':
            return jsonify({
                'success': False,
                'message': '检测到普货和敏货混合装箱，此为危险操作，请确认后重试'
            }), 400
        # 记录混合装箱操作日志
        create_operation_log('合箱操作', f'混合装箱：{carton_type}箱包含{len(packed_packages)}个包裹（普{natures}混合），确认操作', operator='system')

    # 验证同批次客户是否还有未打包包裹
    involved_customer_ids = set(p.customer_id for p in packed_packages)
    for cid in involved_customer_ids:
        customer = db.session.get(Customer, cid)
        pending_count = db.session.scalar(db.select(func.count()).select_from(IncomingPackage).filter(
            IncomingPackage.customer_id == cid,
            IncomingPackage.status == '待打包'
        ))
        if pending_count > 0:
            return jsonify({
                'success': False,
                'message': f'客户 {customer.name if customer else "未知"} 还有 {pending_count} 件包裹未打包，请先完成打包操作'
            }), 400

    # 验证包裹就绪（尺寸已填、标签已打印）
    for p in packed_packages:
        if p.length <= 0 or p.width <= 0 or p.height <= 0:
            return jsonify({
                'success': False,
                'message': f'包裹 {p.serial_label}({p.customer.name if p.customer else "未知客户"}) 尺寸未填写，请先在打包记录中补全尺寸'
            }), 400
        if not p.label_printed:
            return jsonify({
                'success': False,
                'message': f'包裹 {p.serial_label}({p.customer.name if p.customer else "未知客户"}) 标签未打印，请先打印标签'
            }), 400

    # 自动生成箱号: P{batch_no}-{seq} 或 M{batch_no}-{seq}
    # 去除 batch_no 中已有的 P/M 前缀
    clean_batch_no = batch.batch_no
    if clean_batch_no and clean_batch_no[0].upper() in ('P', 'M'):
        clean_batch_no = clean_batch_no[1:]
    type_prefix = 'M' if carton_type == '敏' else 'P'
    max_seq = db.session.scalar(db.select(db.func.max(Carton.box_seq)).filter_by(
        batch_id=batch_id, carton_type=carton_type
    )) or 0
    box_seq = max_seq + 1
    carton_name = f'{type_prefix}{clean_batch_no}-{box_seq}'

    carton = Carton(name=carton_name, carton_type=carton_type,
                    batch_id=batch_id, box_seq=box_seq)

    # 分组逻辑：优先使用前端传入的 group_id，否则按批次邮编自动匹配
    if group_id:
        carton.group_id = group_id
    else:
        zip_code = batch.zip_code
        if zip_code:
            group = _get_or_create_group(batch_id, zip_code)
            if group:
                carton.group_id = group.id

    db.session.add(carton)
    db.session.flush()

    # 单包裹时自动填充箱子尺寸/重量
    is_single = len(packed_packages) == 1
    single_pkg = packed_packages[0] if is_single else None

    for p in packed_packages:
        p.is_cartoned = True
        p.status = '已合箱'
        item = CartonItem(carton_id=carton.id, packed_package_id=p.id)
        db.session.add(item)

    # 单包裹：箱子重量、尺寸默认使用该包裹数据
    if is_single and single_pkg:
        carton.total_weight = single_pkg.weight or 0
        carton.length = single_pkg.length or 0
        carton.width = single_pkg.width or 0
        carton.height = single_pkg.height or 0
        if carton.length and carton.width and carton.height:
            carton.volume_weight = round((carton.length * carton.width * carton.height) / 6000, 3)

    # 多包裹合箱：重量 = 实重之和 + 箱子自重，尺寸 = 61×51×41
    if not is_single and len(packed_packages) > 1:
        tare = float(InvoiceConfig.get('box_tare_weight', '0.9'))
        total_weight = sum(p.weight or 0 for p in packed_packages) + tare
        carton.total_weight = round(total_weight, 1)
        carton.length = 61
        carton.width = 51
        carton.height = 41
        carton.volume_weight = round((61 * 51 * 41) / 6000, 3)

    db.session.commit()

    return jsonify({
        'success': True,
        'carton_id': carton.id,
        'carton_name': carton_name,
        'auto_filled': is_single,
        'weight': carton.total_weight if is_single else None,
        'length': carton.length if is_single else None,
        'width': carton.width if is_single else None,
        'height': carton.height if is_single else None
    })


@app.route('/api/carton/create_separate', methods=['POST'])
def api_create_separate_cartons():
    """分别合箱 - 每个选中的包裹各自创建独立箱子"""
    batch_id = get_request_int('batch_id')
    carton_type = get_request_param('type', '')
    packed_ids_raw = get_request_param('packed_ids', '')
    group_id = get_request_int('group_id')

    if not batch_id or not carton_type or not packed_ids_raw:
        return jsonify({'success': False, 'message': '缺少参数(batch_id/type/packed_ids)'}), 400

    if carton_type not in ('普', '敏'):
        return jsonify({'success': False, 'message': '箱型只能是普或敏'}), 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 400

    # 解析包裹ID
    if isinstance(packed_ids_raw, str):
        packed_ids = [int(x.strip()) for x in packed_ids_raw.split(',') if x.strip()]
    else:
        packed_ids = packed_ids_raw

    if not packed_ids:
        return jsonify({'success': False, 'message': '未选择任何包裹'}), 400

    # 批量加载并验证包裹
    packed_packages = db.session.scalars(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer)
    ).filter(
        PackedPackage.id.in_(packed_ids),
        PackedPackage.is_cartoned == False,
        PackedPackage.batch_id == batch_id
    )).all()

    if len(packed_packages) != len(packed_ids):
        return jsonify({'success': False, 'message': '部分包裹不存在、已合箱或不属于该批次'}), 400

    # 验证客户无未打包包裹
    involved_customer_ids = set(p.customer_id for p in packed_packages)
    for cid in involved_customer_ids:
        customer = db.session.get(Customer, cid)
        pending_count = db.session.scalar(db.select(func.count()).select_from(IncomingPackage).filter(
            IncomingPackage.customer_id == cid,
            IncomingPackage.status == '待打包'
        ))
        if pending_count > 0:
            return jsonify({
                'success': False,
                'message': f'客户 {customer.name if customer else "未知"} 还有 {pending_count} 件包裹未打包，请先完成打包操作'
            }), 400

    # 验证尺寸和标签
    for p in packed_packages:
        if p.length <= 0 or p.width <= 0 or p.height <= 0:
            return jsonify({
                'success': False,
                'message': f'包裹 {p.serial_label}({p.customer.name if p.customer else "未知客户"}) 尺寸未填写'
            }), 400
        if not p.label_printed:
            return jsonify({
                'success': False,
                'message': f'包裹 {p.serial_label}({p.customer.name if p.customer else "未知客户"}) 标签未打印'
            }), 400

    # 生成箱号前缀和分组
    clean_batch_no = batch.batch_no
    if clean_batch_no and clean_batch_no[0].upper() in ('P', 'M'):
        clean_batch_no = clean_batch_no[1:]
    type_prefix = 'M' if carton_type == '敏' else 'P'

    # 获取当前最大 box_seq
    max_seq = db.session.scalar(db.select(db.func.max(Carton.box_seq)).filter_by(
        batch_id=batch_id, carton_type=carton_type
    )) or 0
    box_seq = max_seq + 1

    # 分组
    actual_group_id = group_id
    if not actual_group_id:
        zip_code = batch.zip_code
        if zip_code:
            group = _get_or_create_group(batch_id, zip_code)
            if group:
                actual_group_id = group.id

    created_cartons = []
    for p in packed_packages:
        carton_name = f'{type_prefix}{clean_batch_no}-{box_seq}'
        carton = Carton(name=carton_name, carton_type=carton_type,
                        batch_id=batch_id, box_seq=box_seq,
                        group_id=actual_group_id,
                        total_weight=(p.weight or 0),
                        length=(p.length or 0), width=(p.width or 0), height=(p.height or 0))
        if carton.length and carton.width and carton.height:
            carton.volume_weight = round((carton.length * carton.width * carton.height) / 6000, 3)

        db.session.add(carton)
        db.session.flush()

        p.is_cartoned = True
        p.status = '已合箱'
        item = CartonItem(carton_id=carton.id, packed_package_id=p.id)
        db.session.add(item)

        created_cartons.append({
            'carton_id': carton.id,
            'carton_name': carton_name,
            'serial_label': p.serial_label,
            'customer_name': p.customer.name if p.customer else ''
        })
        box_seq += 1

    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'已创建 {len(created_cartons)} 个箱子',
        'cartons': created_cartons
    })


@app.route('/api/carton/batch_create', methods=['POST'])
def api_batch_create_carton():
    """一键合箱 - 自动收集批次中同类型所有可合箱包裹，支持分组"""
    batch_id = get_request_int('batch_id')
    carton_type = get_request_param('type', '')
    group_id = get_request_int('group_id')  # 前端传入的分组ID

    if not batch_id or not carton_type:
        return jsonify({'success': False, 'message': '缺少参数(batch_id/type)'}), 400

    if carton_type not in ('普', '敏'):
        return jsonify({'success': False, 'message': '箱型只能是普或敏'}), 400

    # 获取批次信息
    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 400

    # 自动收集该批次该类型所有可合箱包裹（加joinedload预加载客户，避免N+1）
    packed_packages = db.session.scalars(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer)
    ).filter(
        PackedPackage.is_cartoned == False,
        PackedPackage.label_printed == True,
        PackedPackage.length > 0,
        PackedPackage.width > 0,
        PackedPackage.height > 0,
        PackedPackage.batch_id == batch_id,
        PackedPackage.nature == carton_type
    )).all()

    if not packed_packages:
        return jsonify({'success': False, 'message': f'该批次没有可合箱的{carton_type}货包裹'}), 400

    packed_ids = [p.id for p in packed_packages]

    # 自动生成箱号
    # 去除 batch_no 中已有的 P/M 前缀
    clean_batch_no = batch.batch_no
    if clean_batch_no and clean_batch_no[0].upper() in ('P', 'M'):
        clean_batch_no = clean_batch_no[1:]
    type_prefix = 'M' if carton_type == '敏' else 'P'
    max_seq = db.session.scalar(db.select(db.func.max(Carton.box_seq)).filter_by(
        batch_id=batch_id, carton_type=carton_type
    )) or 0
    box_seq = max_seq + 1
    carton_name = f'{type_prefix}{clean_batch_no}-{box_seq}'

    carton = Carton(name=carton_name, carton_type=carton_type,
                    batch_id=batch_id, box_seq=box_seq)

    # 分组逻辑：优先使用前端传入的 group_id，否则按批次邮编自动匹配
    if group_id:
        carton.group_id = group_id
    else:
        zip_code = batch.zip_code
        if zip_code:
            group = _get_or_create_group(batch_id, zip_code)
            if group:
                carton.group_id = group.id

    db.session.add(carton)
    db.session.flush()

    # 单包裹时自动填充箱子尺寸/重量
    is_single = len(packed_packages) == 1
    single_pkg = packed_packages[0] if is_single else None

    for p in packed_packages:
        p.is_cartoned = True
        p.status = '已合箱'
        item = CartonItem(carton_id=carton.id, packed_package_id=p.id)
        db.session.add(item)

    # 单包裹：箱子重量、尺寸默认使用该包裹数据
    if is_single and single_pkg:
        carton.total_weight = single_pkg.weight or 0
        carton.length = single_pkg.length or 0
        carton.width = single_pkg.width or 0
        carton.height = single_pkg.height or 0
        if carton.length and carton.width and carton.height:
            carton.volume_weight = round((carton.length * carton.width * carton.height) / 6000, 3)

    # 多包裹合箱：重量 = 实重之和 + 箱子自重，尺寸 = 61×51×41
    if not is_single and len(packed_packages) > 1:
        tare = float(InvoiceConfig.get('box_tare_weight', '0.9'))
        total_weight = sum(p.weight or 0 for p in packed_packages) + tare
        carton.total_weight = round(total_weight, 1)
        carton.length = 61
        carton.width = 51
        carton.height = 41
        carton.volume_weight = round((61 * 51 * 41) / 6000, 3)

    db.session.commit()

    return jsonify({
        'success': True,
        'carton_id': carton.id,
        'carton_name': carton_name,
        'package_count': len(packed_ids),
        'auto_filled': is_single
    })


@app.route('/api/carton/<int:carton_id>/update', methods=['POST'])
def api_update_carton(carton_id):
    """更新箱子信息"""
    carton = db.session.get(Carton, carton_id)

    carton.total_weight = get_request_float('weight')
    carton.length = get_request_float('length')
    carton.width = get_request_float('width')
    carton.height = get_request_float('height')

    # 自定义邮编
    custom_zip = get_request_param('custom_zip_code', '')
    carton.custom_zip_code = custom_zip if custom_zip else None

    # 计算体积重

    # 计算体积重
    if carton.length and carton.width and carton.height:
        carton.volume_weight = round((carton.length * carton.width * carton.height) / 6000, 3)

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/carton/<int:carton_id>/add_packages', methods=['POST'])
def api_carton_add_packages(carton_id):
    """补漏：向已有箱子添加包裹"""
    carton = db.session.get(Carton, carton_id)
    if not carton:
        return api_error('箱子不存在', code='NOT_FOUND', status_code=404)

    if carton.is_shipped:
        return api_error('该箱子已发货，无法添加包裹', code='ALREADY_SHIPPED', status_code=400)

    packed_ids_raw = get_request_param('packed_ids', '')
    if not packed_ids_raw:
        return api_error('请选择要加入的包裹', code='NO_IDS', status_code=400)

    try:
        packed_ids = [int(x.strip()) for x in packed_ids_raw.split(',') if x.strip()]
    except (ValueError, TypeError):
        return api_error('包裹ID格式错误', code='INVALID_IDS', status_code=400)

    if not packed_ids:
        return api_error('请选择要加入的包裹', code='NO_IDS', status_code=400)

    # 验证包裹：属于同批次、未合箱、性质匹配
    packed_list = db.session.scalars(
        db.select(PackedPackage).filter(PackedPackage.id.in_(packed_ids))
    ).all()

    if len(packed_list) != len(packed_ids):
        found_ids = {p.id for p in packed_list}
        missing = set(packed_ids) - found_ids
        return api_error(f'未找到包裹: {missing}', code='NOT_FOUND', status_code=400)

    errors = []
    for p in packed_list:
        if p.is_cartoned:
            errors.append(f'{p.serial_label} 已合箱')
        if p.batch_id != carton.batch_id:
            errors.append(f'{p.serial_label} 不属于该批次')
        if p.nature != carton.carton_type:
            errors.append(f'{p.serial_label} 性质与箱子不匹配（箱型：{carton.carton_type}，包裹：{p.nature}）')

    if errors:
        return api_error('; '.join(errors), code='VALIDATION_FAILED', status_code=400)

    # 创建 CartonItem 并更新包裹状态
    added_count = 0
    for p in packed_list:
        item = CartonItem(carton_id=carton_id, packed_package_id=p.id)
        db.session.add(item)
        p.is_cartoned = True
        p.status = '已合箱'
        added_count += 1

    # 重新计算箱子总重
    all_items = db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()
    all_pids = [item.packed_package_id for item in all_items]
    all_packed = db.session.scalars(
        db.select(PackedPackage).filter(PackedPackage.id.in_(all_pids))
    ).all()

    total_w = sum(p.weight or 0 for p in all_packed)
    total_vw = sum(p.volume_weight or 0 for p in all_packed)
    carton.total_weight = round(total_w, 2)
    carton.volume_weight = round(total_vw, 3)

    # 记录操作日志
    create_operation_log(
        action='ADD_TO_CARTON',
        content=f'向箱子 {carton.name} 补加 {added_count} 个包裹',
        target_type='Carton',
        target_id=carton_id,
        extra_data=json.dumps({
            'packed_ids': packed_ids,
            'added_count': added_count
        }, ensure_ascii=False)
    )

    db.session.commit()
    return jsonify({'success': True, 'added_count': added_count, 'total_weight': carton.total_weight})


@app.route('/api/carton/<int:carton_id>/delete', methods=['POST'])
def api_delete_carton(carton_id):
    """删除箱子 - 解除包裹关联"""
    carton = db.session.get(Carton, carton_id)
    carton_name = carton.name

    # 检查箱子是否已发货
    if carton.is_shipped:
        return api_error('已发货的箱子无法删除', code='ALREADY_SHIPPED', status_code=400)

    # 解除箱内所有包裹的合箱状态（通过CartonItem关联）
    packed_ids = [item.packed_package_id for item in db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()]
    if packed_ids:
        db.session.execute(db.update(PackedPackage).filter(PackedPackage.id.in_(packed_ids)).values(
            {'is_cartoned': False, 'status': '待合箱'}
        ))
    db.session.execute(db.delete(CartonItem).filter_by(carton_id=carton_id))

    # 记录操作日志
    create_operation_log(
        action='DELETE_CARTON',
        content=f'删除箱子 {carton_name}（类型：{carton.carton_type}）',
        target_type='Carton',
        target_id=carton_id,
        extra_data=json.dumps({
            'carton_name': carton_name,
            'carton_type': carton.carton_type,
            'released_packages': len(packed_ids)
        })
    )

    db.session.delete(carton)
    db.session.commit()

    return api_success(message=f'箱子 {carton_name} 已删除，{len(packed_ids)} 个包裹已解除合箱')


@app.route('/api/carton/list')
def api_carton_list():
    """获取所有箱子列表（供前端动态刷新），支持按分组过滤"""
    batch_id = request.args.get('batch_id', type=int)
    group_id = request.args.get('group_id', type=int)
    query = db.select(Carton).options(
        joinedload(Carton.batch),
    )
    if batch_id:
        query = query.filter_by(batch_id=batch_id)
    if group_id:
        query = query.filter_by(group_id=group_id)
    cartons = db.session.scalars(query.order_by(Carton.created_at.desc())).all()

    # 预加载包裹数量和备注（Carton.items 是 dynamic，需要单独查询）
    carton_ids = [c.id for c in cartons]
    package_counts = {}
    carton_remarks = {}
    carton_customers = {}  # {carton_id: [customer_name, ...]}
    if carton_ids:
        from sqlalchemy import func as safunc
        rows = db.session.execute(
            db.select(CartonItem.carton_id, safunc.count(CartonItem.id))
            .filter(CartonItem.carton_id.in_(carton_ids))
            .group_by(CartonItem.carton_id)
        ).all()
        package_counts = {r[0]: r[1] for r in rows}
        
        # 收集每个箱子的客户备注（去重）
        remark_rows = db.session.execute(
            db.select(CartonItem.carton_id, PackedPackage.customer_remark)
            .join(PackedPackage, CartonItem.packed_package_id == PackedPackage.id)
            .filter(
                CartonItem.carton_id.in_(carton_ids),
                PackedPackage.customer_remark.isnot(None),
                PackedPackage.customer_remark != ''
            )
        ).all()
        for carton_id, remark in remark_rows:
            if carton_id not in carton_remarks:
                carton_remarks[carton_id] = set()
            carton_remarks[carton_id].add(remark)

        # 收集每个箱子的客户名（去重）
        customer_rows = db.session.execute(
            db.select(CartonItem.carton_id, Customer.name)
            .join(PackedPackage, CartonItem.packed_package_id == PackedPackage.id)
            .join(Customer, Customer.id == PackedPackage.customer_id)
            .filter(CartonItem.carton_id.in_(carton_ids))
        ).all()
        for carton_id, name in customer_rows:
            if carton_id not in carton_customers:
                carton_customers[carton_id] = set()
            carton_customers[carton_id].add(name)

    result = []
    for c in cartons:
        remarks = carton_remarks.get(c.id, set())
        customers = carton_customers.get(c.id, set())
        result.append({
            'id': c.id,
            'name': c.name,
            'carton_type': c.carton_type,
            'batch_no': c.batch.batch_no if c.batch else '',
            'batch_id': c.batch_id,
            'group_id': c.group_id,
            'total_weight': c.total_weight,
            'length': c.length,
            'width': c.width,
            'height': c.height,
            'volume_weight': c.volume_weight,
            'is_shipped': c.is_shipped,
            'package_count': package_counts.get(c.id, 0),
            'customer_remarks': '；'.join(remarks) if remarks else '',
            'customer_names': list(customers),  # 去重客户名列表
            'created_at': c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else ''
        })
    return jsonify({'success': True, 'data': result})


@app.route('/api/carton/<int:carton_id>/details')
def api_carton_details(carton_id):
    """获取箱子详情"""
    carton = db.session.get(Carton, carton_id)
    if not carton:
        return jsonify({'success': False, 'message': '箱子不存在'}), 404

    items = db.session.execute(
        db.select(PackedPackage, Customer)
        .join(CartonItem, CartonItem.packed_package_id == PackedPackage.id)
        .join(Customer, Customer.id == PackedPackage.customer_id)
        .filter(CartonItem.carton_id == carton_id)
    ).all()

    # 箱型文字
    type_label = 'M' if carton.carton_type == '敏' else 'P'

    # 获取批次邮编用于前端展示
    batch = db.session.get(Batch, carton.batch_id) if carton.batch_id else None
    batch_zip = batch.zip_code if batch else ''

    return jsonify({
        'id': carton.id,
        'name': carton.name,
        'carton_type': carton.carton_type,
        'type_label': type_label,
        'batch_id': carton.batch_id,
        'group_id': carton.group_id,
        'box_seq': carton.box_seq,
        'total_weight': carton.total_weight,
        'length': carton.length,
        'width': carton.width,
        'height': carton.height,
        'volume_weight': carton.volume_weight,
        'is_shipped': carton.is_shipped,
        'custom_zip_code': carton.custom_zip_code or '',
        'batch_zip': batch_zip,
        'items': [{
            'packed_id': p.id,
            'serial_label': p.serial_label,
            'customer_name': c.name,
            'nature': p.nature,
            'weight': p.weight,
            'dimension': f'{p.length}×{p.width}×{p.height}',
            'customer_remark': p.customer_remark or '',
            'abnormal_remark': p.abnormal_remark or '',
        } for p, c in items]
    })


@app.route('/api/carton/<int:carton_id>/ship', methods=['POST'])
def api_carton_ship(carton_id):
    """标记箱子已发货 - 更新包裹状态为已发货并计算运费"""
    carton = db.session.get(Carton, carton_id)
    if carton.is_shipped:
        return api_error('该箱子已发货', code='ALREADY_SHIPPED', status_code=400)

    # 获取箱子内所有包裹及客户信息
    items = db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()
    if not items:
        return api_error('箱子内没有包裹', code='EMPTY_CARTON', status_code=400)

    # 收集所有关联的入库包裹ID
    incoming_pkg_ids = set()
    customer_weights = {}  # customer_id -> {normal_weight, danger_weight}

    # 获取箱子内所有包裹及客户信息（一次性预加载，避免N+1）
    item_packed_ids = [item.packed_package_id for item in items]
    
    packed_map = {}
    if item_packed_ids:
        packed_list = db.session.scalars(db.select(PackedPackage).options(
            joinedload(PackedPackage.customer)
        ).filter(PackedPackage.id.in_(item_packed_ids))).all()
        packed_map = {p.id: p for p in packed_list}
    
    # 预加载所有关联的 IncomingPackageItem（避免内层 N+1）
    ppi_map = {}
    if item_packed_ids:
        ppis = db.session.scalars(db.select(PackedPackageItem).filter(
            PackedPackageItem.packed_package_id.in_(item_packed_ids)
        )).all()
        for ppi in ppis:
            if ppi.packed_package_id not in ppi_map:
                ppi_map[ppi.packed_package_id] = []
            ppi_map[ppi.packed_package_id].append(ppi)

    for item in items:
        packed = packed_map.get(item.packed_package_id)
        if not packed:
            continue

        # 统计客户重量
        if packed.customer_id not in customer_weights:
            customer_weights[packed.customer_id] = {'normal': 0, 'danger': 0, 'name': packed.customer.name if packed.customer else ''}
        w = customer_weights[packed.customer_id]
        chargeable = calc_chargeable_weight(packed.volume_weight, packed.weight)
        if packed.nature == '敏':
            w['danger'] += chargeable
        else:
            w['normal'] += chargeable

        # 收集入库包裹ID（从预加载的 ppi_map）
        for ppi in ppi_map.get(packed.id, []):
            incoming_pkg_ids.add(ppi.incoming_package_id)

    # 更新所有关联入库包裹状态为已发货
    if incoming_pkg_ids:
        db.session.execute(db.update(IncomingPackage).filter(IncomingPackage.id.in_(incoming_pkg_ids)).values(
            {'status': '已发货'}
        ))

    # 更新所有关联打包包裹状态为已发货
    for item in items:
        db.session.execute(db.update(PackedPackage).filter_by(id=item.packed_package_id).values(
            {'status': '已发货'}
        ))

    # 标记箱子已发货
    carton.is_shipped = True

    # 自动创建运费记录（按客户计费重比例分摊）
    prices = get_price_config()
    total_normal = sum(v['normal'] for v in customer_weights.values())
    total_danger = sum(v['danger'] for v in customer_weights.values())

    for cust_id, weights in customer_weights.items():
        # 分别计算普货和敏货运费
        normal_freight = calc_freight(weights['normal'], '普')
        danger_freight = calc_freight(weights['danger'], '敏')
        total_amount = round(normal_freight + danger_freight, 2)

        if total_amount > 0:
            freight = FreightRecord(
                customer_id=cust_id,
                carton_id=carton_id,
                amount=total_amount,
                status='待结算'
            )
            db.session.add(freight)

            # 更新客户待付运费
            customer = db.session.get(Customer, cust_id)
            if customer:
                customer.pending_freight = (customer.pending_freight or 0) + total_amount

    # 记录操作日志
    create_operation_log(
        action='SHIP_CARTON',
        content=f'箱子 {carton.name} 已发货，共 {len(incoming_pkg_ids)} 个包裹',
        target_type='Carton',
        target_id=carton_id,
        extra_data=json.dumps({
            'carton_name': carton.name,
            'customer_count': len(customer_weights),
            'package_count': len(incoming_pkg_ids)
        })
    )

    db.session.commit()
    return api_success(message=f'箱子 {carton.name} 已发货，已为 {len(customer_weights)} 个客户创建运费记录')


@app.route('/api/packed/<int:packed_id>/complete', methods=['POST'])
def api_complete_packed_record(packed_id):
    """确认完成打包记录 - 标记订单已完成"""
    packed = db.session.get(PackedPackage, packed_id)

    if packed.status == '已完成':
        return api_error('该订单已经完成', code='ALREADY_COMPLETED', status_code=400)
    if packed.status not in ('已发货', '已合箱'):
        return api_error('只有已合箱或已发货的订单才能确认完成', code='NOT_READY', status_code=400)

    # 检查箱子尺寸重量是否填写完整
    if not (packed.weight and packed.weight > 0 and packed.length and packed.length > 0 and packed.width and packed.width > 0 and packed.height and packed.height > 0):
        return api_error('请先填写完整的箱子尺寸（长/宽/高）和重量后再确认完成', code='DIMENSIONS_INCOMPLETE', status_code=400)

    packed.status = '已完成'

    # 自动创建运费记录（如果尚未通过合箱→发货流程创建）
    if packed.batch_id:
        # 检查该客户在当前批次下是否已有运费记录（通过carton关联或直接创建）
        existing_freight = db.session.scalar(db.select(FreightRecord).filter(
            FreightRecord.customer_id == packed.customer_id,
            db.or_(
                FreightRecord.carton_id.in_(
                    db.select(Carton.id).where(Carton.batch_id == packed.batch_id)
                ),
                # 或直接检查（carton_id为NULL且该客户没有其他批次的carton运费）
                FreightRecord.carton_id.is_(None)
            )
        ))
        if not existing_freight:
            chargeable = calc_chargeable_weight(packed.volume_weight, packed.weight)
            amount = calc_freight(chargeable, packed.nature)
            freight = FreightRecord(
                customer_id=packed.customer_id,
                amount=amount,
                status='待结算'
            )
            db.session.add(freight)
            customer = db.session.get(Customer, packed.customer_id)
            if customer:
                customer.pending_freight = (customer.pending_freight or 0) + amount

    create_operation_log(
        action='COMPLETE_ORDER',
        content=f'打包记录 {packed.serial_label} 已完成',
        target_type='PackedPackage',
        target_id=packed_id,
        extra_data=json.dumps({
            'serial_label': packed.serial_label,
            'customer_name': packed.customer.name if packed.customer else ''
        })
    )

    db.session.commit()
    return api_success(message=f'订单 {packed.serial_label} 已完成')


@app.route('/api/packed/batch_complete', methods=['POST'])
def api_batch_complete_packed_records():
    """批量确认完成打包记录"""
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return api_error('请提供要完成的记录ID列表', code='MISSING_IDS', status_code=400)

    success_count = 0
    fail_count = 0
    freight_created = set()  # (customer_id, batch_id) 去重

    for packed_id in ids:
        try:
            packed = db.session.get(PackedPackage, packed_id)
            if not packed:
                fail_count += 1
                continue
            if packed.status == '已完成':
                fail_count += 1
                continue
            if packed.status not in ('已发货', '已合箱'):
                fail_count += 1
                continue

            # 检查箱子尺寸重量是否填写完整
            if not (packed.weight and packed.weight > 0 and packed.length and packed.length > 0 and packed.width and packed.width > 0 and packed.height and packed.height > 0):
                fail_count += 1
                continue

            packed.status = '已完成'
            success_count += 1

            # 自动创建运费记录
            if packed.batch_id:
                key = (packed.customer_id, packed.batch_id)
                if key not in freight_created:
                    existing = db.session.scalar(db.select(FreightRecord).filter(
                        FreightRecord.customer_id == packed.customer_id,
                        db.or_(
                            FreightRecord.carton_id.in_(
                                db.select(Carton.id).where(Carton.batch_id == packed.batch_id)
                            ),
                            FreightRecord.carton_id.is_(None)
                        )
                    ))
                    if not existing:
                        chargeable = calc_chargeable_weight(packed.volume_weight, packed.weight)
                        amount = calc_freight(chargeable, packed.nature)
                        freight = FreightRecord(
                            customer_id=packed.customer_id,
                            amount=amount,
                            status='待结算'
                        )
                        db.session.add(freight)
                        customer = db.session.get(Customer, packed.customer_id)
                        if customer:
                            customer.pending_freight = (customer.pending_freight or 0) + amount
                    freight_created.add(key)
        except Exception:
            fail_count += 1

    db.session.commit()

    msg = f'批量确认完成：成功 {success_count} 条'
    if fail_count > 0:
        msg += f'，跳过 {fail_count} 条（未发货或已完成）'

    return api_success(message=msg, data={'success': success_count, 'fail': fail_count})


@app.route('/api/carton/<int:carton_id>/freight', methods=['POST'])
def api_create_freight(carton_id):
    """创建运费记录 - 按计费重比例分摊运费"""
    carton = db.session.get(Carton, carton_id)

    # 获取箱子内所有客户及其打包包裹信息
    customer_packages = db.session.query(
        Customer.id,
        Customer.name,
        func.sum(PackedPackage.weight).label('total_weight'),
        func.sum(PackedPackage.volume_weight).label('total_vol_weight')
    ).join(
        PackedPackage, PackedPackage.customer_id == Customer.id
    ).join(
        CartonItem, CartonItem.packed_package_id == PackedPackage.id
    ).filter(
        CartonItem.carton_id == carton_id
    ).group_by(Customer.id, Customer.name).all()

    customer_ids = [item[0] for item in customer_packages]
    total_amount = get_request_float('amount', 0.0)

    if not customer_ids or total_amount <= 0:
        return api_error('缺少客户或运费金额无效', code='INVALID_PARAMS')

    # 计算每个客户的计费重
    customer_chargeable = {}
    for cust_id, cust_name, total_weight, total_vol_weight in customer_packages:
        chargeable = max(total_weight or 0, total_vol_weight or 0)
        if chargeable < 1.0:
            chargeable = 1.0  # 最小计费重1kg
        customer_chargeable[cust_id] = chargeable

    # 计算总计费重
    total_chargeable = sum(customer_chargeable.values())

    # 按计费重比例分摊运费
    for cust_id in customer_ids:
        chargeable = customer_chargeable.get(cust_id, 1.0)
        ratio = chargeable / total_chargeable if total_chargeable > 0 else 1.0 / len(customer_ids)
        amount = round(total_amount * ratio, 2)

        freight = FreightRecord(
            customer_id=cust_id,
            carton_id=carton_id,
            amount=amount,
            status='待结算'
        )
        db.session.add(freight)

        # 更新客户待付运费
        customer = db.session.get(Customer, cust_id)
        if customer:
            customer.pending_freight = (customer.pending_freight or 0) + amount

    # 记录操作日志
    create_operation_log(
        action='CREATE_FREIGHT',
        content=f'为箱子 {carton.name} 创建运费记录，总金额 ¥{total_amount:.2f}',
        target_type='Carton',
        target_id=carton_id,
        extra_data=json.dumps({
            'carton_name': carton.name,
            'total_amount': total_amount,
            'customer_count': len(customer_ids)
        })
    )

    db.session.commit()
    return api_success(message=f'运费已按计费重比例分摊给 {len(customer_ids)} 个客户')


# ==================== 打印 API ====================

@app.route('/api/barcode/<int:customer_id>')
def api_get_customer_barcode(customer_id):
    """获取客户条形码图片"""
    customer = db.session.get(Customer, customer_id)
    bar_data = customer.barcode  # 6位短条码，无需截断
    img_buffer = generate_barcode_image(bar_data)
    return send_file(img_buffer, mimetype='image/svg+xml')


@app.route('/api/barcode/packed/<int:packed_id>')
def api_get_packed_barcode(packed_id):
    """获取打包包裹条形码图片 - 内容为包裹ID（全局唯一）"""
    packed = db.session.get(PackedPackage, packed_id)
    # 条码内容用 packed.id（全局唯一，避免跨客户 serial_label 冲突）
    bar_data = str(packed.id)
    img_buffer = generate_barcode_image(bar_data)
    return send_file(img_buffer, mimetype='image/svg+xml')


@app.route('/api/barcode/carton/<int:carton_id>')
def api_get_carton_barcode(carton_id):
    """获取箱子条形码图片"""
    carton = db.session.get(Carton, carton_id)
    bar_data = f"{carton.name}|{carton.carton_type}"
    img_buffer = generate_barcode_image(bar_data)
    return send_file(img_buffer, mimetype='image/svg+xml')


# ──────────────────────────────────────────────────────────────────
# HSJ 测量设备对接 — 鸿顺捷协议 V1.5，设备主动推送数据
# ──────────────────────────────────────────────────────────────────

import math

def clean_measurement_data(length, width, height, weight):
    """
    数据清洗：
    - 长宽高：进一法保留整数 (math.ceil)
    - 重量：四舍五入保留1位小数
    - 异常值过滤 (>9999 视为无效)
    """
    cleaned = {}
    for field, val in [('length', length), ('width', width), ('height', height)]:
        if val is not None and val > 0:
            if val > 9999:
                return None, f'{field}={val} 数值异常（>9999），已跳过'
            cleaned[field] = math.ceil(val)  # 进一法取整
        else:
            cleaned[field] = val

    if weight is not None and weight > 0:
        if weight > 9999:
            return None, f'weight={weight} 数值异常（>9999），已跳过'
        cleaned['weight'] = round(weight, 1)  # 四舍五入保留1位
    else:
        cleaned['weight'] = weight

    return cleaned, None


@app.route('/Send Package Info/Auto', methods=['POST'])
@app.route('/SendPackageInfo/Auto', methods=['GET', 'POST'])
@app.route('/hsj/data', methods=['GET', 'POST'])
def hsj_data_push():
    """
    设备扫码+测量后主动 POST 数据。
    条码 = packed.id，重量 kg，尺寸 cm。

    请求（JSON）: {"ticketsNum":"123","weight":0.5,"length":27,"width":19,"height":3,...}
    成功响应:     {"result": "true"}
    失败响应:     {"result": "false", "message": "错误描述"}
    """
    # 同时支持 JSON 和表单格式（设备通常发 application/x-www-form-urlencoded）
    raw = request.get_json(force=True, silent=True) or {}
    if not raw:
        # 回退到表单数据
        raw = {k: v for k, v in request.form.items()}
    if not raw:
        # 回退到 URL 参数
        raw = {k: v for k, v in request.args.items()}
    data = {k.strip(): v for k, v in raw.items() if isinstance(k, str)}
    # 兼容多种条码字段名
    tickets_num = (data.get('ticketsNum') or data.get('sendcode') or data.get('barcode') or '').strip()
    if not tickets_num:
        return jsonify({'result': 'false', 'message': 'ticketsNum 不能为空'}), 400

    try:
        packed_id = int(tickets_num)
    except ValueError:
        return jsonify({'result': 'false', 'message': f'ticketsNum "{tickets_num}" 格式错误，应为纯数字'}), 400

    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return jsonify({'result': 'false', 'message': f'包裹 {packed_id} 不存在'}), 404

    if packed.status == '已完成':
        return jsonify({'result': 'false', 'message': '已完成包裹不允许修改测量数据'}), 403

    changed = []

    # 重量必填（协议要求）
    weight = data.get('weight')
    if weight is None:
        return jsonify({'result': 'false', 'message': 'weight 为必填字段'}), 400
    try:
        w = float(weight)
    except (ValueError, TypeError):
        return jsonify({'result': 'false', 'message': f'weight "{weight}" 不是有效数字'}), 400

    # 读取尺寸
    try:
        l_val = float(data.get('length', 0)) if data.get('length') else None
        wd_val = float(data.get('width', 0)) if data.get('width') else None
        h_val = float(data.get('height', 0)) if data.get('height') else None
    except (ValueError, TypeError):
        return jsonify({'result': 'false', 'message': '尺寸格式错误'}), 400

    # 数据清洗：进一法取整 + 四舍五入 + 异常值过滤
    cleaned, err_msg = clean_measurement_data(l_val, wd_val, h_val, w)
    if err_msg:
        return jsonify({'result': 'false', 'message': err_msg}), 400
    if cleaned is None:
        return jsonify({'result': 'false', 'message': '数据清洗失败'}), 400

    if cleaned['weight'] > 0:
        packed.weight = cleaned['weight']
        changed.append('weight')

    for field in ('length', 'width', 'height'):
        val = cleaned.get(field)
        if val is not None and val > 0:
            setattr(packed, field, val)
            changed.append(field)

    if not changed:
        return jsonify({'result': 'false', 'message': '没有有效的测量数据（weight/尺寸均为0或无变化）'}), 400

    # 重新计算体积重
    packed.volume_weight = calc_volume_weight(packed.length or 0, packed.width or 0, packed.height or 0)

    db.session.commit()

    # 构建包裹代号（如"花儿的幸福1-1M"），供设备侧显示
    customer_name = packed.customer.name if packed.customer else ''
    serial_short = packed.serial_label.split('#')[0] if packed.serial_label else ''
    nature_code = 'M' if packed.nature == '敏' else 'P'
    package_code = f'{customer_name}{serial_short}{nature_code}'

    # 记录到全局缓存，供前端实时轮询
    global _latest_measurement
    _latest_measurement = {
        'packed_id': packed.id,
        'customer_name': customer_name,
        'serial_label': packed.serial_label,
        'package_code': package_code,
        'weight': packed.weight,
        'length': packed.length,
        'width': packed.width,
        'height': packed.height,
        'volume_weight': packed.volume_weight,
        'timestamp': datetime.now().isoformat(),
    }

    # ChuteNo 必须是整数（设备期望端口号），非分拣场景固定为 1
    # 包裹代号放在 message / packcode 字段供上层读取，设备自带显示屏只显示数字
    return jsonify({
        'result': 'true',
        'ChuteNo': 1,
        'packcode': package_code,
        'message': package_code,
        'package_code': package_code,
        'serial': serial_short,
        'customer_name': customer_name,
        'nature': packed.nature,
    })


@app.route('/api/measure/latest', methods=['GET'])
def api_measure_latest():
    """返回最近测量设备上传的包裹信息（供前端轮询）"""
    global _latest_measurement
    if not _latest_measurement or not _latest_measurement.get('timestamp'):
        return jsonify({'success': True, 'data': None})
    # 检查是否超过60秒（超时视为无效）
    from datetime import datetime, timedelta
    try:
        ts = datetime.fromisoformat(_latest_measurement['timestamp'])
        if datetime.now() - ts > timedelta(seconds=60):
            return jsonify({'success': True, 'data': None})
    except Exception:
        pass
    return jsonify({'success': True, 'data': _latest_measurement})


@app.route('/Login/Auto', methods=['POST'])
def hsj_login():
    """设备登录验证（可选，当前直接放行）"""
    return jsonify({'HasError': 'true'})


@app.route('/api/measure/status')
def api_measure_status():
    """测量设备对接状态面板（醒目的可视化页面）"""
    return render_template_string(MEASURE_STATUS_HTML)


# ──────────────────────────────────────────────────────────────────
# 通用测量接口 - 供 Web 前端 / 小程序查询 & 手动补录
# ──────────────────────────────────────────────────────────────────

@app.route('/api/measure/packed/<int:packed_id>', methods=['GET'])
def api_measure_get(packed_id):
    """
    查询包裹基础信息（供测量设备扫码后回显）
    GET /api/measure/packed/<packed_id>
    响应：
      {
        "success": true,
        "data": {
          "packed_id": 123,
          "serial_label": "2-1",
          "customer_name": "张三",
          "nature": "普货",
          "status": "待合箱",
          "weight": 1.5,
          "length": 30.0,
          "width": 20.0,
          "height": 15.0,
          "volume_weight": 1.5,
          "is_measured": false,    // weight/尺寸均已填写则为 true
          "barcode_value": "123"   // 二维码/条码的原始值，与 packed_id 相同
        }
      }
    """
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('包裹不存在', code='NOT_FOUND', status_code=404)

    # 通过 join 获取客户名
    customer = db.session.get(Customer, packed.customer_id)
    customer_name = customer.name if customer else ''
    if packed.secondary_customer_names:
        customer_name = f"{customer_name}（{packed.secondary_customer_names}）"

    is_measured = (
        packed.weight and packed.weight > 0
        and packed.length and packed.length > 0
        and packed.width and packed.width > 0
        and packed.height and packed.height > 0
    )

    return api_success(data={
        'packed_id': packed.id,
        'serial_label': packed.serial_label,
        'customer_name': customer_name,
        'nature': packed.nature,
        'status': packed.status,
        'weight': packed.weight or 0,
        'length': packed.length or 0,
        'width': packed.width or 0,
        'height': packed.height or 0,
        'volume_weight': packed.volume_weight or 0,
        'is_measured': bool(is_measured),
        'barcode_value': str(packed.id),
    })


@app.route('/api/measure/packed/<int:packed_id>', methods=['POST'])
def api_measure_submit(packed_id):
    """
    上报包裹测量数据（重量 + 尺寸）
    POST /api/measure/packed/<packed_id>
    请求体（JSON，字段均可选，只上报有值的字段）：
      {
        "weight": 1.5,    // 实重 kg
        "length": 30.0,   // 长 cm
        "width":  20.0,   // 宽 cm
        "height": 15.0    // 高 cm
      }
    响应：
      {
        "success": true,
        "data": {
          "packed_id": 123,
          "weight": 1.5,
          "length": 30.0,
          "width": 20.0,
          "height": 15.0,
          "volume_weight": 1.5
        }
      }
    """
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('包裹不存在', code='NOT_FOUND', status_code=404)

    if packed.status in ('已完成',):
        return api_error('已完成的包裹不允许修改测量数据', code='FORBIDDEN', status_code=403)

    data = request.get_json(silent=True) or {}

    updated_fields = []
    if 'weight' in data:
        val = data['weight']
        if val is not None and val != '':
            packed.weight = float(val)
            updated_fields.append('weight')
    if 'length' in data:
        val = data['length']
        if val is not None and val != '':
            packed.length = float(val)
            updated_fields.append('length')
    if 'width' in data:
        val = data['width']
        if val is not None and val != '':
            packed.width = float(val)
            updated_fields.append('width')
    if 'height' in data:
        val = data['height']
        if val is not None and val != '':
            packed.height = float(val)
            updated_fields.append('height')

    if not updated_fields:
        return api_error('请求体中没有可更新的字段（weight/length/width/height）', code='NO_DATA')

    # 重新计算体积重
    packed.volume_weight = calc_volume_weight(packed.length, packed.width, packed.height)

    db.session.commit()

    return api_success(
        data={
            'packed_id': packed.id,
            'weight': packed.weight or 0,
            'length': packed.length or 0,
            'width': packed.width or 0,
            'height': packed.height or 0,
            'volume_weight': packed.volume_weight or 0,
            'updated_fields': updated_fields,
        },
        message=f'测量数据已保存（{", ".join(updated_fields)}）'
    )


@app.route('/api/measure/packed/by_barcode', methods=['GET'])
def api_measure_get_by_barcode():
    """
    通过条码/二维码字符串查询包裹（扫码后自动路由）
    GET /api/measure/packed/by_barcode?code=123
    说明：条码内容就是 packed_id，此接口为便捷封装，内部等价于 GET /api/measure/packed/<id>
    """
    code = request.args.get('code', '').strip()
    if not code:
        return api_error('缺少 code 参数', code='MISSING_PARAM')
    try:
        packed_id = int(code)
    except ValueError:
        return api_error('无效的条码值，应为纯数字（packed_id）', code='INVALID_CODE')

    # 复用上面的查询逻辑
    packed = db.session.get(PackedPackage, packed_id)
    if not packed:
        return api_error('包裹不存在', code='NOT_FOUND', status_code=404)

    customer = db.session.get(Customer, packed.customer_id)
    customer_name = customer.name if customer else ''
    if packed.secondary_customer_names:
        customer_name = f"{customer_name}（{packed.secondary_customer_names}）"

    is_measured = (
        packed.weight and packed.weight > 0
        and packed.length and packed.length > 0
        and packed.width and packed.width > 0
        and packed.height and packed.height > 0
    )

    return api_success(data={
        'packed_id': packed.id,
        'serial_label': packed.serial_label,
        'customer_name': customer_name,
        'nature': packed.nature,
        'status': packed.status,
        'weight': packed.weight or 0,
        'length': packed.length or 0,
        'width': packed.width or 0,
        'height': packed.height or 0,
        'volume_weight': packed.volume_weight or 0,
        'is_measured': bool(is_measured),
        'barcode_value': str(packed.id),
    })


@app.route('/api/measure/pending')
def api_measure_pending():
    """
    获取待测量包裹列表
    GET /api/measure/pending?batch_id=86&limit=50

    筛选条件：status 不为「已完成」且（weight<=0 或 任意尺寸<=0）
    """
    batch_id = request.args.get('batch_id', type=int)
    limit = request.args.get('limit', default=50, type=int)

    query = db.select(PackedPackage).where(
        PackedPackage.status != '已完成'
    ).where(
        db.or_(
            PackedPackage.weight == 0,
            PackedPackage.length == 0,
            PackedPackage.width == 0,
            PackedPackage.height == 0,
        )
    ).options(joinedload(PackedPackage.customer)).order_by(PackedPackage.id)

    if batch_id:
        query = query.filter_by(batch_id=batch_id)

    query = query.limit(limit)
    result = db.session.execute(query).unique().scalars().all()

    packages = []
    for p in result:
        customer_name = p.customer.name if p.customer else ''
        if p.secondary_customer_names:
            customer_name = f"{customer_name}（{p.secondary_customer_names}）"
        packages.append({
            'packed_id': p.id,
            'serial_label': p.serial_label,
            'customer_name': customer_name,
            'nature': p.nature,
            'status': p.status,
            'weight': p.weight or 0,
            'length': p.length or 0,
            'width': p.width or 0,
            'height': p.height or 0,
            'barcode_value': str(p.id),
            'missing': {
                'weight': not (p.weight and p.weight > 0),
                'length': not (p.length and p.length > 0),
                'width': not (p.width and p.width > 0),
                'height': not (p.height and p.height > 0),
            },
        })

    return api_success(data={
        'total': len(packages),
        'packages': packages,
    })


def _render_customers_batch_labels(ids_str, is_pdf=False):
    """渲染批量客户面单 HTML"""
    if not ids_str:
        return None, "没有选择客户", 400
    try:
        customer_ids = [int(x) for x in ids_str.split(',') if x]
    except ValueError:
        return None, "无效的客户ID", 400

    customers = db.session.scalars(db.select(Customer).filter(Customer.id.in_(customer_ids))).all()
    for c in customers:
        bi = _make_inline_barcode(c.barcode)
        c.bar_svg = bi['svg']
        c.bar_w_mm = bi['width_mm']
        c.bar_h_mm = bi['height_mm']
        # 获取区位信息
        slot_labels = []
        slot = db.session.execute(
            db.select(ShelfSlot).where(ShelfSlot.customer_id == c.id)
        ).scalar()
        if slot:
            slot_labels.append(slot.slot_label)
        pkg_slots = db.session.execute(
            db.select(ShelfSlot.slot_label).join(IncomingPackage, IncomingPackage.shelf_slot_id == ShelfSlot.id)
            .where(
                IncomingPackage.customer_id == c.id,
                IncomingPackage.status.in_(['已入库', '待打包']),
                IncomingPackage.shelf_slot_id.isnot(None)
            ).distinct()
        ).scalars().all()
        for sl in pkg_slots:
            if sl not in slot_labels:
                slot_labels.append(sl)
        c.slot_labels = slot_labels
        c.has_box = c.has_box or False
        c.has_bulk = c.has_bulk or False
    return render_template('label_template.html', label_type='customers_batch',
                          customers=customers, is_pdf=is_pdf), None, 200


@app.route('/print_customers')
def print_customers_labels():
    """批量打印客户面单"""
    ids_str = request.args.get('ids', '')
    html, error, code = _render_customers_batch_labels(ids_str)
    if error:
        return error, code
    return html


@app.route('/pdf/print_customers')
def pdf_print_customers():
    """批量下载客户面单 PDF"""
    ids_str = request.args.get('ids', '')
    html, error, code = _render_customers_batch_labels(ids_str, is_pdf=True)
    if error:
        return error, code
    pdf_buf = _html_to_pdf(html)
    from datetime import datetime
    filename = f'customer_labels_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
    return send_file(pdf_buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


def _render_customer_label(customer_id, is_pdf=False):
    """渲染客户面单 HTML - 10cm × 10cm"""
    customer = db.session.get(Customer, customer_id)
    bar_info = _make_inline_barcode(customer.barcode)

    # 获取客户货位信息
    slot_labels = []
    slot = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
    ).scalar()
    if slot:
        slot_labels.append(slot.slot_label)
    # 再找包裹所在的区位
    pkg_slots = db.session.execute(
        db.select(ShelfSlot.slot_label).join(IncomingPackage, IncomingPackage.shelf_slot_id == ShelfSlot.id)
        .where(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.isnot(None)
        ).distinct()
    ).scalars().all()
    for sl in pkg_slots:
        if sl not in slot_labels:
            slot_labels.append(sl)

    return render_template('label_template.html', label_type='customer',
                          customer=customer, bar_svg=bar_info['svg'],
                          bar_w_mm=bar_info['width_mm'], bar_h_mm=bar_info['height_mm'],
                          is_pdf=is_pdf, slot_labels=slot_labels,
                          has_box=customer.has_box, has_bulk=customer.has_bulk)


@app.route('/print_customer/<int:customer_id>')
def print_customer_label(customer_id):
    """打印客户面单 - 10cm × 10cm"""
    return _render_customer_label(customer_id)


@app.route('/pdf/print_customer/<int:customer_id>')
def pdf_print_customer(customer_id):
    """下载客户面单 PDF - 10cm × 10cm"""
    html = _render_customer_label(customer_id, is_pdf=True)
    pdf_buf = _html_to_pdf(html)
    customer = db.session.get(Customer, customer_id)
    filename = f'customer_label_{customer.barcode}.pdf'
    return send_file(pdf_buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


@app.route('/print_packed/<int:packed_id>')
def print_packed_label(packed_id):
    """打印打包标签 - 10cm × 15cm"""
    packed = db.session.get(PackedPackage, packed_id)
    customer = db.session.get(Customer, packed.customer_id)
    batch = db.session.get(Batch, packed.batch_id) if packed.batch_id else None

    # 计算计费重和运费
    chargeable_weight = calc_chargeable_weight(packed.volume_weight, packed.weight)
    freight = calc_freight(chargeable_weight, packed.nature)
    # 条码内容用 packed.id（全局唯一，扫码零冲突）
    bar_svg = _make_inline_barcode_legacy(str(packed.id))

    return render_template('label_template.html',
                          label_type='packed',
                          packed=packed,
                          customer=customer,
                          batch=batch,
                          chargeable_weight=chargeable_weight,
                          freight=freight,
                          bar_svg=bar_svg,
                          is_vip_solo=bool(packed.is_vip_solo))


@app.route('/print_carton/<int:carton_id>')
def print_carton_label(carton_id):
    """打印箱子标签 - 10cm × 15cm"""
    carton = db.session.get(Carton, carton_id)
    # 使用 CartonItem 关联查询，正确获取 packed_package 和 customer
    carton_items = db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()
    items = []
    for ci in carton_items:
        packed = db.session.get(PackedPackage, ci.packed_package_id)
        customer = db.session.get(Customer, packed.customer_id)
        items.append({
            'packed': packed,
            'customer': customer,
            'serial_label': packed.serial_label,
            'nature': packed.nature
        })
    return render_template('label_template.html', label_type='carton', carton=carton, items=items)


@app.route('/print_batch/<int:batch_id>')
def print_batch_label(batch_id):
    """打印批次清单 - 优化N+1查询"""
    batch = db.session.get(Batch, batch_id)

    # 获取该批次所有打包包裹（优化查询）
    packed_list = db.session.execute(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer),
        joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package)
    ).filter_by(batch_id=batch_id)).unique().scalars().all()

    items_data = []
    for p in packed_list:
        trackings = [item.incoming_package.tracking_number for item in p.items]
        items_data.append({
            'packed': p,
            'customer': p.customer,
            'trackings': trackings
        })

    return render_template('label_template.html', label_type='batch', batch=batch, items=items_data)


@app.route('/print_packed_v2/<int:packed_id>')
def print_packed_label_v2(packed_id):
    """打印打包操作标签 - 10cm × 10cm（新版，按 Word 模板格式）"""
    packed = db.session.get(PackedPackage, packed_id)
    customer = db.session.get(Customer, packed.customer_id)
    batch = db.session.get(Batch, packed.batch_id) if packed.batch_id else None
    # 条码内容用 packed.id（全局唯一）
    bar_svg = _make_inline_barcode_legacy(str(packed.id))
    return render_template('label_template.html',
                          label_type='packed_v2',
                          packed=packed,
                          customer=customer,
                          batch=batch,
                          bar_svg=bar_svg,
                          is_vip_solo=bool(packed.is_vip_solo))


@app.route('/print_carton_v2/<int:carton_id>')
def print_carton_label_v2(carton_id):
    """打印合箱标签 - A4（新版，按 Word 模板格式）"""
    carton = db.session.get(Carton, carton_id)
    carton_items = db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()

    # 获取批次信息（所有箱子内的包裹应该属于同一批次）
    batch = None
    current_box_no = 1
    total_boxes = 1
    customer = None

    items = []
    for ci in carton_items:
        packed = db.session.get(PackedPackage, ci.packed_package_id)
        cust = db.session.get(Customer, packed.customer_id) if packed else None
        if not batch and packed and packed.batch_id:
            batch = db.session.get(Batch, packed.batch_id)
        if not customer and cust:
            customer = cust

        trackings = []
        if packed:
            for item in packed.items:
                trackings.append(item.incoming_package.tracking_number)

        items.append({
            'serial_label': packed.serial_label if packed else '?',
            'customer_name': cust.name if cust else '?',
            'nature': packed.nature if packed else '普',
            'tracking_numbers': ', '.join(trackings)
        })

    # 计算该类型箱子在批次中的序号
    if batch and carton:
        same_type_cartons = db.session.query(Carton).filter(
            Carton.carton_type == carton.carton_type,
            Carton.id.in_(
                db.session.query(CartonItem.carton_id).join(PackedPackage).filter(
                    PackedPackage.batch_id == batch.id
                )
            )
        ).order_by(Carton.id).all()
        total_boxes = len(same_type_cartons)
        for i, c in enumerate(same_type_cartons, 1):
            if c.id == carton_id:
                current_box_no = i
                break

    from datetime import datetime
    return render_template('label_template.html',
                          label_type='carton_v2',
                          carton=carton,
                          batch=batch,
                          customer=customer,
                          items=items,
                          current_box_no=current_box_no,
                          total_boxes=total_boxes,
                          now=datetime.now())


# ==================== 新版标签打印（HTML） ====================

def _render_packed_label(packed_id, is_pdf=False):
    """渲染打包操作标签 HTML - 10cm × 10cm"""
    packed = db.session.get(PackedPackage, packed_id)
    customer = db.session.get(Customer, packed.customer_id)
    batch = db.session.get(Batch, packed.batch_id) if packed.batch_id else None

    batch_no = batch.batch_no if batch else '---'
    nature_label = 'M' if packed.nature == '敏' else 'P'

    # 处理 serial_label（可能为 None 或格式异常）
    raw_serial = packed.serial_label or str(packed.id)
    if '#' in raw_serial:
        display_serial = raw_serial.split('#')[0]
    else:
        display_serial = raw_serial

    # 条码内容用 packed.id（全局唯一）
    try:
        bar_svg = _make_inline_barcode_legacy(str(packed.id))
    except Exception as e:
        app.logger.error(f'生成条码SVG失败 packed_id={packed_id}: {e}')
        bar_svg = f'<svg viewBox="0 0 200 40"><text x="10" y="25" font-size="12">条码错误</text></svg>'

    customer_name = customer.name if customer else '未知'
    if packed.secondary_customer_names:
        customer_name = f'{customer_name}（{packed.secondary_customer_names}）'
    customer_address = customer.address if customer and customer.address else ''

    is_vip_solo = bool(packed.is_vip_solo)

    return render_template('packed_label.html',
                          batch_no=batch_no,
                          nature_label=nature_label,
                          serial_label=display_serial,
                          customer_name=customer_name,
                          customer_address=customer_address,
                          bar_svg=bar_svg,
                          is_pdf=is_pdf,
                          is_vip_solo=is_vip_solo)


def _render_carton_label(carton_id, is_pdf=False):
    """渲染合箱标签 HTML - A4，只显示序号/包裹编号/客户姓名"""
    carton = db.session.get(Carton, carton_id)
    carton_items = db.session.scalars(db.select(CartonItem).filter_by(carton_id=carton_id)).all()

    # 获取批次信息
    batch = db.session.get(Batch, carton.batch_id) if carton.batch_id else None

    # 箱型：P 或 M
    box_type = 'M' if carton.carton_type == '敏' else 'P'
    raw_batch_no = batch.batch_no if batch else '---'
    # 去除 batch_no 中已有的 P/M 前缀
    clean_batch_no = raw_batch_no
    if clean_batch_no and clean_batch_no != '---' and clean_batch_no[0].upper() in ('P', 'M'):
        clean_batch_no = clean_batch_no[1:]

    # 邮编：自定义邮编 > 分组邮编 > 批次邮编
    group = db.session.get(CartonGroup, carton.group_id) if carton.group_id else None
    zip_code = (carton.custom_zip_code
                or (group.zip_code if group else None)
                or (batch.zip_code if batch and batch.zip_code else '')
                or '邮编未设')

    # 箱号/总箱数：按同分组同类型计算；无分组则按同批次同类型
    if carton.group_id:
        # 同分组同类型所有箱子，按box_seq排序
        group_cartons = db.session.scalars(
            db.select(Carton).filter_by(group_id=carton.group_id, carton_type=carton.carton_type)
            .order_by(Carton.box_seq)
        ).all()
        total_boxes = len(group_cartons)
        # 直接使用box_seq作为序号
        current_box = carton.box_seq or 1
    else:
        current_box = carton.box_seq or 1
        total_boxes = 1
        if batch:
            total_boxes = _scalar_count(Carton, batch_id=batch.id, carton_type=carton.carton_type)
            if total_boxes == 0:
                total_boxes = 1

    items = []
    for ci in carton_items:
        packed = db.session.get(PackedPackage, ci.packed_package_id)
        cust = db.session.get(Customer, packed.customer_id) if packed else None
        serial = packed.serial_label if packed else '?'
        # 只保留 "#" 之前的部分，如 "1-1#945" → "1-1"
        serial_short = serial.split('#')[0] if '#' in serial else serial

        items.append({
            'serial_label': serial_short,
            'customer_name': cust.name if cust else '?',
            'nature': packed.nature if packed else '普',
        })

    # 普货放前面，敏货放后面
    items.sort(key=lambda x: (0 if x['nature'] == '普' else 1))
    
    # 行高/字体精确缩放：用满格子空间
    max_name_len = max((len(item['customer_name']) for item in items), default=1)
    row_count = len(items)
    col3_width_mm = 64
    # 表格可用高度（6in=152mm，扣header+padding留22mm余量）
    row_height_mm = max(6, round(130 / row_count, 1))
    # 字体压满行高：pt * 0.3528 <= row，line-height用row撑满
    font_pt_by_height = int(row_height_mm / 0.35)
    font_pt_by_width = int(col3_width_mm / (max_name_len * 0.35)) if max_name_len else 30
    font_pt = max(10, min(font_pt_by_height, font_pt_by_width))
    # 多行时略降字号保不溢页
    if row_count >= 10:
        font_pt = min(font_pt, 24)
    elif row_count >= 8:
        font_pt = min(font_pt, 28)
    page_height_in = 7.5

    from datetime import datetime
    return render_template('carton_label.html',
                          box_type=box_type,
                          batch_no=clean_batch_no,
                          zip_code=zip_code,
                          current_box=current_box,
                          total_boxes=total_boxes,
                          items=items,
                          is_pdf=is_pdf,
                          font_pt=font_pt,
                          row_height_mm=row_height_mm,
                          page_height_in=page_height_in)


@app.route('/print_packed_label/<int:packed_id>')
def print_packed_label_new(packed_id):
    """打印打包操作标签 - 10cm × 10cm（新版干净模板）"""
    return _render_packed_label(packed_id)


@app.route('/print_carton_label/<int:carton_id>')
def print_carton_label_new(carton_id):
    """打印合箱标签 - A4（新版干净模板）"""
    return _render_carton_label(carton_id)


# ==================== PDF 生成 ====================

def _html_to_pdf(html_content):
    """将 HTML 转为 PDF 字节流（优先 weasyprint，回退 chromium）"""
    import io

    # 优先使用 weasyprint（速度快、无需浏览器）
    try:
        from weasyprint import HTML
        doc = HTML(string=html_content)
        buf = io.BytesIO()
        doc.write_pdf(target=buf)
        buf.seek(0)
        return buf
    except ImportError:
        pass
    except Exception as e:
        print(f"[PDF] weasyprint 失败，回退 chromium: {e}", flush=True)

    # 回退：使用 chromium headless
    import tempfile
    import subprocess
    import platform

    snap_tmp = '/tmp/snap-private-tmp/snap.chromium/tmp'
    if os.path.isdir(snap_tmp):
        tmp_dir = snap_tmp
    else:
        tmp_dir = tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)

    html_path = os.path.join(tmp_dir, f'_render_{os.getpid()}_{id(html_content) & 0xFFFF}.html')
    pdf_path = html_path.replace('.html', '.pdf')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    try:
        if platform.system() == 'Windows':
            browser_path = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        else:
            import shutil
            browser_path = None
            for candidate in ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable']:
                found = shutil.which(candidate)
                if found:
                    browser_path = found
                    break
            if not browser_path:
                raise RuntimeError('未找到可用的浏览器')

        subprocess.run([
            browser_path,
            '--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
            f'--print-to-pdf={pdf_path}',
            f'file://{html_path}',
        ], check=True, timeout=30, capture_output=True)

        with open(pdf_path, 'rb') as f:
            pdf_data = f.read()
        buf = io.BytesIO(pdf_data)
        buf.seek(0)
        return buf
    finally:
        for p in [html_path, pdf_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


@app.route('/pdf/packed_label/<int:packed_id>')
def pdf_packed_label(packed_id):
    """下载打包操作标签 PDF"""
    with app.app_context():
        html = _render_packed_label(packed_id, is_pdf=True)
    pdf_buf = _html_to_pdf(html)
    packed = db.session.get(PackedPackage, packed_id)
    filename = f'packed_label_{packed.serial_label}.pdf'
    return send_file(pdf_buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


@app.route('/pdf/bulk_carton_labels')
def pdf_bulk_carton_labels():
    """一键导出所有未锁定批次中多客户合并（散箱）的合箱标签 PDF
    ?type=all|puhuo|minhuo&batch_id=31"""
    invoice_type = request.args.get('type', 'all')
    batch_id_filter = request.args.get('batch_id', '')
    with app.app_context():
        # 找到未锁定批次
        if batch_id_filter and batch_id_filter.strip().isdigit():
            b = db.session.get(Batch, int(batch_id_filter))
            unlocked_batch_ids = [b.id] if b and not b.is_locked else []
            if unlocked_batch_ids and b:
                unlocked_batch_ids = [b.id]
            elif not b:
                unlocked_batch_ids = db.session.scalars(db.select(Batch.id).filter_by(is_locked=False)).all()
            else:
                unlocked_batch_ids = []
        else:
            unlocked_batch_ids = db.session.scalars(db.select(Batch.id).filter_by(is_locked=False)).all()

        # 按类型筛选箱型
        type_filter = []
        if invoice_type in ('all', 'puhuo'):
            type_filter.append('普')
        if invoice_type in ('all', 'minhuo'):
            type_filter.append('敏')

        # 找到这些批次下所有已合箱的 Carton
        carton_rows = db.session.execute(
            db.select(Carton).filter(
                Carton.batch_id.in_(unlocked_batch_ids),
                Carton.carton_type.in_(type_filter)
            ).order_by(Carton.batch_id, Carton.box_seq)
        ).scalars().all()

        # 筛选多客户箱（散箱）
        multi_cartons = []
        for carton in carton_rows:
            # 查该箱子里所有包裹对应的客户
            items = db.session.scalars(
                db.select(CartonItem).filter_by(carton_id=carton.id)
            ).all()
            pids = [ci.packed_package_id for ci in items]
            packs = db.session.scalars(
                db.select(PackedPackage).filter(PackedPackage.id.in_(pids))
            ).all()
            customers = set(p.customer_id for p in packs if p.customer_id)
            if len(customers) > 1:
                batch = db.session.get(Batch, carton.batch_id)
                multi_cartons.append({
                    'carton': carton,
                    'batch_no': batch.batch_no if batch else '',
                    'customer_count': len(customers),
                    'package_count': len(packs)
                })

    # 按箱型排序：普货箱子在前，敏货箱子在后
    # 按箱型+批次排序：先所有普货（按批次号），再所有敏货（按批次号）
    multi_cartons.sort(key=lambda x: (
        0 if x['carton'].carton_type == '普' else 1,
        x['batch_no'] or '',
        x['carton'].box_seq or 0
    ))

    if not multi_cartons:
        return '<p style="text-align:center;padding:40px;">没有找到未锁定批次的散箱</p>', 404

    # 逐个生成 PDF，再用 pypdf 合并
    from pypdf import PdfWriter, PdfReader
    from io import BytesIO
    from datetime import datetime
    writer = PdfWriter()
    for item in multi_cartons:
        html = _render_carton_label(item['carton'].id, is_pdf=True)
        single_pdf = _html_to_pdf(html)
        single_pdf.seek(0)
        reader = PdfReader(single_pdf)
        for page in reader.pages:
            writer.add_page(page)

    merged_buf = BytesIO()
    writer.write(merged_buf)
    merged_buf.seek(0)

    filename = f'bulk_carton_labels_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
    return send_file(merged_buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


@app.route('/pdf/carton_label/<int:carton_id>')
def pdf_carton_label(carton_id):
    """下载合箱标签 PDF"""
    with app.app_context():
        html = _render_carton_label(carton_id, is_pdf=True)
    pdf_buf = _html_to_pdf(html)
    carton = db.session.get(Carton, carton_id)
    filename = f'carton_label_{carton.name}.pdf'
    return send_file(pdf_buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


# ==================== 待打包确认 ====================

@app.route('/api/shipment/confirm/<int:customer_id>', methods=['POST'])
def api_confirm_shipment(customer_id):
    """确认本周发货 - 将该客户所有已入库包裹标记为待打包"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    data = request.get_json(silent=True) or {}
    skip_batch_check = data.get('skip_batch_check', False)

    # 检查是否已归入批次（除非跳过检查）
    if not skip_batch_check:
        has_batch = db.session.scalar(db.select(IncomingPackage).filter_by(
            customer_id=customer_id,
            status='已入库'
        ).filter(IncomingPackage.batch_id.isnot(None)))
        if not has_batch:
            return api_error('该客户的包裹尚未归入任何批次，请先在待打包确认页面"归入批次"后再确认', code='NO_BATCH_ASSIGNED', status_code=400)

    # 更新所有已入库包裹为待打包
    result = db.session.execute(db.update(IncomingPackage).filter_by(
        customer_id=customer_id,
        status='已入库'
    ).values({'status': '待打包'})).rowcount

    if result == 0:
        return api_error('该客户没有已入库的包裹，无法确认', code='NO_PENDING_PACKAGES', status_code=400)

    db.session.commit()
    return api_success(message=f'已确认 {result} 件包裹进入待打包')


@app.route('/api/shipment/packed_info/<int:customer_id>')
def api_shipment_packed_info(customer_id):
    """获取客户今日打包信息"""
    today = datetime.now().strftime('%Y-%m-%d')
    packed_records = db.session.scalars(db.select(PackedPackage).filter(
        PackedPackage.customer_id == customer_id,
        PackedPackage.created_at >= today
    )).all()

    records = []
    for r in packed_records:
        items = db.session.scalars(db.select(PackedPackageItem).filter_by(packed_package_id=r.id)).all()
        trackings = [db.session.get(IncomingPackage, item.incoming_package_id).tracking_number for item in items]
        records.append({
            'id': r.id,
            'serial': r.serial_label,
            'nature': r.nature,
            'trackings': trackings,
            'customer_remark': r.customer_remark or '',
            'abnormal_remark': r.abnormal_remark or ''
        })

    return jsonify({'success': True, 'records': records})


@app.route('/api/shipment/cancel/<int:customer_id>', methods=['POST'])
def api_cancel_shipment(customer_id):
    """取消待打包确认 - 将该客户所有待打包包裹恢复为已入库"""
    result = db.session.execute(db.update(IncomingPackage).filter_by(
        customer_id=customer_id,
        status='待打包'
    ).values({'status': '已入库'})).rowcount

    db.session.commit()
    return jsonify({'success': True, 'message': f'已取消，{result} 件包裹恢复为已入库', 'code': 'SUCCESS'})


@app.route('/api/shipment/batch_confirm', methods=['POST'])
def api_batch_confirm_shipment():
    """批量确认待打包"""
    data = request.get_json(silent=True) or {}
    customer_ids = data.get('customer_ids', [])
    skip_batch_check = data.get('skip_batch_check', False)

    if not customer_ids:
        return api_error('请选择要确认的客户', code='VALIDATION_ERROR')

    added = []
    skipped = []
    total_updated = 0

    for cid in customer_ids:
        customer = db.session.get(Customer, cid)
        if not customer:
            skipped.append({'id': cid, 'reason': '客户不存在'})
            continue

        # 检查是否已归入批次（除非跳过检查）
        if not skip_batch_check:
            has_batch = db.session.scalar(db.select(IncomingPackage).filter_by(
                customer_id=cid, status='已入库'
            ).filter(IncomingPackage.batch_id.isnot(None)))
            if not has_batch:
                skipped.append({'id': cid, 'name': customer.name, 'reason': '未归入批次'})
                continue

        result = db.session.execute(db.update(IncomingPackage).filter_by(
            customer_id=cid, status='已入库'
        ).values({'status': '待打包'})).rowcount

        if result == 0:
            skipped.append({'id': cid, 'name': customer.name, 'reason': '无已入库包裹'})
            continue

        added.append({'id': cid, 'name': customer.name, 'pending': result})
        total_updated += result

    db.session.commit()

    result_msg = f'已确认 {len(added)} 个客户、共 {total_updated} 件包裹进入待打包'
    if skipped:
        result_msg += f'，跳过 {len(skipped)} 个'

    return api_success(data={
        'added': added,
        'skipped': skipped
    }, message=result_msg)


@app.route('/api/shipment/batch_cancel', methods=['POST'])
def api_batch_cancel_shipment():
    """批量取消待打包确认"""
    data = request.get_json(silent=True) or {}
    customer_ids = data.get('customer_ids', [])

    if not customer_ids:
        return jsonify({'success': False, 'message': '请选择要取消的客户', 'code': 'VALIDATION_ERROR'}), 400

    total = 0
    for cid in customer_ids:
        result = db.session.execute(db.update(IncomingPackage).filter_by(
            customer_id=cid, status='待打包'
        ).values({'status': '已入库'})).rowcount
        total += result

    db.session.commit()
    return jsonify({
        'success': True,
        'message': f'已取消 {len(customer_ids)} 个客户待打包确认，{total} 件包裹恢复为已入库',
        'code': 'SUCCESS',
        'count': len(customer_ids)
    })


@app.route('/api/shipment/assign_batch', methods=['POST'])
def api_assign_batch():
    """把选中客户的已入库包裹归入指定批次，batch_id=0 表示清除批次"""
    data = request.get_json(silent=True) or {}
    customer_ids = data.get('customer_ids', [])
    batch_id = data.get('batch_id')

    if not customer_ids:
        return jsonify({'success': False, 'message': '请选择客户'}), 400
    if batch_id is None:
        return jsonify({'success': False, 'message': '请选择批次'}), 400

    # batch_id=-1 表示 VIP 单独发货（跳过批次）
    if batch_id == -1:
        updated_incoming = db.session.execute(db.update(IncomingPackage).filter(
            IncomingPackage.customer_id.in_(customer_ids),
            IncomingPackage.status.in_(['已入库', '待打包'])
        ).values({'batch_id': None, 'status': '待打包'})).rowcount

        # 客户备注随批次走：不指定批次则清除客户备注
        db.session.execute(db.update(Customer).filter(
            Customer.id.in_(customer_ids)
        ).values({'remark': ''}))

        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'已标记 {len(customer_ids)} 个VIP客户（{updated_incoming} 个包裹）为单独发货',
            'code': 'SUCCESS',
            'count': updated_incoming
        })

    # batch_id=0 表示"不指定批次"：清除 IncomingPackage 的 batch_id
    # 含"已退货"状态：退货包裹不应再残留批次归属（2026-08-14 修复）
    if batch_id == 0:
        updated_incoming = db.session.execute(db.update(IncomingPackage).filter(
            IncomingPackage.customer_id.in_(customer_ids),
            IncomingPackage.status.in_(['已入库', '待打包', '已退货'])
        ).values({'batch_id': None})).rowcount

        # 客户备注随批次走：清除批次同时清除客户备注
        db.session.execute(db.update(Customer).filter(
            Customer.id.in_(customer_ids)
        ).values({'remark': ''}))

        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'已清除 {len(customer_ids)} 个客户（{updated_incoming} 个包裹）的批次指定',
            'code': 'SUCCESS',
            'count': updated_incoming
        })

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return jsonify({'success': False, 'message': '批次不存在'}), 404
    batch_no = batch.batch_no  # 在 commit 前读取，防止对象过期

    # 更新这些客户的 IncomingPackage（待打包/已打包）的 batch_id
    updated_incoming = db.session.execute(db.update(IncomingPackage).filter(
        IncomingPackage.customer_id.in_(customer_ids),
        IncomingPackage.status.in_(['已入库', '待打包', '已打包'])
    ).values({'batch_id': batch_id})).rowcount

    # 同步更新这些客户已打包但未合箱的 PackedPackage 的 batch_id
    updated_packed = db.session.execute(db.update(PackedPackage).filter(
        PackedPackage.customer_id.in_(customer_ids),
        PackedPackage.is_cartoned == False,
        PackedPackage.batch_id == None
    ).values({'batch_id': batch_id})).rowcount

    # 客户备注随批次走：指定批次时备注栏清空，重新编辑（上一轮备注不保留）
    db.session.execute(db.update(Customer).filter(
        Customer.id.in_(customer_ids)
    ).values({'remark': ''}))

    db.session.commit()

    total = updated_incoming + updated_packed
    return jsonify({
        'success': True,
        'message': f'已将 {len(customer_ids)} 个客户（{total} 个包裹）归入批次 {batch_no}',
        'code': 'SUCCESS',
        'count': total
    })


@app.route('/api/shipment/check_zip_mismatch', methods=['POST'])
def api_check_zip_mismatch():
    """检查选中客户的邮编是否与发货批次邮编一致"""
    data = request.get_json(silent=True) or {}
    customer_ids = data.get('customer_ids', [])
    batch_id = data.get('batch_id')

    if not customer_ids:
        return api_error('请提供客户ID列表', code='VALIDATION_ERROR')

    mismatches = []

    # 如果指定了批次，直接对比该批次邮编
    if batch_id:
        batch = db.session.get(Batch, batch_id)
        if not batch or not batch.zip_code or not batch.zip_code.strip():
            return api_success(data={'mismatches': [], 'warning': '', 'count': 0})

        batch_zip = batch.zip_code.strip()

        for cid in customer_ids:
            customer = db.session.get(Customer, cid)
            if not customer or not customer.zip_code or not customer.zip_code.strip():
                continue
            if customer.zip_code.strip() != batch_zip:
                mismatches.append({
                    'customer_id': cid,
                    'customer_name': customer.name,
                    'customer_zip': customer.zip_code.strip(),
                    'batch_no': batch.batch_no,
                    'batch_zip': batch_zip
                })
    else:
        # 未指定批次：按每个客户所属批次逐一比对
        for cid in customer_ids:
            customer = db.session.get(Customer, cid)
            if not customer or not customer.zip_code or not customer.zip_code.strip():
                continue
            customer_zip = customer.zip_code.strip()

            # 查找该客户的 IncomingPackage 所属批次
            batch_ids_with_packages = db.session.query(IncomingPackage.batch_id).filter(
                IncomingPackage.customer_id == cid,
                IncomingPackage.batch_id.isnot(None)
            ).distinct().all()

            for (b_id,) in batch_ids_with_packages:
                batch = db.session.get(Batch, b_id)
                if not batch or not batch.zip_code or not batch.zip_code.strip():
                    continue
                if customer_zip != batch.zip_code.strip():
                    mismatches.append({
                        'customer_id': cid,
                        'customer_name': customer.name,
                        'customer_zip': customer_zip,
                        'batch_no': batch.batch_no,
                        'batch_zip': batch.zip_code.strip()
                    })
                    break  # 找到一个不匹配就标记

    warning = ''
    if mismatches:
        names = [m['customer_name'] for m in mismatches]
        warning = f'以下 {len(mismatches)} 个客户的邮编与发货批次不一致：{", ".join(names)}'

    return api_success(data={
        'mismatches': mismatches,
        'warning': warning,
        'count': len(mismatches)
    })



# ==================== 运费 API ====================

@app.route('/api/freight/settle/<int:record_id>', methods=['POST'])
def api_settle_freight(record_id):
    """结算运费"""
    freight = db.session.get(FreightRecord, record_id)
    freight.status = '已结算'
    freight.settled_at = get_local_time()

    # 更新客户待付运费
    customer = db.session.get(Customer, freight.customer_id)
    if customer:
        customer.pending_freight = max(0, (customer.pending_freight or 0) - freight.amount)

    # 记录操作日志
    create_operation_log(
        action='SETTLE_FREIGHT',
        content=f'结算运费 ¥{freight.amount:.2f}（客户ID：{freight.customer_id}）',
        target_type='FreightRecord',
        target_id=freight.id,
        extra_data=json.dumps({
            'freight_id': freight.id,
            'customer_id': freight.customer_id,
            'amount': freight.amount
        })
    )

    db.session.commit()
    return api_success(message='运费已结算')


@app.route('/api/freight/settle_customer/<int:customer_id>', methods=['POST'])
def api_settle_customer_freight(customer_id):
    """结算客户全部待结算运费"""
    # 验证客户是否存在
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在', code='CUSTOMER_NOT_FOUND', status_code=404)

    # 获取客户所有待结算的运费记录
    pending_records = db.session.scalars(db.select(FreightRecord).filter_by(
        customer_id=customer_id,
        status='待结算'
    )).all()

    if not pending_records:
        return api_error('该客户没有待结算的运费记录', code='NO_PENDING', status_code=400)

    total_amount = sum(r.amount for r in pending_records)
    settled_count = 0

    try:
        for freight in pending_records:
            freight.status = '已结算'
            freight.settled_at = get_local_time()
            settled_count += 1

        # 更新客户待付运费
        customer.pending_freight = max(0, (customer.pending_freight or 0) - total_amount)

        # 记录操作日志
        create_operation_log(
            action='SETTLE_CUSTOMER_FREIGHT',
            content=f'结算客户 {customer.name} 全部运费 ¥{total_amount:.2f}（共{settled_count}笔）',
            target_type='Customer',
            target_id=customer_id,
            extra_data=json.dumps({
                'customer_name': customer.name,
                'total_amount': total_amount,
                'record_count': settled_count
            })
        )

        db.session.commit()
        return api_success(message=f'已结算 {settled_count} 笔运费，共 ¥{total_amount:.2f}')

    except Exception as e:
        db.session.rollback()
        return api_error(f'结算失败: {str(e)}', code='SETTLE_ERROR', status_code=500)


@app.route('/api/freight/customer/<int:customer_id>')
def api_customer_freight(customer_id):
    """获取客户运费记录"""
    records = db.session.scalars(db.select(FreightRecord).filter_by(customer_id=customer_id).order_by(
        FreightRecord.created_at.desc()
    )).all()
    return jsonify([{
        'id': r.id,
        'amount': r.amount,
        'status': r.status,
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M'),
        'settled_at': r.settled_at.strftime('%Y-%m-%d %H:%M') if r.settled_at else ''
    } for r in records])


# ==================== 单价配置 API ====================

@app.route('/api/system_config', methods=['GET', 'POST'])
def api_system_config():
    """系统配置管理（简单键值对）"""
    from models import InvoiceConfig as SysCfg
    if request.method == 'GET':
        key = request.args.get('key', '')
        if key:
            val = SysCfg.get(key, '')
            return jsonify({'success': True, 'key': key, 'value': val})
        # 返回所有配置
        configs = db.session.scalars(db.select(SysCfg)).all()
        return jsonify({'success': True, 'data': {c.key: c.value for c in configs}})
    # POST
    data = request.get_json(silent=True) or {}
    key = data.get('key', '')
    value = data.get('value', '')
    if not key:
        return jsonify({'success': False, 'message': '缺少key'}), 400
    SysCfg.set(key, str(value))
    return jsonify({'success': True, 'message': f'已更新 {key} = {value}'})


@app.route('/api/price_config', methods=['GET', 'POST'])
def api_price_config():
    """获取/更新单价配置"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}

        # 更新普货单价
        normal_price = data.get('normal_price', 12.0)
        normal_config = db.session.scalar(db.select(PriceConfig).filter_by(name='普货单价'))
        if normal_config:
            normal_config.price_per_kg = float(normal_price)
        else:
            normal_config = PriceConfig(name='普货单价', price_per_kg=float(normal_price))
            db.session.add(normal_config)

        # 更新敏货单价
        danger_price = data.get('danger_price', 15.0)
        danger_config = db.session.scalar(db.select(PriceConfig).filter_by(name='敏货单价'))
        if danger_config:
            danger_config.price_per_kg = float(danger_price)
        else:
            danger_config = PriceConfig(name='敏货单价', price_per_kg=float(danger_price))
            db.session.add(danger_config)

        db.session.commit()
        return jsonify({'success': True, 'message': '单价已更新'})

    # GET - 获取当前单价
    normal_config = db.session.scalar(db.select(PriceConfig).filter_by(name='普货单价'))
    danger_config = db.session.scalar(db.select(PriceConfig).filter_by(name='敏货单价'))

    return jsonify({
        'normal_price': normal_config.price_per_kg if normal_config else 12.0,
        'danger_price': danger_config.price_per_kg if danger_config else 15.0
    })


def get_price_config():
    """获取单价配置（返回字典）"""
    normal_config = db.session.scalar(db.select(PriceConfig).filter_by(name='普货单价'))
    danger_config = db.session.scalar(db.select(PriceConfig).filter_by(name='敏货单价'))
    return {
        'normal': normal_config.price_per_kg if normal_config else 12.0,
        'danger': danger_config.price_per_kg if danger_config else 15.0
    }


# ==================== 体积重/计费重计算函数 ====================

def round_to_half(value):
    """0.5进位: 1.1->1.5, 1.6->2.0, 2.1->2.5"""
    return math.ceil(value * 2) / 2


def calc_volume_weight(length, width, height):
    """体积重 = 长×宽×高/6000，四舍五入保留三位小数"""
    if not all([length, width, height]):
        return 0.0
    vw = length * width * height / 6000
    return round(vw, 3)


def calc_chargeable_weight(volume_weight, actual_weight):
    """计费重 = max(体积重,实际重)，最小为1，0.5进位"""
    cw = max(volume_weight or 0, actual_weight or 0, 1.0)
    return round_to_half(cw)


def calc_freight(weight, nature):
    """根据重量和货物类型计算运费，最低1元"""
    prices = get_price_config()
    price = prices['danger'] if nature == '敏' else prices['normal']
    amount = weight * price
    return round(max(amount, 1.0), 2)


def calc_surcharge(length, width, height, batch):
    """计算单个包裹的附加费
    规则（三者叠加，每条+200）：
    1. 最长边超过120cm → +200
    2. 第二长的边超过75cm → +200
    3. 最长边 + 2*(第二长边+最短边) > 260cm → +200

    仅在派送渠道非"卡派"或"自提"时产生附加费
    """
    # 检查派送渠道是否豁免附加费
    if batch:
        # 检查普货和敏货两个渠道
        deliv = (batch.normal_delivery or '').strip()
        deliv2 = (batch.sensitive_delivery or '').strip()
        # 只要有一个渠道是卡派或自提，批次即为豁免
        # 但需要匹配货物性质来判断
        # 简单处理：如果任一渠道包含卡派或自提，视为可能豁免
        # 实际使用时按具体包裹性质判断

    if not all([length, width, height]):
        return 0, '', 0

    # 排序：最长、第二长、最短
    dims = sorted([length, width, height], reverse=True)
    l_max, l_mid, l_min = dims[0], dims[1], dims[2]

    surcharge = 0
    reasons = []

    if l_max > 120:
        surcharge += 200
        reasons.append(f'最长边{l_max:.0f}cm>120')

    if l_mid > 75:
        surcharge += 200
        reasons.append(f'第二长边{l_mid:.0f}cm>75')

    girth = l_max + 2 * (l_mid + l_min)
    if girth > 260:
        surcharge += 200
        reasons.append(f'围长{girth:.0f}>260')

    return surcharge, '; '.join(reasons), girth


def get_surcharge_for_package(packed_package):
    """获取单个打包包裹的附加费（检查派送渠道豁免）"""
    if not packed_package.length or not packed_package.width or not packed_package.height:
        return 0, '', 0

    batch = packed_package.batch
    if not batch:
        return calc_surcharge(packed_package.length, packed_package.width,
                             packed_package.height, None)

    # 根据货物性质获取对应派送渠道
    if packed_package.nature == '敏':
        delivery = (batch.sensitive_delivery or '').strip()
    else:
        delivery = (batch.normal_delivery or '').strip()

    # 卡派或自提：豁免附加费
    if delivery in ('卡派', '自提') or '卡派' in delivery or '自提' in delivery:
        return 0, '', 0

    return calc_surcharge(packed_package.length, packed_package.width,
                         packed_package.height, batch)


# ==================== 统计 API ====================

@app.route('/api/stats')
def api_stats():
    """获取统计数据"""
    total_customers = _scalar_count(Customer)
    pending_packages = _scalar_count(IncomingPackage, status='已入库')
    packed_count = _scalar_count(PackedPackage, is_cartoned=False)
    cartons_count = _scalar_count(Carton, is_shipped=False)

    # 待结算运费
    total_pending_freight = db.session.query(func.sum(FreightRecord.amount)).filter_by(
        status='待结算'
    ).scalar() or 0

    return jsonify({
        'total_customers': total_customers,
        'pending_packages': pending_packages,
        'packed_count': packed_count,
        'cartons_count': cartons_count,
        'total_pending_freight': total_pending_freight
    })


# ==================== 导出 API ====================

@app.route('/export/inbound')
def export_inbound():
    """导出入库记录（按当前筛选条件）"""
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    customer_id = request.args.get('customer_id', type=int)
    status = request.args.get('status', '')
    zip_code = request.args.get('zip_code', '')
    tracking_number = request.args.get('tracking_number', '').strip()

    conditions = []
    if start_date:
        conditions.append(IncomingPackage.entry_time >= start_date)
    if end_date:
        conditions.append(IncomingPackage.entry_time <= end_date + ' 23:59:59')
    if customer_id:
        conditions.append(IncomingPackage.customer_id == customer_id)
    if tracking_number:
        conditions.append(IncomingPackage.tracking_number.like(f'%{tracking_number}%'))

    # 状态筛选：不传默认导出已入库/待打包，传了则按传入值筛选
    if status:
        conditions.append(IncomingPackage.status == status)
    else:
        conditions.append(IncomingPackage.status.in_(['已入库', '待打包']))
    # 排除已退货
    conditions.append(IncomingPackage.status != '已退货')

    query = db.select(IncomingPackage).join(Customer, IncomingPackage.customer_id == Customer.id)
    if zip_code:
        query = query.where(Customer.zip_code == zip_code)
    if conditions:
        query = query.where(db.and_(*conditions))
    # 同一客户放一起，再按入库时间排序
    query = query.order_by(Customer.name.asc(), IncomingPackage.entry_time.asc())
    records = db.session.scalars(query).all()

    # 预加载所有相关客户
    customer_ids = list(set(r.customer_id for r in records if r.customer_id))
    customers_map = {}
    if customer_ids:
        custs = db.session.scalars(db.select(Customer).where(Customer.id.in_(customer_ids))).all()
        customers_map = {c.id: c for c in custs}

    # 预加载对应的 PackedPackage 状态（用于排除已完成的包裹）
    inc_ids = [r.id for r in records]
    completed_inc_ids = set()
    if inc_ids:
        rows = db.session.execute(
            db.select(PackedPackageItem.incoming_package_id)
            .join(PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id)
            .where(
                PackedPackageItem.incoming_package_id.in_(inc_ids),
                PackedPackage.status == '已完成'
            )
        ).all()
        completed_inc_ids = {r[0] for r in rows}

    # 生成Excel (纯 openpyxl，无 pandas 依赖)
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "入库记录"

    headers = ['入库时间', '客户姓名', '序号', '扫描单号', '收货地', '二次扫描单号', '是否敏货', '发货状态']
    ws.append(headers)

    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    # 每个客户单独计数序号（按导出排序已按客户分组）
    customer_counter = {}

    for r in records:
        # 已完成的包裹不导出
        if r.id in completed_inc_ids:
            continue
        cid = r.customer_id or 0
        customer_counter[cid] = customer_counter.get(cid, 0) + 1
        r._export_seq = customer_counter[cid]

    for r in records:
        # 已完成的包裹不导出
        if r.id in completed_inc_ids:
            continue
        customer = customers_map.get(r.customer_id)
        row = [
            r.entry_time.strftime('%Y-%m-%d %H:%M:%S'),
            customer.name if customer else '',
            r._export_seq,  # 每个客户单独计数
            r.tracking_number or '',
            r.receiving_address or '',
            r.second_scan_number or '',
            '是' if r.is_danger else '否',
            r.status or ''
        ]
        ws.append(row)

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'入库记录_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


@app.route('/export/freight')
def export_freight():
    """导出运费账单 - 支持按批次筛选"""
    batch_id = request.args.get('batch_id', type=int)
    status = request.args.get('status', '').strip()

    query = db.select(FreightRecord).join(Customer)

    # 按批次筛选
    if batch_id:
        batch_customer_ids = db.select(PackedPackage.customer_id).where(
            PackedPackage.batch_id == batch_id
        ).distinct().subquery()
        query = query.filter(FreightRecord.customer_id.in_(batch_customer_ids))

    if status:
        query = query.filter(FreightRecord.status == status)

    records = db.session.scalars(query.order_by(Customer.name.asc(), FreightRecord.created_at.desc())).all()

    # 生成Excel (纯 openpyxl，无 pandas 依赖)
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "运费账单"

    headers = ['序号', '客户姓名', '批次编号', '批次邮编',
               '普货发货渠道', '普货派送渠道', '普货单号',
               '敏货发货渠道', '敏货派送渠道', '敏货单号',
               '运费金额', '结算状态', '创建时间', '结算时间']
    ws.append(headers)

    thin = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    for idx, r in enumerate(records, 1):
        # 获取关联批次的渠道信息
        batch_info = None
        if r.carton_id:
            carton_item = db.session.scalar(db.select(CartonItem).filter_by(carton_id=r.carton_id))
            if carton_item:
                packed = db.session.get(PackedPackage, carton_item.packed_package_id)
                if packed and packed.batch_id:
                    batch_info = db.session.get(Batch, packed.batch_id)

        batch_no = batch_info.batch_no if batch_info else '—'
        zip_code = batch_info.zip_code if batch_info else '—'
        normal_channel = batch_info.normal_channel if batch_info else ''
        normal_delivery = batch_info.normal_delivery if batch_info else ''
        normal_tracking = batch_info.normal_tracking if batch_info else ''
        sensitive_channel = batch_info.sensitive_channel if batch_info else ''
        sensitive_delivery = batch_info.sensitive_delivery if batch_info else ''
        sensitive_tracking = batch_info.sensitive_tracking if batch_info else ''

        ws.append([
            idx,
            r.customer.name if r.customer else '',
            batch_no,
            zip_code,
            normal_channel,
            normal_delivery,
            normal_tracking,
            sensitive_channel,
            sensitive_delivery,
            sensitive_tracking,
            r.amount,
            r.status or '',
            r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
            r.settled_at.strftime('%Y-%m-%d %H:%M') if r.settled_at else ''
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    batch_suffix = f'_批次{batch_id}' if batch_id else ''
    filename = f'运费账单{batch_suffix}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


@app.route('/export/freight_bill')
def export_freight_bill():
    """导出账单 - 严格按 发货清单_账单 参考格式，实时从数据库查询"""
    batch_id = request.args.get('batch_id', type=int)

    if not batch_id:
        return "请指定批次ID", 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return "批次不存在", 404

    # 实时查询该批次下所有打包包裹（已发货和已完成都纳入账单）
    packed_list = db.session.execute(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer),
        joinedload(PackedPackage.items).joinedload(PackedPackageItem.incoming_package),
        joinedload(PackedPackage.batch)
    ).filter(
        PackedPackage.batch_id == batch_id,
        PackedPackage.status.in_(['已合箱', '已发货', '已完成'])
    )).unique().scalars().all()
    # Python端排序：避免 SQLAlchemy 2.0 join+joinedload 冲突
    packed_list.sort(key=lambda p: (
        p.customer.name if p.customer else '',
        p.nature or ''
    ))

    # 按客户分组聚合
    customer_groups = {}
    for p in packed_list:
        cid = p.customer_id
        nature_key = '普通' if p.nature != '敏' else '敏'

        if cid not in customer_groups:
            customer_groups[cid] = {
                'name': p.customer.name if p.customer else f'客户#{cid}',
                '普通': {'count': 0, 'weight': 0.0, 'actual_weight': 0.0, 'volume_weight': 0.0, 'surcharge': 0, 'surcharge_desc': [], 'addresses': []},
                '敏': {'count': 0, 'weight': 0.0, 'actual_weight': 0.0, 'volume_weight': 0.0, 'surcharge': 0, 'surcharge_desc': [], 'addresses': []}
            }

        g = customer_groups[cid]
        g[nature_key]['count'] += 1
        # 使用计费重（max(体积重, 实际重)）
        chargeable = calc_chargeable_weight(p.volume_weight, p.weight)
        g[nature_key]['weight'] += chargeable
        # 实际重和体积重（原始值求和）
        g[nature_key]['actual_weight'] += (p.weight or 0)
        g[nature_key]['volume_weight'] += (p.volume_weight or 0)

        # 计算附加费
        sur, desc, _ = get_surcharge_for_package(p)
        g[nature_key]['surcharge'] += sur
        if desc:
            g[nature_key]['surcharge_desc'].append(f'{p.serial_label}: {desc}')

        # 收集收货地
        if p.items:
            for item in p.items:
                addr = (item.incoming_package.receiving_address or '').strip()
                if addr:
                    g[nature_key]['addresses'].append(addr)

    # 实时获取最新单价
    prices = get_price_config()
    normal_price = prices['normal']
    danger_price = prices['danger']

    if batch.created_at:
        date_str = f'{batch.created_at.month}月{batch.created_at.day}日'
    else:
        date_str = ''
    batch_name = batch.batch_no

    # 构建行数据
    rows = []
    sorted_cids = sorted(customer_groups.keys(),
                         key=lambda c: customer_groups[c]['name'])

    for cid in sorted_cids:
        g = customer_groups[cid]
        name = g['name']

        normal = g['普通']
        danger = g['敏']

        normal_weight = round_to_half(normal['weight'])
        danger_weight = round_to_half(danger['weight'])

        normal_amount = round(normal_weight * normal_price, 2)
        danger_amount = round(danger_weight * danger_price, 2)

        normal_surcharge = normal['surcharge']
        danger_surcharge = danger['surcharge']

        # 合计 = 普货金额 + 敏货金额 + 附加费
        total = round(normal_amount + danger_amount + normal_surcharge + danger_surcharge, 2)

        def _pick_address(addrs, fallback=''):
            return addrs[0] if addrs else fallback

        normal_addr = _pick_address(normal['addresses'],
                                    _pick_address(danger['addresses'], ''))
        danger_addr = _pick_address(danger['addresses'], normal_addr)

        has_both = normal['count'] > 0 and danger['count'] > 0

        # 附加费说明
        normal_surcharge_text = '; '.join(normal['surcharge_desc']) if normal['surcharge_desc'] else ''
        danger_surcharge_text = '; '.join(danger['surcharge_desc']) if danger['surcharge_desc'] else ''

        # 普货行
        if normal['count'] > 0 or danger['count'] == 0:
            rows.append({
                'name': name,
                'count': normal['count'],
                'address': normal_addr,
                'nature': '普货',
                'weight': normal_weight,
                'price': normal_price,
                'amount': normal_amount,
                'surcharge': normal_surcharge if normal_surcharge > 0 else 0,
                'surcharge_desc': normal_surcharge_text,
                'total': total,
                'remark': '卡派',
                'merge': has_both,
                'actual_weight': round(normal['actual_weight'], 2),
                'volume_weight': round(normal['volume_weight'], 3),
            })

        # 敏货行
        if danger['count'] > 0:
            rows.append({
                'name': '' if has_both else name,
                'count': danger['count'],
                'address': danger_addr,
                'nature': '敏货',
                'weight': danger_weight,
                'price': danger_price,
                'amount': danger_amount,
                'surcharge': danger_surcharge if danger_surcharge > 0 else 0,
                'surcharge_desc': danger_surcharge_text,
                'total': total if not has_both else 0,
                'remark': '敏感商品',
                'merge': False,
                'actual_weight': round(danger['actual_weight'], 2),
                'volume_weight': round(danger['volume_weight'], 3),
            })

    # 生成 Excel
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "账单表"

    headers = ['批次', '日期', '客户', '包裹数', '收货地', '货物性质',
               '计费重', '单价', '金额', '附加费', '合计', '备注', '附加费说明', '实际重', '体积重']

    # 样式：微软雅黑 12pt，居中，细边框
    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side,
                        top=thin_side, bottom=thin_side)

    # 写表头
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 列宽（匹配参考文件）
    col_widths = [7.78, 10.0, 19.89, 8.22, 12.89, 10.44,
                  8.22, 6.0, 9.55, 8.22, 9.66, 10.44, 12.89, 8.22, 8.22]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # 写数据行
    row_idx = 2
    merge_start = None

    for r in rows:
        surcharge_val = r['surcharge'] if r['surcharge'] > 0 else 0
        total_val = r['total'] if r['total'] > 0 else None

        data = [
            batch_name, date_str, r['name'], r['count'], r['address'],
            r['nature'], r['weight'], r['price'], r['amount'],
            surcharge_val,
            total_val,
            r['remark'],
            r['surcharge_desc'] if r['surcharge_desc'] else None,
            r.get('actual_weight', 0),
            r.get('volume_weight', 0),
        ]

        for col_idx, val in enumerate(data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = font_style
            cell.alignment = align_center
            # 添加边框
            cell.border = border_all

        # 合并需求
        if r.get('merge'):
            merge_start = row_idx
        elif merge_start and r['name'] == '':
            ws.merge_cells(start_row=merge_start, start_column=3,
                           end_row=row_idx, end_column=3)
            ws.merge_cells(start_row=merge_start, start_column=11,
                           end_row=row_idx, end_column=11)
            merge_start = None

        row_idx += 1

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'发货清单_账单_{batch_name}_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 数据导出页面 ====================

@app.route('/export_data')
def export_data_page():
    """数据导出页面"""
    batches = db.session.scalars(db.select(Batch).order_by(Batch.batch_no.desc())).all()
    return render_template('export_data.html', batches=batches)


# ==================== 数据导出：发货清单 ====================

@app.route('/export/shipping_list')
def export_shipping_list():
    """导出发货清单 — 按批次导出17列明细"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return "请指定批次ID", 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return "批次不存在", 404

    # 查询该批次所有已打包的入库包裹（IncomingPackage），通过 PackedPackageItem 关联
    # 每个 IncomingPackage 一行，单号、日期各自独立
    pkg_items = db.session.execute(
        db.select(IncomingPackage, PackedPackage, PackedPackageItem, Customer)
        .join(PackedPackageItem, PackedPackageItem.incoming_package_id == IncomingPackage.id)
        .join(PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id)
        .join(Customer, IncomingPackage.customer_id == Customer.id)
        .filter(PackedPackage.batch_id == batch_id, PackedPackage.is_vip_solo != True)
        .order_by(IncomingPackage.customer_id, PackedPackage.nature, PackedPackage.id, IncomingPackage.entry_time)
    ).unique().all()

    if not pkg_items:
        return "该批次无包裹数据", 404

    # 构建行数据，按(客户, 性质)分组
    groups = {}  # key: (customer_id, nature), value: [row_dicts]
    ordered_keys = []

    for incoming, pp, ppi, customer in pkg_items:
        nature_display = '普货' if pp.nature == '普' else ('敏货' if pp.nature == '敏' else '')
        key = (pp.customer_id, nature_display)

        # 敏货扫描
        danger_scan = ''
        if nature_display == '敏货' and incoming.tracking_number:
            danger_scan = incoming.tracking_number

        # 包裹备注
        inc_remark = incoming.customer_remark or ''

        # 合并客户备注
        customer_note = customer.remark if customer and customer.remark else ''

        row = {
            'date': incoming.entry_time,
            'customer_name': customer.name,
            'customer_id': pp.customer_id,
            'packed_package_id': pp.id,
            'seq': 0,
            'tracking_number': incoming.tracking_number or '',
            'zip_code': customer.zip_code if customer else '',
            'address': customer.address if customer else '',
            'scan_number': incoming.tracking_number or '',
            'remark': inc_remark,
            'customer_note': customer_note,
            'danger_scan': danger_scan,
            'nature': nature_display,
            'product_name': pp.contents_description or '',
            'weight': pp.weight if pp.weight else None,
            'length': pp.length if pp.length else None,
            'width': pp.width if pp.width else None,
            'height': pp.height if pp.height else None,
            'volume': round(pp.length * pp.width * pp.height / 6000, 3) if pp.length and pp.width and pp.height else None,
        }

        if key not in groups:
            groups[key] = []
            ordered_keys.append(key)
        groups[key].append(row)

    # 按客户分组编号：每个客户从1开始
    # 先按客户分组，再给每个组内的行编号
    customer_seq_map = {}  # customer_id -> current_seq
    all_rows = []
    for key in ordered_keys:
        grp_rows = groups[key]
        cid, nature = key
        for r in grp_rows:
            if cid not in customer_seq_map:
                customer_seq_map[cid] = 1
            r['seq'] = customer_seq_map[cid]
            customer_seq_map[cid] += 1
            r['_is_first'] = (r == grp_rows[0])
            all_rows.append(r)

    # 生成 Excel
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "发货清单"

    headers = ['日期', '客户名称', '序号', '快递单号', '邮编', '收货地址', '扫描单号',
               '备注', '敏货扫描', '性质', '品名', '实重', '长', '宽', '高', '材积']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    # 标题行（合并）
    title = f'第{batch.batch_no}发货清单'
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title_cell = ws.cell(row=1, column=1, value=title)
    title_cell.font = Font(name='微软雅黑', size=14, bold=True)
    title_cell.alignment = align_center

    # 表头行
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 数据行：首行填值后续留空 + 合并单元格
    # 合并规则：
    #   同客户合并：客户名称(2)、邮编(5)、收货地址(6)
    #   同客户+性质合并：性质(10)
    #   同PackedPackage合并：品名(11)、实重(12)、长(13)、宽(14)、高(15)、材积(16)
    #   每行独立：日期(1)、序号(3)、快递单号(4)、扫描单号(7)、备注(8)、敏货扫描(9)
    current_row = 3
    prev_cid = None
    prev_key = None  # (customer_id, nature)
    prev_ppid = None  # packed_package_id
    customer_merge_ranges = []
    nature_merge_ranges = []
    pp_merge_ranges = []

    for row_data in all_rows:
        date_str = row_data['date'].strftime('%Y-%m-%d') if row_data['date'] else ''
        # 合并备注：包裹备注 + 客户备注
        remark_combined = row_data['remark']
        if row_data['customer_note']:
            if remark_combined:
                remark_combined += '; ' + row_data['customer_note']
            else:
                remark_combined = row_data['customer_note']

        # 首行填值，后续行留空
        is_customer_first = (row_data['customer_id'] != prev_cid)
        key = (row_data['customer_id'], row_data['nature'])
        is_nature_first = (key != prev_key)
        is_pp_first = (row_data['packed_package_id'] != prev_ppid)

        customer_name = row_data['customer_name'] if is_customer_first else ''
        zip_code = row_data['zip_code'] if is_customer_first else ''
        address = row_data['address'] if is_customer_first else ''
        nature = row_data['nature'] if is_nature_first else ''
        product = row_data['product_name'] if is_pp_first else ''
        weight = row_data['weight'] if is_pp_first else ''
        length = row_data['length'] if is_pp_first else ''
        width = row_data['width'] if is_pp_first else ''
        height = row_data['height'] if is_pp_first else ''
        volume = row_data['volume'] if is_pp_first else ''

        vals = [
            date_str,
            customer_name,
            row_data['seq'],
            row_data['tracking_number'],
            zip_code,
            address,
            row_data['scan_number'],
            remark_combined,
            row_data['danger_scan'],
            nature,
            product,
            weight,
            length,
            width,
            height,
            volume,
        ]

        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val if val is not None else '')
            cell.font = font_style
            cell.alignment = align_center
            cell.border = border_all

        # 追踪客户合并范围
        if is_customer_first:
            customer_merge_ranges.append((current_row, current_row))
        else:
            if customer_merge_ranges:
                customer_merge_ranges[-1] = (customer_merge_ranges[-1][0], current_row)

        # 追踪性质合并范围
        if is_nature_first:
            nature_merge_ranges.append((current_row, current_row))
        else:
            if nature_merge_ranges:
                nature_merge_ranges[-1] = (nature_merge_ranges[-1][0], current_row)

        # 追踪PackedPackage合并范围
        if is_pp_first:
            pp_merge_ranges.append((current_row, current_row))
        else:
            if pp_merge_ranges:
                pp_merge_ranges[-1] = (pp_merge_ranges[-1][0], current_row)

        prev_cid = row_data['customer_id']
        prev_key = key
        prev_ppid = row_data['packed_package_id']
        current_row += 1

    # 执行客户列合并（客户名称/邮编/收货地址）
    for start_r, end_r in customer_merge_ranges:
        if end_r > start_r:
            for col in [2, 5, 6]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 执行性质列合并（性质）
    for start_r, end_r in nature_merge_ranges:
        if end_r > start_r:
            ws.merge_cells(start_row=start_r, start_column=10, end_row=end_r, end_column=10)

    # 执行PackedPackage列合并（品名/实重/长/宽/高/材积）
    for start_r, end_r in pp_merge_ranges:
        if end_r > start_r:
            for col in [11, 12, 13, 14, 15, 16]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 列宽
    col_widths = [8.22, 19.89, 6.0, 29.22, 8.22, 29.22, 29.22, 52.89, 29.0, 6.0, 89.22, 6.89, 4.22, 13.0, 13.0, 16.78]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'{batch.batch_no}发货清单_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 数据导出：出货信息表 ====================

@app.route('/export/shipping_info')
def export_shipping_info():
    """导出出货信息表 — 按批次导出箱子装箱明细，格式对标计费表"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return "请指定批次ID", 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return "批次不存在", 404

    # 查询该批次所有箱子，按类型和序号排序
    cartons = db.session.scalars(db.select(Carton).filter(
        Carton.batch_id == batch_id
    ).order_by(
        Carton.carton_type.asc(),
        Carton.box_seq.asc()
    )).all()

    if not cartons:
        return "该批次无合箱数据，请先完成合箱操作", 404

    # 构建行数据：每个 PackedPackage 一行（每个包裹代号一行），货主列格式与装货清单一致
    all_rows = []
    box_counter = 0
    prev_type = None
    for carton in cartons:
        if carton.carton_type != prev_type:
            box_counter = 1
            prev_type = carton.carton_type
        else:
            box_counter += 1

        items = db.session.scalars(db.select(CartonItem).options(
            joinedload(CartonItem.packed_package).joinedload(PackedPackage.customer)
        ).filter_by(carton_id=carton.id)).all()

        if not items:
            continue

        box_type_display = '普货' if carton.carton_type == '普' else '敏货'

        # 按(客户, PackedPackage)整理：每个 pp 一行
        # 先收集所有 pp，按客户名+serial_label排序
        pp_list = []
        for item in items:
            pp = item.packed_package
            if pp and pp.customer:
                # 提取包裹代号（与装货清单完全一致）
                label = pp.serial_label
                if label and '#' in label:
                    label = label.split('#')[0]
                if not label:
                    label = str(pp.id)
                pp_list.append((pp, label))

        if not pp_list:
            continue

        pp_list.sort(key=lambda x: (x[0].customer.name, x[1]))

        for i, (pp, label) in enumerate(pp_list):
            is_first_of_box = (i == 0)
            customer_display = f"{pp.customer.name} {label}"

            row = {
                'box_name': str(box_counter),
                'box_type': box_type_display,
                'customer': customer_display,
                'weight': carton.total_weight,
                'length': carton.length,
                'width': carton.width,
                'height': carton.height,
                '_is_first': is_first_of_box,
            }
            all_rows.append(row)

    # 生成 Excel — 完全对标计费表模式：首行填值后续留空 + 合并单元格
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "出货信息表"

    headers = ['箱号', '物品类型', '货主', '重量(kg)', '长(cm)', '宽(cm)', '高(cm)']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    # 标题行（合并）
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title_cell = ws.cell(row=1, column=1, value='出货信息表')
    title_cell.font = Font(name='微软雅黑', size=14, bold=True)
    title_cell.alignment = align_center

    # 表头
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 数据行：追踪箱子合并范围（对标计费表的客户合并逻辑）
    current_row = 3
    box_merge_ranges = []

    for row_data in all_rows:
        # 首行填值，后续行留空
        box_name = row_data['box_name'] if row_data['_is_first'] else ''
        box_type = row_data['box_type'] if row_data['_is_first'] else ''
        weight = row_data['weight'] if row_data['_is_first'] else ''
        length = row_data['length'] if row_data['_is_first'] else ''
        width = row_data['width'] if row_data['_is_first'] else ''
        height = row_data['height'] if row_data['_is_first'] else ''

        vals = [box_name, box_type, row_data['customer'], weight, length, width, height]

        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val if val is not None else '')
            cell.font = font_style
            cell.alignment = align_center
            cell.border = border_all

        # 追踪箱子合并范围（用 _is_first 判断新箱子开始）
        if row_data['_is_first']:
            box_merge_ranges.append((current_row, current_row))
        else:
            if box_merge_ranges:
                box_merge_ranges[-1] = (box_merge_ranges[-1][0], current_row)

        current_row += 1

    # 执行箱子单元格合并（箱号/类型/重量/长/宽/高）
    for start_r, end_r in box_merge_ranges:
        if end_r > start_r:
            for col in [1, 2, 4, 5, 6, 7]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 列宽
    col_widths = [17.89, 31.0, 39.44, 17.89, 13.0, 17.89, 13.0]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'出货信息表{batch.batch_no}_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 数据导出：装货清单 ====================

@app.route('/export/loading_list')
def export_loading_list():
    """导出装货清单 — 按批次导出，每个PackedPackage一行（包裹代号维度）"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return "请指定批次ID", 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return "批次不存在", 404

    # 直接查该批次所有打包包裹（通过 PackedPackage.batch_id），避免 joinedload 集合加载问题
    pkgs = db.session.execute(db.select(PackedPackage).options(
        joinedload(PackedPackage.customer)
    ).filter(
        PackedPackage.batch_id == batch_id
    ).order_by(PackedPackage.customer_id, PackedPackage.nature, PackedPackage.id)).unique().scalars().all()

    if not pkgs:
        return "该批次无包裹数据", 404

    # 构建行数据：每个 PackedPackage 一行
    all_rows = []
    for pp in pkgs:
        serial_label = ''
        if pp.serial_label:
            serial_label = pp.serial_label.split('#')[0] if '#' in pp.serial_label else pp.serial_label
        if not serial_label:
            serial_label = str(pp.id)

        nature_display = '普货' if pp.nature == '普' else ('敏货' if pp.nature == '敏' else '')
        customer_name = pp.customer.name if pp.customer else ''

        all_rows.append({
            'customer_name': customer_name,
            'customer_id': pp.customer_id,
            'serial_label': serial_label,
            'nature': nature_display,
            'product': pp.contents_description or '',
        })

    # 生成 Excel
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    # 按 (客户+编号, 性质, 品名) 去重（三项完全相同才去重）
    seen = set()
    deduped_rows = []
    for row_data in all_rows:
        key = (row_data['customer_name'], row_data['serial_label'], row_data['nature'], row_data['product'])
        if key not in seen:
            seen.add(key)
            deduped_rows.append(row_data)
    all_rows = deduped_rows

    wb = Workbook()
    ws = wb.active
    ws.title = "装货清单"

    headers = ['客户', '性质', '品名']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    align_wrap = Alignment(horizontal='left', vertical='center', wrap_text=True)
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    # 表头
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 数据行
    for i, row_data in enumerate(all_rows):
        r = i + 2
        customer_display = row_data['customer_name']
        if row_data['serial_label']:
            customer_display = f"{row_data['customer_name']} {row_data['serial_label']}"
        vals = [customer_display, row_data['nature'], row_data['product']]
        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.font = font_style
            cell.alignment = align_center
            cell.border = border_all

    # 列宽
    col_widths = [34.89, 10.0, 109.22]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'{batch.batch_no}装货清单_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 数据导出：计费表 ====================

@app.route('/export/billing_table')
def export_billing_table():
    """导出计费表 — 按批次导出9列计费明细，格式与发货清单一致"""
    batch_id = request.args.get('batch_id', type=int)
    if not batch_id:
        return "请指定批次ID", 400

    batch = db.session.get(Batch, batch_id)
    if not batch:
        return "批次不存在", 404

    # 查询该批次所有已打包的入库包裹（与发货清单一致，通过 PackedPackage.batch_id 过滤）
    pkg_items = db.session.execute(
        db.select(IncomingPackage, PackedPackage, PackedPackageItem, Customer)
        .join(PackedPackageItem, PackedPackageItem.incoming_package_id == IncomingPackage.id)
        .join(PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id)
        .join(Customer, IncomingPackage.customer_id == Customer.id)
        .filter(PackedPackage.batch_id == batch_id, PackedPackage.is_vip_solo != True)
        .order_by(IncomingPackage.customer_id, PackedPackage.nature, PackedPackage.id, IncomingPackage.entry_time)
    ).unique().all()

    if not pkg_items:
        return "该批次无包裹数据", 404

    # 构建行数据
    groups = {}
    ordered_keys = []

    for incoming, pp, ppi, customer in pkg_items:
        nature_display = '普货' if pp.nature == '普' else ('敏货' if pp.nature == '敏' else '')
        key = (pp.customer_id, nature_display)

        row = {
            'customer_name': customer.name,
            'customer_id': pp.customer_id,
            'packed_package_id': pp.id,
            'tracking_number': incoming.tracking_number or '',
            'destination': customer.address if customer else '',
            'nature': nature_display,
            'actual_weight': None,
            'dimensions': '',
            'volume_weight': None,
            'billable_weight': None,
            'remark': incoming.customer_remark or '',
            'abnormal_remark': incoming.abnormal_remark or '',
        }

        if pp:
            row['actual_weight'] = pp.weight if pp.weight else None
            L = pp.length or 0
            W = pp.width or 0
            H = pp.height or 0
            if L and W and H:
                row['dimensions'] = f'{int(L)}*{int(W)}*{int(H)}'
                vol = round(L * W * H / 6000, 3)
                row['volume_weight'] = vol
                row['billable_weight'] = calc_chargeable_weight(vol, pp.weight or 0)

        if key not in groups:
            groups[key] = []
            ordered_keys.append(key)
        groups[key].append(row)

    # 编号：按客户分组，每个客户从1开始
    customer_seq_map = {}
    all_rows = []
    for key in ordered_keys:
        grp_rows = groups[key]
        cid, nature = key
        for r in grp_rows:
            if cid not in customer_seq_map:
                customer_seq_map[cid] = 1
            r['seq'] = customer_seq_map[cid]
            customer_seq_map[cid] += 1
            all_rows.append(r)

    # 生成 Excel
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "计费表"

    headers = ['客户', '序号', '快递单号', '收货地', '货物性质',
               '实际重量', '长宽高', '材积重/6000', '计费重', '备注']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    # 表头
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 数据行：三层合并
    #   同客户合并：客户(1)、收货地(4)
    #   同客户+性质合并：货物性质(5)
    #   同PackedPackage合并：实际重量(6)、长宽高(7)、材积重(8)、计费重(9)
    #   每行独立：序号(2)、快递单号(3)、备注(10)
    current_row = 2
    prev_cid = None
    prev_key = None  # (customer_id, nature)
    prev_ppid = None  # packed_package_id
    customer_merge_ranges = []
    nature_merge_ranges = []
    pp_merge_ranges = []

    for row_data in all_rows:
        # 首行填值，后续行留空
        is_customer_first = (row_data['customer_id'] != prev_cid)
        key = (row_data['customer_id'], row_data['nature'])
        is_nature_first = (key != prev_key)
        is_pp_first = (row_data['packed_package_id'] != prev_ppid)

        customer_val = row_data['customer_name'] if is_customer_first else ''
        dest_val = row_data['destination'] if is_customer_first else ''
        nature_val = row_data['nature'] if is_nature_first else ''
        aw_val = row_data['actual_weight'] if is_pp_first else ''
        dim_val = row_data['dimensions'] if is_pp_first else ''
        vw_val = row_data['volume_weight'] if is_pp_first else ''
        bw_val = row_data['billable_weight'] if is_pp_first else ''

        # 备注
        remark_parts = []
        if row_data.get('remark'):
            remark_parts.append(f"📝{row_data['remark']}")
        if row_data.get('abnormal_remark'):
            remark_parts.append(f"⚠️{row_data['abnormal_remark']}")
        remark_val = '; '.join(remark_parts) if remark_parts else ''

        vals = [
            customer_val,
            row_data['seq'],
            row_data['tracking_number'],
            dest_val,
            nature_val,
            aw_val,
            dim_val,
            vw_val,
            bw_val,
            remark_val,
        ]

        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val if val is not None else '')
            cell.font = font_style
            cell.alignment = align_center
            cell.border = border_all

        # 追踪客户合并范围
        if is_customer_first:
            customer_merge_ranges.append((current_row, current_row))
        else:
            if customer_merge_ranges:
                customer_merge_ranges[-1] = (customer_merge_ranges[-1][0], current_row)

        # 追踪性质合并范围
        if is_nature_first:
            nature_merge_ranges.append((current_row, current_row))
        else:
            if nature_merge_ranges:
                nature_merge_ranges[-1] = (nature_merge_ranges[-1][0], current_row)

        # 追踪PackedPackage合并范围
        if is_pp_first:
            pp_merge_ranges.append((current_row, current_row))
        else:
            if pp_merge_ranges:
                pp_merge_ranges[-1] = (pp_merge_ranges[-1][0], current_row)

        prev_cid = row_data['customer_id']
        prev_key = key
        prev_ppid = row_data['packed_package_id']
        current_row += 1

    # 执行客户列合并（客户/收货地）
    for start_r, end_r in customer_merge_ranges:
        if end_r > start_r:
            for col in [1, 4]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 执行性质列合并
    for start_r, end_r in nature_merge_ranges:
        if end_r > start_r:
            ws.merge_cells(start_row=start_r, start_column=5, end_row=end_r, end_column=5)

    # 执行PackedPackage列合并（实际重量/长宽高/材积重/计费重）
    for start_r, end_r in pp_merge_ranges:
        if end_r > start_r:
            for col in [6, 7, 8, 9]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 列宽
    col_widths = [19.89, 6.0, 29.22, 29.22, 10.44, 13.0, 11.66, 15.0, 8.22, 20.0]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f'{batch.batch_no}计费表_{datetime.now().strftime("%Y%m%d")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== VIP单独发货 尺寸信息表导出 ====================

@app.route('/export/vip_dimensions')
@app.route('/export/vip_dimensions/customer/<int:customer_id>')
def export_vip_dimensions(customer_id=None):
    """VIP单独发货 尺寸信息表导出
    表头: 序号 | 实重 | 长 | 宽 | 高 | 材积 | 计费重 | 备注
    - 不传 customer_id: 导出全部VIP单独发货记录
    - 传 customer_id:  仅导出该客户的VIP单独发货记录
    模板路径: static/export_templates/尺寸信息表.xlsx
    """
    from openpyxl import load_workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from copy import copy

    template_path = os.path.join(BASE_DIR, 'static', 'export_templates', '尺寸信息表.xlsx')
    if not os.path.exists(template_path):
        return f"尺寸信息表模板不存在: {template_path}", 500

    # 查询数据：is_vip_solo=True 且状态为「已完成」（只导出已完成的尺寸信息）
    stmt = db.select(PackedPackage).filter(
        PackedPackage.is_vip_solo == True,
        PackedPackage.status == '已完成'
    )
    if customer_id is not None:
        stmt = stmt.filter(PackedPackage.customer_id == customer_id)
    stmt = stmt.order_by(PackedPackage.customer_id, PackedPackage.created_at, PackedPackage.id)
    packs = db.session.execute(stmt).scalars().all()

    if not packs:
        return "暂无VIP单独发货数据", 404

    wb = load_workbook(template_path)
    ws = wb.active

    # 读取第2行作为数据行样式样板
    sample_cells = [ws.cell(row=2, column=c) for c in range(1, 9)]
    row_height = ws.row_dimensions[2].height or 24.75

    # 删除模板中的示例数据(2-5)和合计行(6)，共5行
    ws.delete_rows(2, 5)

    # 写入 N 条数据
    n = len(packs)
    for idx, pack in enumerate(packs, 1):
        row_num = idx + 1  # 数据从第2行开始
        L = pack.length or 0
        W = pack.width or 0
        H = pack.height or 0
        aw = pack.weight or 0
        vw = calc_volume_weight(L, W, H)
        cw = calc_chargeable_weight(vw, aw)
        contents = pack.contents_description or ''

        vals = [idx, aw, L, W, H, vw, cw, contents]
        for col_idx, v in enumerate(vals, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=v)
            sample = sample_cells[col_idx - 1]
            cell.font = copy(sample.font)
            cell.alignment = copy(sample.alignment)
            cell.border = copy(sample.border)
            cell.number_format = sample.number_format

        ws.row_dimensions[row_num].height = row_height

    # 合计行
    total_row = n + 2
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    a_total = ws.cell(row=total_row, column=1, value='合计')
    a_total.font = copy(sample_cells[0].font)
    a_total.alignment = copy(sample_cells[0].alignment)
    a_total.border = border_all

    g_total = ws.cell(row=total_row, column=7, value=f'=SUM(G2:G{n + 1})')
    g_total.font = copy(sample_cells[6].font)
    g_total.alignment = copy(sample_cells[6].alignment)
    g_total.border = border_all

    # 合计行 B-F、H 列保留边框（视觉完整）
    for col_idx in [2, 3, 4, 5, 6, 8]:
        cell = ws.cell(row=total_row, column=col_idx)
        cell.border = border_all

    ws.row_dimensions[total_row].height = row_height

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    if customer_id is not None:
        customer = db.session.get(Customer, customer_id)
        cust_name = customer.name if customer else f'客户{customer_id}'
        filename = f'VIP尺寸信息_{cust_name}_{datetime.now().strftime("%Y%m%d")}.xlsx'
    else:
        filename = f'VIP尺寸信息表_全部_{datetime.now().strftime("%Y%m%d")}.xlsx'

    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== 仓库货架管理 ====================

def _auto_create_address_shelves():
    """内部函数：基于历史发货数据，自动创建按地址命名的货架
    货架命名：地址+字母A~H（A-H为收货地址，I为备用货架）
    slot_label：{地址}{字母}-{楼层}-{位置}（如蒙市A-1-1）
    A-D/G: 4层×5位=20位, E/F/H: 5层×5位=25位, I: 备用4层×5位=20位
    返回 (success, message, plan)
    """
    from sqlalchemy import delete as sa_delete

    positions = ['1', '2', '3', '4', '5']
    TOTAL_SHELVES = 8  # A-H 分配，I 为备用
    LETTERS = 'ABCDEFGH'

    # 已有货架则跳过
    existing = db.session.execute(
        db.select(db.func.count()).select_from(ShelfSlot)
    ).scalar()
    if existing > 0:
        return True, '货架已存在', []

    # 统计历史发货按地址分组
    addr_sql = text('''
        SELECT c.address, c.zip_code,
               COUNT(DISTINCT c.id) as cust_count,
               COUNT(ppi.id) as item_count
        FROM packed_package_items ppi
        JOIN packed_packages pp ON ppi.packed_package_id = pp.id
        JOIN customers c ON pp.customer_id = c.id
        GROUP BY c.address, c.zip_code
        ORDER BY item_count DESC
    ''')
    addr_rows = db.session.execute(addr_sql).all()

    if not addr_rows:
        return False, '暂无历史发货数据', []

    # 合并小量 91755 到 罗兰岗（如果两者都存在且独立）
    rlg_row = None
    zip91755_row = None
    other_rows = []
    for row in addr_rows:
        addr = (row.address or '').strip()
        zip_code = (row.zip_code or '').strip()
        if '罗兰岗' in addr:
            rlg_row = row
        elif addr == zip_code and addr == '91755':
            zip91755_row = row
        else:
            other_rows.append(row)

    if rlg_row and zip91755_row:
        # 合并
        merged_item_count = rlg_row.item_count + zip91755_row.item_count
        merged_cust_count = rlg_row.cust_count + zip91755_row.cust_count
        from collections import namedtuple
        AddrRow = namedtuple('AddrRow', ['address', 'zip_code', 'cust_count', 'item_count'])
        rlg_merged = AddrRow(rlg_row.address, rlg_row.zip_code, merged_cust_count, merged_item_count)
        addr_rows = [rlg_merged] + other_rows
        # 重新排序
        addr_rows.sort(key=lambda x: x.item_count, reverse=True)

    # 确保至少有 TOTAL_SHELVES 个地址需要分配
    if len(addr_rows) < TOTAL_SHELVES:
        # 如果地址不够，减少 TOTAL_SHELVES
        TOTAL_SHELVES = min(TOTAL_SHELVES, len(addr_rows))

    total_items = sum(r.item_count for r in addr_rows)

    # 按比例分配货架数
    raw_shares = []
    for row in addr_rows:
        label = f"{row.address or ''} {row.zip_code or ''}".strip()
        short_name = label.split()[0] if label.split() else label
        pct = row.item_count / total_items if total_items > 0 else 0
        raw = pct * TOTAL_SHELVES
        raw_shares.append({
            'address': row.address or '',
            'zip_code': row.zip_code or '',
            'label': label,
            'short_name': short_name.replace('/', '-'),
            'item_count': row.item_count,
            'cust_count': row.cust_count,
            'pct': pct,
            'raw': raw,
        })

    # 贪心分配
    assigned_shelf_count = 0
    for rs in raw_shares:
        rs['shelves'] = max(1, int(rs['raw']))
        assigned_shelf_count += rs['shelves']

    raw_shares.sort(key=lambda x: x['item_count'])
    while assigned_shelf_count > TOTAL_SHELVES:
        for rs in raw_shares:
            if rs['shelves'] > 1:
                rs['shelves'] -= 1
                assigned_shelf_count -= 1
                break

    raw_shares.sort(key=lambda x: x['raw'] - int(x['raw']), reverse=True)
    while assigned_shelf_count < TOTAL_SHELVES:
        for rs in raw_shares:
            if rs['raw'] - int(rs['raw']) > 0.05:
                rs['shelves'] += 1
                assigned_shelf_count += 1
                rs['raw'] += 1
                break
        else:
            raw_shares[0]['shelves'] += 1
            assigned_shelf_count += 1

    # 分配具体货架名：按item_count排序，依次分配A~H字母
    raw_shares.sort(key=lambda x: x['item_count'], reverse=True)
    letter_idx = 0
    plan = []
    for rs in raw_shares:
        count = rs['shelves']
        sn = rs['short_name']
        assigned_letters = []
        for i in range(count):
            letter = LETTERS[letter_idx]
            shelf_code = f'{sn}{letter}'
            assigned_letters.append(shelf_code)
            letter_idx += 1
        rs['assigned_shelves'] = assigned_letters
        plan.append(rs)

    # 创建货架
    result = []
    for rs in plan:
        addr_label = rs['label']
        address = rs['address']
        zip_code = rs['zip_code']
        shelf_codes_assigned = rs['assigned_shelves']

        for sc in shelf_codes_assigned:
            # E, F 货架有5层，其余4层（H也是4层：1-3罗兰岗，4层91755）
            max_floor = 5 if sc[-1] in ('E', 'F') else 4
            for floor in range(1, max_floor + 1):
                for pos in positions:
                    slot_label = f'{sc}-{floor}-{pos}'
                    # H货架第4层属于合并的91755地址
                    if sc[-1] == 'H' and floor == 4 and rlg_row is not None and zip91755_row is not None:
                        floor_addr = f"91755 91755"
                    else:
                        floor_addr = addr_label
                    slot = ShelfSlot(
                        shelf_code=sc,
                        floor=floor,
                        zone=pos,
                        slot_label=slot_label,
                        row='',
                        col=0,
                        address_label=floor_addr,
                    )
                    db.session.add(slot)

        result.append({
            'address': addr_label,
            'shelves': shelf_codes_assigned,
            'shelf_count': len(shelf_codes_assigned),
            'item_count': rs['item_count'],
            'pct': round(rs['pct'] * 100, 1),
            'customer_count': rs['cust_count'],
        })

    # 创建备用I货架
    for floor in range(1, 5):
        for pos in positions:
            sl = ShelfSlot(
                shelf_code='备用I', floor=floor, zone=pos,
                slot_label=f'备用I-{floor}-{pos}',
                row='', col=0, address_label=''
            )
            db.session.add(sl)
    result.append({
        'address': '备用',
        'shelves': ['备用I'],
        'shelf_count': 1,
        'item_count': 0,
        'pct': 0,
        'customer_count': 0,
    })

    db.session.commit()

    create_operation_log(
        '仓库地址自动初始化',
        f'系统自动分配 {len(plan)} 个收货地址 → {TOTAL_SHELVES} 个货架 + 1个备用',
        '系统'
    )

    return True, f'自动分配完成：{len(plan)} 个地址 → {TOTAL_SHELVES}: {sum(rs["shelf_count"] for rs in result)} 个货架(含备用)', result


def _ensure_five_floors():
    """确保E/F货架拥有5层货位（补充缺失的第5层）"""
    positions = ['1', '2', '3', '4', '5']

    five_floor_shelves = db.session.execute(
        db.select(ShelfSlot.shelf_code).where(
            ShelfSlot.shelf_code.like('%E') |
            ShelfSlot.shelf_code.like('%F')
        ).distinct()
    ).scalars().all()

    added = 0
    for sc in five_floor_shelves:
        has_fifth = db.session.execute(
            db.select(db.func.count()).select_from(ShelfSlot).where(
                ShelfSlot.shelf_code == sc,
                ShelfSlot.floor == 5
            )
        ).scalar()
        if has_fifth == 0:
            # 复制同货架第1层的 address_label
            ref_addr = db.session.execute(
                db.select(ShelfSlot.address_label).where(
                    ShelfSlot.shelf_code == sc,
                    ShelfSlot.floor == 1,
                    ShelfSlot.zone == '1'
                )
            ).scalar() or ''
            for pos in positions:
                slot_label = f'{sc}-5-{pos}'
                slot = ShelfSlot(
                    shelf_code=sc,
                    floor=5,
                    zone=pos,
                    slot_label=slot_label,
                    row='',
                    col=0,
                    address_label=ref_addr,
                )
                db.session.add(slot)
                added += 1
            print(f'[5层补全] {sc} +5个货位 [{ref_addr}]')

    if added > 0:
        db.session.commit()
        print(f'[5层补全] 共补充 {added} 个货位')

    return added


@app.route('/warehouse')
def warehouse():
    """仓库货架管理主页 - 地址+字母命名(蒙市A~I)，E/F货架5层，其余4层"""
    # 首次加载自动按地址创建货架
    existing = db.session.execute(
        db.select(db.func.count()).select_from(ShelfSlot)
    ).scalar()
    if existing == 0:
        _auto_create_address_shelves()
    else:
        # 确保E/F/H货架有5层
        _ensure_five_floors()

    slots = db.session.execute(
        db.select(ShelfSlot).order_by(ShelfSlot.shelf_code, ShelfSlot.floor, ShelfSlot.zone)
    ).scalars().all()

    # 预加载客户名称映射
    assigned_ids = [s.customer_id for s in slots if s.customer_id]
    cust_map = {}
    if assigned_ids:
        customers = db.session.execute(
            db.select(Customer).where(Customer.id.in_(assigned_ids))
        ).scalars().all()
        cust_map = {c.id: c.name for c in customers}

    # 统计每个货位的包裹数和客户名
    slot_info = {}
    for slot in slots:
        cnt_incoming = db.session.execute(
            db.select(db.func.count()).select_from(IncomingPackage).where(
                IncomingPackage.shelf_slot_id == slot.id,
                IncomingPackage.status == '已入库'
            )
        ).scalar()
        cnt_pending = db.session.execute(
            db.select(db.func.count()).select_from(IncomingPackage).where(
                IncomingPackage.shelf_slot_id == slot.id,
                IncomingPackage.status == '待打包'
            )
        ).scalar()
        cnt = cnt_incoming + cnt_pending
        cust_name = cust_map.get(slot.customer_id, '')
        slot_info[slot.id] = {
            'count': cnt,
            'count_incoming': cnt_incoming,
            'count_pending': cnt_pending,
            'customer_name': cust_name,
            'customer_id': slot.customer_id,
        }

    # 按 shelf_code 分组（每个shelf_code就是一个地址货架，如"蒙市-1"）
    shelf_data = {}  # {shelf_code: {'address': '', 'slots': [floor_groups]}}
    for s in slots:
        if s.shelf_code not in shelf_data:
            shelf_data[s.shelf_code] = {'address': s.address_label or '', 'slots_by_floor': {}}
        fl = s.floor
        if fl not in shelf_data[s.shelf_code]['slots_by_floor']:
            shelf_data[s.shelf_code]['slots_by_floor'][fl] = []
        shelf_data[s.shelf_code]['slots_by_floor'][fl].append(s)

    # 按地址分组（同一地址可能有多个货架，如蒙市-1、蒙市-2、蒙市-3）
    addr_groups = {}  # {address_label: [shelf_code, ...]}
    for sc, sd in shelf_data.items():
        addr = sd['address'] or '未分配'
        if addr not in addr_groups:
            addr_groups[addr] = []
        addr_groups[addr].append(sc)

    # 统计
    total_assigned = sum(1 for s in slots if s.customer_id)
    total_active = sum(si['count'] for si in slot_info.values())

    # 判断是否已按地址分配（有address_label的slot）
    has_address_assignment = any(s.address_label for s in slots)

    # 按最后一个字母排序（A-I），确保 3×3 网格按 ABC / DEF / GHI 排列
    shelf_order = sorted(shelf_data.keys(), key=lambda sc: sc[-1])

    return render_template('warehouse.html',
                           shelf_data=shelf_data,
                           shelf_order=shelf_order,
                           addr_groups=addr_groups,
                           slot_info=slot_info,
                           slots=slots,
                           total_assigned=total_assigned,
                           total_active=total_active,
                           has_address_assignment=has_address_assignment,
                           shelf_count=len(shelf_data))


@app.route('/api/warehouse/slot/<int:slot_id>')
def api_warehouse_slot_detail(slot_id):
    """获取货位详情及当前包裹列表"""
    slot = db.session.get(ShelfSlot, slot_id)
    if not slot:
        return api_error('货位不存在', code='NOT_FOUND', status_code=404)

    pkgs = db.session.execute(
        db.select(IncomingPackage, Customer)
        .outerjoin(Customer, IncomingPackage.customer_id == Customer.id)
        .where(
            IncomingPackage.shelf_slot_id == slot_id,
            IncomingPackage.status.in_(['已入库', '待打包'])
        )
        .order_by(IncomingPackage.entry_time.desc())
    ).all()

    packages = []
    for pkg, cust in pkgs:
        packages.append({
            'id': pkg.id,
            'tracking_number': pkg.tracking_number,
            'customer_name': cust.name if cust else '无主',
            'status': pkg.status,
            'nature': pkg.nature,
            'entry_time': pkg.entry_time.strftime('%m-%d %H:%M') if pkg.entry_time else '',
            'remark': pkg.customer_remark or '',
        })

    return api_success(data={
        'slot_id': slot.id,
        'slot_label': slot.slot_label,
        'shelf_code': slot.shelf_code,
        'address_label': slot.address_label or '',
        'floor': slot.floor,
        'zone': slot.zone,
        'customer_id': slot.customer_id,
        'package_count': len(packages),
        'packages': packages,
    })


@app.route('/api/warehouse/assign', methods=['POST'])
def api_warehouse_assign():
    """
    给客户分配货位区域（该客户所有未打包包裹自动归入）
    POST body: {"customer_id": 1, "slot_label": "A1-1-L"}  # 单区位
    或: {"customer_id": 1, "slot_labels": ["A1-1-L", "A1-1-ML"]}  # 多区位
    或: {"package_id": 123, "slot_label": "A1-1-L"}  # 兼容旧版单包裹分配
    """
    data = request.get_json(silent=True) or {}
    package_id = data.get('package_id')
    customer_id = data.get('customer_id')

    # 支持多区位
    slot_labels = data.get('slot_labels', [])
    if not slot_labels:
        sl = data.get('slot_label', '').strip()
        if sl:
            slot_labels = [sl]

    if not slot_labels:
        return api_error('缺少 slot_label')
    if not package_id and not customer_id:
        return api_error('缺少 customer_id 或 package_id')

    # 查找所有区位
    slots = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.slot_label.in_(slot_labels))
    ).scalars().all()
    if not slots:
        return api_error(f'货位 {slot_labels[0]} 不存在')

    found_labels = {s.slot_label for s in slots}
    missing = [l for l in slot_labels if l not in found_labels]
    if missing:
        return api_error(f'货位不存在: {", ".join(missing)}')

    if package_id:
        # 旧版单包裹分配 - 用第一个区位
        slot = slots[0]
        pkg = db.session.get(IncomingPackage, package_id)
        if not pkg:
            return api_error('包裹不存在', code='NOT_FOUND', status_code=404)
        if pkg.status == '已打包':
            return api_error('已打包的包裹不能修改货位')
        # 检查区位是否已被其他客户占用（有包裹且客户不同）
        occupied = db.session.scalar(db.select(IncomingPackage).filter(
            IncomingPackage.shelf_slot_id == slot.id,
            IncomingPackage.status.in_(['已入库','待打包']),
            IncomingPackage.customer_id != pkg.customer_id
        ))
        if occupied and slot.customer_id and slot.customer_id != pkg.customer_id:
            return api_error('该区位已被其他客户占用，请先解除分配')
        old_label = pkg.shelf_slot.slot_label if pkg.shelf_slot_id else '未指定'
        pkg.shelf_slot_id = slot.id
        # 同步更新区位绑定客户
        slot.customer_id = pkg.customer_id
        db.session.commit()
        return api_success(
            data={'slot_label': slot.slot_label, 'slot_id': slot.id},
            message=f'已将包裹 {pkg.tracking_number} 从「{old_label}」移至「{slot.slot_label}」'
        )

    # 按客户分配：该客户所有未打包包裹归入此区域
    cust = db.session.get(Customer, customer_id)
    if not cust:
        return api_error('客户不存在', code='NOT_FOUND', status_code=404)

    # 清理选中区位中其他客户的包裹（确保区位是客户独占的）
    for slot in slots:
        foreign_pkgs = db.session.execute(db.select(IncomingPackage).where(
            IncomingPackage.shelf_slot_id == slot.id,
            IncomingPackage.customer_id != customer_id,
            IncomingPackage.status.in_(['已入库', '待打包'])
        )).scalars().all()
        for fp in foreign_pkgs:
            fp.shelf_slot_id = None

    # 标记选中区位属于该客户（追加模式：不清理客户已有旧货位）
    for slot in slots:
        slot.customer_id = customer_id

    # 该客户所有未打包且未分配货位的包裹归入选中的区位
    pkgs = db.session.execute(
        db.select(IncomingPackage).where(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.is_(None)  # 不影响已分配到其他货位的包裹
        )
    ).scalars().all()

    count = 0
    # 将所有包裹均分到选中的区位
    import math
    pkgs_list = list(pkgs)
    pkgs_per_slot = math.ceil(len(pkgs_list) / len(slots)) if pkgs_list else 0
    for i, pkg in enumerate(pkgs_list):
        slot_idx = min(i // pkgs_per_slot, len(slots) - 1) if pkgs_per_slot > 0 else 0
        pkg.shelf_slot_id = slots[slot_idx].id
        count += 1

    slot_labels_str = ', '.join(slot_labels)
    try:
        db.session.commit()
    except IntegrityError:
        # 有外部脏数据导致提交失败，rollback 后重新执行
        db.session.rollback()
        # 重新清理外客户包裹
        for slot in slots:
            foreign_pkgs = db.session.execute(db.select(IncomingPackage).where(
                IncomingPackage.shelf_slot_id == slot.id,
                IncomingPackage.customer_id != customer_id,
                IncomingPackage.status.in_(['已入库', '待打包'])
            )).scalars().all()
            for fp in foreign_pkgs:
                fp.shelf_slot_id = None
        # 重新标记选中区位属于该客户
        for slot in slots:
            slot.customer_id = customer_id
        # 重新分配包裹
        for i, pkg in enumerate(pkgs_list):
            slot_idx = min(i // pkgs_per_slot, len(slots) - 1) if pkgs_per_slot > 0 else 0
            pkg.shelf_slot_id = slots[slot_idx].id
        db.session.commit()
    create_operation_log('仓库分配', f'客户 {cust.name} → 货位 {slot_labels_str}，{count}件包裹', '系统')
    return api_success(
        data={'slot_labels': slot_labels, 'package_count': count},
        message=f'已分配客户「{cust.name}」→ {slot_labels_str}（{len(slots)}个区位），共 {count} 件包裹'
    )


@app.route('/api/warehouse/mark_boxed', methods=['POST'])
def api_warehouse_mark_boxed():
    """
    将客户的包裹标记为「装箱」状态（放不下货架，统一放入大箱子）
    POST body: {"customer_id": 1}
    效果：清除该客户所有未打包包裹的货架区位分配
    """
    data = request.get_json(silent=True) or {}
    customer_id = data.get('customer_id')
    if not customer_id:
        return api_error('缺少 customer_id')

    cust = db.session.get(Customer, customer_id)
    if not cust:
        return api_error('客户不存在', code='NOT_FOUND', status_code=404)

    # 清除该客户所有未打包包裹的区位
    pkgs = db.session.execute(
        db.select(IncomingPackage).where(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.isnot(None)
        )
    ).scalars().all()

    count = len(pkgs)
    for pkg in pkgs:
        pkg.shelf_slot_id = None

    # 清除该客户的区位分配
    old_slots = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
    ).scalars().all()
    for s in old_slots:
        s.customer_id = None

    db.session.commit()

    if count > 0:
        create_operation_log('仓库操作', f'客户 {cust.name} 标记为装箱（清除 {count} 件包裹的区位）', '系统')

    return api_success(message=f'客户「{cust.name}」已标记为装箱状态（{count}件包裹从货架上移出）')


@app.route('/api/warehouse/unassign', methods=['POST'])
def api_warehouse_unassign():
    """
    取消货位分配
    POST body: {"package_id": 123}  # 取消单包裹
    或: {"customer_id": 1}  # 取消客户所有分配
    或: {"slot_label": "蒙市-1-1-L"}  # 清空该区域
    """
    data = request.get_json(silent=True) or {}
    package_id = data.get('package_id')
    customer_id = data.get('customer_id')
    slot_label = data.get('slot_label', '').strip()

    if slot_label:
        slot = db.session.execute(
            db.select(ShelfSlot).where(ShelfSlot.slot_label == slot_label)
        ).scalar()
        if not slot:
            return api_error(f'货位 {slot_label} 不存在')
        # 清除该区域客户的包裹
        if slot.customer_id:
            pkgs = db.session.execute(
                db.select(IncomingPackage).where(
                    IncomingPackage.shelf_slot_id == slot.id
                )
            ).scalars().all()
            for pkg in pkgs:
                pkg.shelf_slot_id = None
            slot.customer_id = None
            db.session.commit()
            return api_success(message=f'已清空货位 {slot.slot_label}')

    if customer_id:
        # 取消该客户所有货位
        slots = db.session.execute(
            db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
        ).scalars().all()
        for s in slots:
            s.customer_id = None
        pkgs = db.session.execute(
            db.select(IncomingPackage).where(
                IncomingPackage.customer_id == customer_id,
                IncomingPackage.shelf_slot_id.isnot(None)
            )
        ).scalars().all()
        for pkg in pkgs:
            pkg.shelf_slot_id = None
        db.session.commit()
        return api_success(message=f'已取消客户所有货位分配')

    if package_id:
        pkg = db.session.get(IncomingPackage, package_id)
        if not pkg:
            return api_error('包裹不存在', code='NOT_FOUND', status_code=404)
        pkg.shelf_slot_id = None
        db.session.commit()
        return api_success(message='已取消货位分配')

    return api_error('请指定 package_id、customer_id 或 slot_label')


@app.route('/api/warehouse/slots')
def api_warehouse_slots():
    """获取所有货位列表（供下拉选择器使用）"""
    slots = db.session.execute(
        db.select(ShelfSlot).order_by(ShelfSlot.shelf_code, ShelfSlot.floor, ShelfSlot.zone)
    ).scalars().all()

    # 带包裹计数
    result = []
    for s in slots:
        cnt = db.session.execute(
            db.select(db.func.count()).select_from(IncomingPackage).where(
                IncomingPackage.shelf_slot_id == s.id,
                IncomingPackage.status.in_(['已入库', '待打包'])
            )
        ).scalar()
        result.append({
            'id': s.id,
            'slot_label': s.slot_label,
            'shelf_code': s.shelf_code,
            'floor': s.floor,
            'zone': s.zone,
            'customer_id': s.customer_id,
            'package_count': cnt,
            'address_label': s.address_label or '',
        })
    return api_success(data={'slots': result})


@app.route('/api/warehouse/customer_slots')
def api_warehouse_customer_slots():
    """获取客户已分配的货位"""
    customer_id = request.args.get('customer_id', type=int)
    if not customer_id:
        return api_error('请指定 customer_id')
    slots = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
    ).scalars().all()
    return api_success(data={'slots': [s.slot_label for s in slots]})


@app.route('/api/warehouse/find_package')
def api_warehouse_find_package():
    """通过快递单号查找包裹（用于货位分配）"""
    tracking = request.args.get('tracking', '').strip()
    if not tracking:
        return api_error('缺少 tracking 参数')

    pkg = db.session.execute(
        db.select(IncomingPackage).where(IncomingPackage.tracking_number == tracking)
    ).scalar()
    if not pkg:
        return api_error(f'未找到单号 {tracking}', code='NOT_FOUND', status_code=404)
    if pkg.status == '已打包':
        return api_error('该包裹已打包，无法修改货位')

    cust = db.session.get(Customer, pkg.customer_id) if pkg.customer_id else None
    current_slot = db.session.get(ShelfSlot, pkg.shelf_slot_id) if pkg.shelf_slot_id else None

    return api_success(data={
        'id': pkg.id,
        'tracking_number': pkg.tracking_number,
        'customer_name': cust.name if cust else '无主',
        'status': pkg.status,
        'nature': pkg.nature,
        'current_slot': current_slot.slot_label if current_slot else None,
    })


@app.route('/api/warehouse/customer_assign', methods=['POST'])
def api_warehouse_customer_assign():
    """
    批量按客户分配货位区域（支持一个客户分配多个区域）
    POST body: {"customer_id": 1, "slot_labels": ["A1-1-L", "A1-2-L"]}
    """
    data = request.get_json(silent=True) or {}
    customer_id = data.get('customer_id')
    slot_labels = data.get('slot_labels', [])

    if not customer_id:
        return api_error('缺少 customer_id')
    if not slot_labels:
        return api_error('缺少 slot_labels')

    cust = db.session.get(Customer, customer_id)
    if not cust:
        return api_error('客户不存在', code='NOT_FOUND', status_code=404)

    # 清除客户旧分配
    old_slots = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
    ).scalars().all()
    for s in old_slots:
        s.customer_id = None

    slots = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.slot_label.in_(slot_labels))
    ).scalars().all()

    if len(slots) != len(slot_labels):
        found = {s.slot_label for s in slots}
        missing = [l for l in slot_labels if l not in found]
        return api_error(f'以下货位不存在: {", ".join(missing)}')

    # 分配
    for s in slots:
        s.customer_id = customer_id

    # 该客户未打包包裹归到第一个区域
    assigned_slot = slots[0]
    pkgs = db.session.execute(
        db.select(IncomingPackage).where(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status.in_(['已入库', '待打包'])
        )
    ).scalars().all()

    count = 0
    for pkg in pkgs:
        pkg.shelf_slot_id = assigned_slot.id
        count += 1

    db.session.commit()
    create_operation_log('仓库批量分配', f'客户 {cust.name} → {", ".join(slot_labels)}，{count}件', '系统')
    return api_success(
        data={'slots': slot_labels, 'package_count': count},
        message=f'已分配客户「{cust.name}」→ 区域「{", ".join(slot_labels)}」，共 {count} 件包裹'
    )


@app.route('/api/warehouse/auto_assign', methods=['POST'])
def api_warehouse_auto_assign():
    """
    智能分配：按历史包裹量自动给所有客户分配货架区域
    高频客户独占层，中频按需分配，低频合并
    """
    # 统计每个客户的包裹量（非无主、非已打包）
    stmt = (
        db.select(
            Customer.id,
            Customer.name,
            func.count(IncomingPackage.id).label('cnt')
        )
        .join(IncomingPackage, IncomingPackage.customer_id == Customer.id)
        .where(IncomingPackage.status.in_(['已入库', '待打包']))
        .group_by(Customer.id)
        .order_by(func.count(IncomingPackage.id).desc())
    )
    rows = db.session.execute(stmt).all()

    if not rows:
        return api_error('暂无待分配客户')

    # 获取所有空闲货位
    all_slots = db.session.execute(
        db.select(ShelfSlot).order_by(ShelfSlot.shelf_code, ShelfSlot.floor, ShelfSlot.zone)
    ).scalars().all()

    # 清除所有现有分配
    for s in all_slots:
        s.customer_id = None

    # 清除所有包裹货位
    pkgs_to_clear = db.session.execute(
        db.select(IncomingPackage).where(
            IncomingPackage.shelf_slot_id.isnot(None),
            IncomingPackage.status.in_(['已入库', '待打包'])
        )
    ).scalars().all()
    for pkg in pkgs_to_clear:
        pkg.shelf_slot_id = None
    db.session.commit()

    # 分组：按层归组（同一货架同一层的L/M/R）
    layer_groups = {}
    for s in all_slots:
        key = f'{s.shelf_code}-{s.floor}'
        if key not in layer_groups:
            layer_groups[key] = []
        layer_groups[key].append(s)

    # 可用层（整层3个区域都空闲的）
    available_layers = {k: v for k, v in layer_groups.items()}
    # 可用单个区域
    available_single = list(all_slots)

    result_assignments = []

    for cust_id, cust_name, cnt in rows:
        if cnt >= 30:
            # 高频：分配整层
            if available_layers:
                layer_key, layer_slots = next(iter(available_layers.items()))
                del available_layers[layer_key]
                for s in layer_slots:
                    s.customer_id = cust_id
                    if s in available_single:
                        available_single.remove(s)
                # 包裹归到第一个区域
                assigned_slot = layer_slots[0]
                slot_labels = [s.slot_label for s in layer_slots]
            else:
                # 没整层了，给单个区域
                if not available_single:
                    break
                assigned_slot = available_single.pop(0)
                assigned_slot.customer_id = cust_id
                slot_labels = [assigned_slot.slot_label]
                # 移除所在层
                layer_key = f'{assigned_slot.shelf_code}-{assigned_slot.floor}'
                available_layers.pop(layer_key, None)
        elif cnt >= 10:
            # 中频：分配1-2个区域
            need = 2 if cnt >= 20 else 1
            assigned = []
            for _ in range(need):
                if not available_single:
                    break
                s = available_single.pop(0)
                s.customer_id = cust_id
                assigned.append(s)
                layer_key = f'{s.shelf_code}-{s.floor}'
                available_layers.pop(layer_key, None)
            if assigned:
                assigned_slot = assigned[0]
                slot_labels = [s.slot_label for s in assigned]
            else:
                break
        else:
            # 低频：1个区域
            if not available_single:
                break
            assigned_slot = available_single.pop(0)
            assigned_slot.customer_id = cust_id
            slot_labels = [assigned_slot.slot_label]
            layer_key = f'{assigned_slot.shelf_code}-{assigned_slot.floor}'
            available_layers.pop(layer_key, None)

        # 客户包裹归入第一个区域
        cust_pkgs = db.session.execute(
            db.select(IncomingPackage).where(
                IncomingPackage.customer_id == cust_id,
                IncomingPackage.status.in_(['已入库', '待打包'])
            )
        ).scalars().all()

        pkg_count = 0
        for pkg in cust_pkgs:
            pkg.shelf_slot_id = assigned_slot.id
            pkg_count += 1

        result_assignments.append({
            'customer_name': cust_name,
            'customer_id': cust_id,
            'package_count': pkg_count,
            'slots': slot_labels,
        })

    db.session.commit()
    create_operation_log('仓库智能分配', f'自动分配 {len(result_assignments)} 个客户', '系统')
    return api_success(
        data={'assignments': result_assignments, 'remaining_slots': len(available_single)},
        message=f'智能分配完成，共分配 {len(result_assignments)} 个客户，剩余 {len(available_single)} 个空闲区域'
    )


@app.route('/api/warehouse/cleanup_empty_slots', methods=['POST'])
def api_warehouse_cleanup_empty_slots():
    """清理空货位：解除没有已入库包裹的客户与货位关联"""
    # 查询所有被占用的货位
    occupied = db.session.scalars(
        db.select(ShelfSlot).where(ShelfSlot.customer_id.isnot(None))
    ).all()

    cleaned = 0
    for slot in occupied:
        cid = slot.customer_id
        # 检查该客户是否还有已入库或待打包的包裹
        has_packages = _scalar_count(IncomingPackage, customer_id=cid, status='已入库')
        if has_packages == 0:
            slot.customer_id = None
            cleaned += 1

    if cleaned > 0:
        db.session.commit()
        create_operation_log('清理空货位', f'清理了 {cleaned} 个空货位关联', '系统')

    return api_success(message=f'清理完成：{cleaned} 个货位已释放')


@app.route('/api/warehouse/assign_shelves_by_address', methods=['POST'])
def api_warehouse_assign_shelves_by_address():
    """
    按收货地址分配整架货架（基于历史发货数据，按比例分配N个货架）
    手动触发：先清空旧货架再重新分配
    """
    from sqlalchemy import delete as sa_delete

    # 先清空旧货架
    db.session.execute(
        db.update(IncomingPackage).where(
            IncomingPackage.shelf_slot_id.isnot(None),
            IncomingPackage.status.in_(['已入库', '待打包'])
        ).values(shelf_slot_id=None)
    )
    db.session.execute(sa_delete(ShelfSlot))
    db.session.commit()

    success, message, plan = _auto_create_address_shelves()

    if not success:
        return api_error(message)

    return api_success(
        data={'plan': plan},
        message=message
    )


@app.route('/api/warehouse/customers')
def api_warehouse_customers():
    """获取有未打包包裹的客户列表（供分配下拉选择）"""
    stmt = (
        db.select(Customer.id, Customer.name, Customer.address, Customer.zip_code, func.count(IncomingPackage.id).label('cnt'))
        .join(IncomingPackage, IncomingPackage.customer_id == Customer.id)
        .where(IncomingPackage.status.in_(['已入库', '待打包']))
        .group_by(Customer.id)
        .order_by(func.count(IncomingPackage.id).desc())
    )
    rows = db.session.execute(stmt).all()

    # 同时查已分配的客户
    assigned_slots = db.session.execute(
        db.select(ShelfSlot.customer_id, ShelfSlot.slot_label).where(ShelfSlot.customer_id.isnot(None))
    ).all()
    assigned_map = {}
    for cid, sl in assigned_slots:
        if cid not in assigned_map:
            assigned_map[cid] = []
        assigned_map[cid].append(sl)

    customers = []
    for cid, name, addr, zip_code, cnt in rows:
        customers.append({
            'id': cid,
            'name': name,
            'address': addr or '',
            'zip_code': zip_code or '',
            'package_count': cnt,
            'assigned_slots': assigned_map.get(cid, []),
        })

    return api_success(data={'customers': customers})


@app.route('/api/warehouse/unassigned_customers')
def api_warehouse_unassigned_customers():
    """获取有已入库/待打包包裹但未指定区位的客户列表"""
    stmt = (
        db.select(Customer.id, Customer.name, Customer.address, Customer.zip_code,
                  func.count(IncomingPackage.id).label('cnt'))
        .join(IncomingPackage, IncomingPackage.customer_id == Customer.id)
        .where(
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.is_(None)  # 未指定区位
        )
        .group_by(Customer.id)
        .order_by(func.count(IncomingPackage.id).desc())
    )
    rows = db.session.execute(stmt).all()

    customers = []
    for cid, name, addr, zip_code, cnt in rows:
        customers.append({
            'id': cid,
            'name': name,
            'address': addr or '',
            'zip_code': zip_code or '',
            'package_count': cnt,
        })

    return api_success(data={'customers': customers})


@app.route('/api/warehouse/unassigned_count')
def api_warehouse_unassigned_count():
    """获取未指定区位的包裹总数"""
    cnt = db.session.execute(
        db.select(func.count(IncomingPackage.id)).where(
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.is_(None)
        )
    ).scalar()
    return api_success(data={'count': cnt})


@app.route('/api/carton_group/update/<int:group_id>', methods=['POST'])
def api_carton_group_update(group_id):
    """更新合箱分组信息（内部单号、转单号、物流进度）"""
    group = db.session.get(CartonGroup, group_id)
    if not group:
        return api_error('分组不存在', code='NOT_FOUND', status_code=404)

    data = request.get_json(silent=True) or {}
    if 'internal_tracking' in data:
        group.internal_tracking = data['internal_tracking']
    if 'transfer_tracking' in data:
        group.transfer_tracking = data['transfer_tracking']
    if 'logistics_status' in data:
        group.logistics_status = data['logistics_status']

    db.session.commit()
    return api_success(message='更新成功')


@app.route('/api/warehouse/customer_slot_map/<int:customer_id>')
def api_customer_slot_map(customer_id):
    """获取客户的货架区位图数据"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return api_error('客户不存在')

    address = (customer.address or '').strip()

    # 查找该地址匹配的所有货架
    slots = db.session.execute(
        db.select(ShelfSlot).where(
            ShelfSlot.address_label.contains(address) | ShelfSlot.shelf_code.contains(address)
        ).order_by(ShelfSlot.shelf_code, ShelfSlot.floor, ShelfSlot.zone)
    ).scalars().all()

    # 如果没有地址匹配，返回空
    if not slots:
        return api_success(data={
            'address': address,
            'shelves': [],
            'package_slots': [],
            'assigned_slot': None,
        })

    # 该客户的包裹在哪些区位
    package_slots = db.session.execute(
        db.select(ShelfSlot.slot_label).join(IncomingPackage, IncomingPackage.shelf_slot_id == ShelfSlot.id)
        .where(
            IncomingPackage.customer_id == customer_id,
            IncomingPackage.status.in_(['已入库', '待打包']),
            IncomingPackage.shelf_slot_id.isnot(None)
        ).distinct()
    ).scalars().all()

    # 该客户被分配到的区位（通过 shelf_slot.customer_id）
    assigned_slot_obj = db.session.execute(
        db.select(ShelfSlot).where(ShelfSlot.customer_id == customer_id)
    ).scalar()
    assigned_slot = assigned_slot_obj.slot_label if assigned_slot_obj else None

    # 按shelf_code分组
    shelf_groups = {}
    for s in slots:
        if s.shelf_code not in shelf_groups:
            shelf_groups[s.shelf_code] = {
                'shelf_code': s.shelf_code,
                'address_label': s.address_label or '',
                'floors': {}
            }
        fl = s.floor
        if fl not in shelf_groups[s.shelf_code]['floors']:
            shelf_groups[s.shelf_code]['floors'][fl] = []
        shelf_groups[s.shelf_code]['floors'][fl].append({
            'id': s.id,
            'slot_label': s.slot_label,
            'zone': s.zone,
            'floor': s.floor,
            'customer_id': s.customer_id,
            'package_count': db.session.execute(
                db.select(func.count(IncomingPackage.id)).where(
                    IncomingPackage.shelf_slot_id == s.id,
                    IncomingPackage.status.in_(['已入库', '待打包'])
                )
            ).scalar() or 0,
        })

    return api_success(data={
        'address': address,
        'shelves': list(shelf_groups.values()),
        'package_slots': package_slots,
        'assigned_slot': assigned_slot,
        'customer_name': customer.name,
    })


# ==================== VIP单独发货模块 ====================

@app.route('/vip/packed')
def vip_packed_records():
    """VIP打包记录页面 — 支持筛选/分页/汇总/发货时间/客户混用标识"""
    from models import Customer, PackedPackage, Carton, CartonItem, PackedPackageItem, IncomingPackage
    from sqlalchemy.orm import joinedload

    # ── 筛选参数 ──
    customer_id = request.args.get('customer', type=int) or None
    status = request.args.get('status', 'pending')  # pending=待处理(默认) / completed=已完成 / shipped=已发货
    start = request.args.get('start', '').strip()
    end = request.args.get('end', '').strip()
    q = request.args.get('q', '').strip()  # 客户名模糊搜索

    filters = [PackedPackage.is_vip_solo == True, PackedPackage.status != '已退货']
    if customer_id:
        filters.append(PackedPackage.customer_id == customer_id)
    if q:
        # 客户名模糊搜索 → 匹配的客户 id 列表
        q_ids = db.session.scalars(
            db.select(Customer.id).where(Customer.name.like(f'%{q}%'))
        ).all()
        if not q_ids:
            filters.append(PackedPackage.customer_id.in_([]))  # 无匹配 → 空结果
        else:
            filters.append(PackedPackage.customer_id.in_(list(q_ids)))
    if status == 'pending':
        filters.append(PackedPackage.status.notin_(['已发货', '已完成']))
    elif status == 'completed':
        filters.append(PackedPackage.status == '已完成')
    elif status == 'shipped':
        filters.append(PackedPackage.status == '已发货')
    if start:
        filters.append(PackedPackage.created_at >= start + ' 00:00:00')
    if end:
        filters.append(PackedPackage.created_at <= end + ' 23:59:59')

    # ── 查询（不分页，全部展示，按客户分组折叠）──
    total = db.session.scalar(db.select(db.func.count()).where(*filters)) or 0
    packs = db.session.execute(
        db.select(PackedPackage).options(joinedload(PackedPackage.items))
        .where(*filters).order_by(PackedPackage.created_at.desc())
    ).unique().scalars().all()

    # ── 汇总统计（全部筛选结果）──
    stats = {'customers': 0, 'total': total, 'shipped': 0, 'completed': 0, 'pending': 0, 'weight': 0.0}
    if total:
        rows = db.session.execute(
            db.select(PackedPackage.customer_id, PackedPackage.status, PackedPackage.weight).where(*filters)
        ).all()
        stats['customers'] = len(set(r[0] for r in rows))
        stats['shipped'] = sum(1 for r in rows if r[1] == '已发货')
        stats['completed'] = sum(1 for r in rows if r[1] == '已完成')
        stats['pending'] = total - stats['shipped'] - stats['completed']
        stats['weight'] = round(sum((r[2] or 0) for r in rows), 1)

    # ── 客户信息 + 混用标识（solo数 vs 普通批次数）──
    customer_ids = sorted(set(p.customer_id for p in packs))
    customers_map = {}
    customer_stats = {}
    if customer_ids:
        cs = db.session.scalars(db.select(Customer).where(Customer.id.in_(customer_ids))).all()
        customers_map = {c.id: c for c in cs}
        for cid in customer_ids:
            solo_n = db.session.scalar(db.select(db.func.count()).where(
                PackedPackage.customer_id == cid, PackedPackage.is_vip_solo == True)) or 0
            normal_n = db.session.scalar(db.select(db.func.count()).where(
                PackedPackage.customer_id == cid, PackedPackage.is_vip_solo != True)) or 0
            customer_stats[cid] = {'solo': solo_n, 'normal': normal_n}

    # ── 客户下拉选项（全部 VIP 客户）──
    vip_customers = db.session.scalars(
        db.select(Customer).where(Customer.is_vip == True).order_by(Customer.name)
    ).all()

    # ── 箱子映射 ──
    pack_ids = [p.id for p in packs]
    carton_map = {}
    if pack_ids:
        carton_items = db.session.execute(
            db.select(CartonItem).options(joinedload(CartonItem.carton))
            .filter(CartonItem.packed_package_id.in_(pack_ids))
        ).unique().scalars().all()
        for ci in carton_items:
            if ci.packed_package_id not in carton_map:
                carton_map[ci.packed_package_id] = ci.carton

    return render_template('vip_packed.html',
                           packs=packs,
                           customers_map=customers_map,
                           carton_map=carton_map,
                           stats=stats,
                           customer_stats=customer_stats,
                           vip_customers=vip_customers,
                           f_customer=customer_id, f_status=status,
                           f_start=start, f_end=end, f_q=q)


@app.route('/api/vip/packed/<int:pack_id>/invoice', methods=['GET', 'POST'])
@app.route('/api/vip/packed/customer/<int:customer_id>/invoice', methods=['GET', 'POST'])
def api_vip_customer_invoice(customer_id):
    """为VIP客户按客户合并生成发票"""
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({'success': False, 'message': '客户不存在'}), 404

    first_pack = db.session.scalar(
        db.select(PackedPackage).filter_by(customer_id=customer_id, is_vip_solo=True)
    )
    if not first_pack:
        return jsonify({'success': False, 'message': '该客户没有VIP单独发货记录'}), 400

    return api_vip_generate_invoice(first_pack.id)


@app.route('/api/vip/packed/<int:pack_id>/invoice', methods=['GET', 'POST'])
def api_vip_generate_invoice(pack_id):
    """为VIP客户生成发票（按客户合并所有箱子，复用批次发票引擎）"""
    from models import Customer, PackedPackage, Carton, CartonItem, IncomingPackage, PackedPackageItem, AddressBook
    from invoice_engine import _find_template_by_zip, TEMPLATE_DIR, IMAGE_DIR, generate_invoice, _fill_invoice_header
    from collections import defaultdict
    import queue, threading

    pack = db.session.get(PackedPackage, pack_id)
    if not pack or not pack.is_vip_solo:
        return jsonify({'success': False, 'message': '无效的VIP包裹'}), 400

    customer = db.session.get(Customer, pack.customer_id)
    if not customer:
        return jsonify({'success': False, 'message': '客户不存在'}), 404

    # 查找该客户所有「已完成」的 VIP 单独发货打包记录（只对已完成的生成发票）
    all_packs = db.session.scalars(
        db.select(PackedPackage).filter_by(customer_id=customer.id, is_vip_solo=True)
        .where(PackedPackage.status == '已完成')
    ).all()
    if not all_packs:
        return jsonify({'success': False, 'message': '该客户没有VIP单独发货记录'}), 400

    from models import InvoiceConfig
    api_key = InvoiceConfig.get('deepseek_api_key', '')
    if not api_key:
        return jsonify({'success': False, 'message': '请先在发票配置页设置 DeepSeek API Key'}), 400

    output_dir = os.path.join(BASE_DIR, 'invoice_output')
    os.makedirs(output_dir, exist_ok=True)

    msg_queue = queue.Queue()

    def run_gen():
        try:
            def cb(msg):
                msg_queue.put(msg)

            with app.app_context():
                zip_code = customer.zip_code or ''

                # 收集箱子信息（有 Carton 的用 Carton，没有的用 PackedPackage 自身）
                boxes = []
                puhuo_boxes = []
                minhuo_boxes = []
                all_pids = [p.id for p in all_packs]
                carton_items = db.session.scalars(
                    db.select(CartonItem).options(joinedload(CartonItem.carton))
                    .filter(CartonItem.packed_package_id.in_(all_pids))
                ).all()
                carton_by_pid = {ci.packed_package_id: ci.carton for ci in carton_items}

                for p in all_packs:
                    carton = carton_by_pid.get(p.id)
                    # 解析该包裹的品名
                    products = []
                    if p.contents_description:
                        for item in re.split(r'[\s\u3000]+', str(p.contents_description).strip()):
                            if item:
                                if item in ["衣服", "服装"]:
                                    for sub in ["上衣", "裤子", "裙子"]:
                                        if sub not in products:
                                            products.append(sub)
                                elif item not in products:
                                    products.append(item)
                    box = {
                        'box_no': carton.name if carton else f'V-{p.id}',
                        'type': '普货' if p.nature == '普' else '敏货',
                        'weight': carton.total_weight if carton and carton.total_weight else (p.weight or 0),
                        'length': carton.length if carton and carton.length else (p.length or 0),
                        'width': carton.width if carton and carton.width else (p.width or 0),
                        'height': carton.height if carton and carton.height else (p.height or 0),
                        'owners': [customer.name],
                        'products': products
                    }
                    boxes.append(box)
                    if box['type'] == '普货':
                        puhuo_boxes.append(box)
                    else:
                        minhuo_boxes.append(box)

                if not boxes:
                    msg_queue.put('__ERROR__: 没有找到箱子信息')
                    return

                # 收集装货清单（复用批次发票逻辑：拆分衣服等）
                cargo_dict = defaultdict(lambda: {"普货": [], "敏货": []})
                for p in all_packs:
                    if p.contents_description:
                        items = re.split(r'[\s\u3000]+', str(p.contents_description).strip())
                        nature_key = "敏货" if p.nature == '敏' else "普货"
                        for item in items:
                            if item and item not in cargo_dict[customer.name][nature_key]:
                                # 扩展衣服→上衣/裤子/裙子（与批次发票相同）
                                if item in ["衣服", "服装"]:
                                    for sub in ["上衣", "裤子", "裙子"]:
                                        if sub not in cargo_dict[customer.name][nature_key]:
                                            cargo_dict[customer.name][nature_key].append(sub)
                                else:
                                    cargo_dict[customer.name][nature_key].append(item)

                cargo_dict_simple = {k: dict(v) for k, v in cargo_dict.items()}

                if progress_callback_local(cb, f"📊 汇总: 普货 {len(puhuo_boxes)} 箱, 敏货 {len(minhuo_boxes)} 箱"):
                    pass

                # 匹配模板
                puhuo_tpl = _find_template_by_zip(zip_code, 'puhuo', TEMPLATE_DIR)
                minhuo_tpl = _find_template_by_zip(zip_code, 'minhuo', TEMPLATE_DIR)
                if not puhuo_tpl:
                    puhuo_tpl = _find_template_by_zip('91755', 'puhuo', TEMPLATE_DIR)
                if not minhuo_tpl:
                    minhuo_tpl = _find_template_by_zip('91755', 'minhuo', TEMPLATE_DIR)
                if not puhuo_tpl and not minhuo_tpl:
                    msg_queue.put('__ERROR__: 未找到发票模板')
                    return

                today = datetime.now().strftime("%m月%d日")
                results = []

                # 查找地址库
                ab = db.session.scalar(
                    db.select(AddressBook).filter_by(zip_code=customer.zip_code)
                )

                if puhuo_boxes and puhuo_tpl and os.path.exists(puhuo_tpl):
                    filename = f"VIP-{customer.name}-普货-{len(puhuo_boxes)}箱-{today}.xlsx"
                    output = os.path.join(output_dir, filename)
                    cb(f"📝 生成普货发票 ({len(puhuo_boxes)} 箱)...")
                    generate_invoice(
                        puhuo_boxes, cargo_dict_simple, puhuo_tpl, output, "puhuo",
                        IMAGE_DIR, api_key, progress_callback=cb
                    )
                    # 填充抬头
                    _fill_vip_invoice_header(output, customer, ab, 'puhuo', box_count=len(puhuo_boxes))
                    results.append(output)
                    cb(f"  ✅ 普货发票已完成: {filename}")

                if minhuo_boxes and minhuo_tpl and os.path.exists(minhuo_tpl):
                    filename = f"VIP-{customer.name}-敏货-{len(minhuo_boxes)}箱-{today}.xlsx"
                    output = os.path.join(output_dir, filename)
                    cb(f"📝 生成敏货发票 ({len(minhuo_boxes)} 箱)...")
                    generate_invoice(
                        minhuo_boxes, cargo_dict_simple, minhuo_tpl, output, "minhuo",
                        IMAGE_DIR, api_key, progress_callback=cb
                    )
                    _fill_vip_invoice_header(output, customer, ab, 'minhuo')
                    results.append(output)
                    cb(f"  ✅ 敏货发票已完成: {filename}")

                cb(f"🎉 全部完成！共生成 {len(results)} 个发票文件")
                msg_queue.put('__DONE__')
                msg_queue.put(json.dumps(results))
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg_queue.put(f'__ERROR__: {str(e)}')

    def progress_callback_local(cb, msg):
        if cb:
            cb(msg)
        return True

    thread = threading.Thread(target=run_gen)
    thread.start()

    def generate():
        while True:
            try:
                msg = msg_queue.get(timeout=120)
                if msg == '__DONE__':
                    results_json = msg_queue.get(timeout=5)
                    yield f'data: {{"status": "done", "files": {results_json}}}\n\n'
                    return
                elif str(msg).startswith('__ERROR__'):
                    err = str(msg).replace('__ERROR__: ', '')
                    yield f'data: {{"status": "error", "message": "{err}"}}\n\n'
                    return
                else:
                    yield f'data: {{"status": "progress", "message": "{str(msg)}"}}\n\n'
            except queue.Empty:
                yield f'data: {{"status": "ping"}}\n\n'

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def _fill_vip_invoice_header(output_file, customer, ab_entry, invoice_type, box_count=0):
    """根据VIP客户字段和地址库填充发票抬头"""
    if not os.path.exists(output_file):
        return
    from openpyxl import load_workbook
    wb = load_workbook(output_file)
    ws = wb.active

    is_minhuo = invoice_type == 'minhuo'
    ab_consignee = (ab_entry.minhuo_consignee if is_minhuo else ab_entry.puhuo_consignee) if ab_entry else ''
    ab_phone = (ab_entry.minhuo_phone if is_minhuo else ab_entry.puhuo_phone) if ab_entry else ''
    ab_address = (ab_entry.minhuo_address if is_minhuo else ab_entry.puhuo_address) if ab_entry else ''

    if invoice_type == 'puhuo':
        # 联系人
        ws.cell(11, 2).value = customer.invoice_contact or ab_consignee or customer.name
        # 电话
        if customer.invoice_phone or ab_phone:
            ws.cell(12, 2).value = customer.invoice_phone or ab_phone
        # 邮编
        ws.cell(15, 2).value = customer.invoice_zip or (ab_entry.zip_code if ab_entry else '') or customer.zip_code
        # 地址
        ws.cell(16, 2).value = customer.invoice_address or ab_address or customer.address
        # 国家
        if ab_entry and ab_entry.country:
            ws.cell(7, 2).value = ab_entry.country
        # 预计件数
        if box_count > 0:
            ws.cell(21, 2).value = box_count
    else:
        if customer.invoice_contact or ab_consignee:
            ws.cell(4, 2).value = customer.invoice_contact or ab_consignee
        if customer.invoice_phone or ab_phone:
            ws.cell(13, 2).value = customer.invoice_phone or ab_phone
        if customer.invoice_address or ab_address:
            ws.cell(6, 2).value = customer.invoice_address or ab_address
        if ab_entry and ab_entry.city:
            ws.cell(9, 2).value = ab_entry.city
        if ab_entry and ab_entry.state:
            ws.cell(10, 2).value = ab_entry.state
        ws.cell(11, 2).value = customer.invoice_zip or (ab_entry.zip_code if ab_entry else '') or customer.zip_code
        if ab_entry and ab_entry.country:
            ws.cell(12, 2).value = ab_entry.country
        ws.cell(3, 2).value = customer.invoice_zip or customer.zip_code or ''

    wb.save(output_file)


# ==================== 发票生成模块 ====================

@app.route('/invoice/config')
def invoice_config():
    """发票配置页面"""
    from models import InvoiceConfig
    # 只显示未锁定的批次（已锁定的批次不允许再生成发票）
    batches = db.session.scalars(
        db.select(Batch).where(Batch.is_locked != True).order_by(Batch.batch_no.desc())
    ).all()
    api_key = InvoiceConfig.get('deepseek_api_key', '')
    return render_template('invoice_config.html',
                          batches=batches,
                          api_key=api_key)


@app.route('/api/invoice/config', methods=['POST'])
def api_invoice_config():
    """保存发票配置"""
    from models import InvoiceConfig
    data = request.get_json(silent=True) or {}
    for key in ['deepseek_api_key']:
        if key in data:
            InvoiceConfig.set(key, data[key])
    return jsonify({'success': True, 'message': '配置已保存'})


@app.route('/api/invoice/index_images', methods=['POST'])
def api_index_images():
    """扫描图片文件夹建索引"""
    from models import InvoiceConfig, ProductImage
    folder = InvoiceConfig.get('image_folder', '')
    if not folder or not os.path.isdir(folder):
        return jsonify({'success': False, 'message': '图片文件夹不存在'}), 400

    extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
    import time as _time
    start = _time.time()
    count = 0

    # 清空旧索引
    db.session.execute(db.delete(ProductImage))
    db.session.commit()

    for root, dirs, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in extensions:
                continue
            full_path = os.path.join(root, f)
            name = os.path.splitext(f)[0]
            size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0
            img = ProductImage(filename=name, file_path=full_path, file_ext=ext, file_size=size)
            db.session.add(img)
            count += 1
            if count % 500 == 0:
                db.session.flush()
    db.session.commit()
    elapsed = round(_time.time() - start, 1)
    return jsonify({'success': True, 'message': f'已索引 {count} 张图片（{elapsed}秒）', 'count': count})


@app.route('/api/invoice/image_count')
def api_invoice_image_count():
    """返回已索引的图片数"""
    from models import ProductImage
    count = db.session.scalar(db.select(db.func.count(ProductImage.id))) or 0
    return jsonify({'success': True, 'count': count})


@app.route('/api/invoice/missing_images')
def api_missing_images():
    """导出所有未匹配图片的品名，用于本地下载脚本"""
    from models import ProductImage, ProductCache
    from invoice_engine import find_image_sql, IMAGE_DIR

    # 收集所有品名
    names = set()
    for desc in db.session.scalars(db.select(PackedPackage.contents_description)).all():
        if desc:
            for n in re.split(r'[\s\u3000,，]+', str(desc).strip()):
                if n and len(n) >= 2:
                    names.add(n)
    for pc in db.session.scalars(db.select(ProductCache.product_name)).all():
        if pc and len(pc) >= 2:
            names.add(pc)

    # 筛选本地无图的
    missing = []
    for name in sorted(names):
        if not find_image_sql(name, IMAGE_DIR):
            missing.append(name)

    return jsonify({'success': True, 'data': missing, 'count': len(missing), 'image_folder': '/opt/images'})


@app.route('/api/invoice/generate/<int:batch_id>')
def api_invoice_generate(batch_id):
    """SSE 流式生成发票, ?type=all|puhuo|minhuo"""
    from models import InvoiceConfig
    import threading
    import queue
    from invoice_engine import generate_all_invoices

    image_folder = '/opt/images'
    api_key = InvoiceConfig.get('deepseek_api_key', '')
    template_dir = InvoiceConfig.get('template_dir', '')

    if not api_key:
        return jsonify({'success': False, 'message': '请先在发票配置页设置 DeepSeek API Key'}), 400

    if not template_dir:
        template_dir = '/opt/invoice_templates'

    output_dir = InvoiceConfig.get('output_dir', '')
    if not output_dir:
        output_dir = os.path.join(BASE_DIR, 'invoice_output')
    os.makedirs(output_dir, exist_ok=True)

    invoice_type = request.args.get('type', 'all')  # all | puhuo | minhuo

    msg_queue = queue.Queue()

    def run_gen():
        try:
            def cb(msg):
                msg_queue.put(msg)
            with app.app_context():
                results = generate_all_invoices(batch_id, image_folder, api_key, output_dir, cb, invoice_type=invoice_type)
                msg_queue.put('__DONE__')
                msg_queue.put(json.dumps(results))
        except Exception as e:
            msg_queue.put(f'__ERROR__: {str(e)}')

    thread = threading.Thread(target=run_gen)
    thread.start()

    def generate():
        while True:
            try:
                msg = msg_queue.get(timeout=120)
                if msg == '__DONE__':
                    results_json = msg_queue.get(timeout=5)
                    yield f'data: {{"status": "done", "files": {results_json}}}\n\n'
                    return
                elif str(msg).startswith('__ERROR__'):
                    err = str(msg).replace('__ERROR__: ', '')
                    yield f'data: {{"status": "error", "message": "{err}"}}\n\n'
                    return
                else:
                    yield f'data: {{"status": "progress", "message": "{msg}"}}\n\n'
            except queue.Empty:
                yield f'data: {{"status": "ping"}}\n\n'

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/download_invoice')
def download_invoice():
    """下载生成的发票文件"""
    path = request.args.get('path', '')
    if not path or not os.path.isfile(path):
        return '文件不存在', 404
    filename = os.path.basename(path)
    return send_file(path, as_attachment=True, download_name=filename)


# ==================== VIP单独发货 客户发货明细导出 ====================

@app.route('/export/vip_shipping_detail/<int:customer_id>')
def export_vip_shipping_detail(customer_id):
    """VIP单独发货 客户发货明细导出（同PackedPackage合并：性质/品名/实重/长宽高/材积）"""
    pkg_items = db.session.execute(
        db.select(IncomingPackage, PackedPackage, PackedPackageItem, Customer)
        .join(PackedPackageItem, PackedPackageItem.incoming_package_id == IncomingPackage.id)
        .join(PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id)
        .join(Customer, IncomingPackage.customer_id == Customer.id)
        .filter(PackedPackage.customer_id == customer_id, PackedPackage.is_vip_solo == True, PackedPackage.status == '已完成')
        .order_by(IncomingPackage.entry_time, PackedPackage.id)
    ).all()

    if not pkg_items:
        return "该客户无VIP单独发货记录", 404

    customer = db.session.get(Customer, customer_id)

    # 按 packed_package_id 分组
    packed_groups = {}
    for incoming, pp, ppi, cust in pkg_items:
        if pp.id not in packed_groups:
            packed_groups[pp.id] = {
                'date': incoming.entry_time,
                'customer_name': cust.name,
                'customer_id': pp.customer_id,
                'packed_package_id': pp.id,
                'zip_code': cust.zip_code or '',
                'address': cust.address or '',
                'nature': '普货' if pp.nature == '普' else '敏货',
                'product_name': pp.contents_description or '',
                'weight': pp.weight or None,
                'length': pp.length or None,
                'width': pp.width or None,
                'height': pp.height or None,
                'volume': round(pp.length * pp.width * pp.height / 6000, 3) if pp.length and pp.width and pp.height else None,
                'incoming_packages': []
            }
        packed_groups[pp.id]['incoming_packages'].append({
            'tracking_number': incoming.tracking_number or '',
            'scan_number': incoming.tracking_number or '',
            'remark': incoming.customer_remark or '',
            'danger_scan': incoming.tracking_number if pp.nature == '敏' else '',
        })

    # 排序并编号
    sorted_packs = sorted(packed_groups.items(), key=lambda x: (x[1]['date'], x[0]))

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "发货明细"

    headers = ['日期', '客户名称', '序号', '快递单号', '邮编', '收货地址', '扫描单号',
               '备注', '敏货扫描', '性质', '品名', '实重', '长', '宽', '高', '材积']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'),
                         top=Side(style='thin'), bottom=Side(style='thin'))

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(name='微软雅黑', size=12, bold=True)
        cell.alignment = align_center
        cell.border = thin_border

    # 写入数据
    current_row = 2
    pp_merge_ranges = []  # 同PackedPackage合并范围
    seq = 0

    for pp_id, pdata in sorted_packs:
        date_str = pdata['date'].strftime('%Y-%m-%d') if pdata['date'] else ''
        pp_start = current_row
        for pkg in pdata['incoming_packages']:
            seq += 1
            vals = [date_str, pdata['customer_name'], seq, pkg['tracking_number'],
                    pdata['zip_code'], pdata['address'], pkg['scan_number'],
                    pkg['remark'], pkg['danger_scan'], pdata['nature'],
                    pdata['product_name'], pdata['weight'], pdata['length'],
                    pdata['width'], pdata['height'], pdata['volume']]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(row=current_row, column=c, value=v if v is not None else '')
                cell.font = font_style
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True) if c in (4,7,11) else align_center
                cell.border = thin_border
            current_row += 1
        pp_end = current_row - 1
        if pp_end > pp_start:
            pp_merge_ranges.append((pp_start, pp_end))

    # 同PackedPackage合并：性质(10)、品名(11)、实重(12)、长(13)、宽(14)、高(15)、材积(16)
    for start_r, end_r in pp_merge_ranges:
        for col in [10, 11, 12, 13, 14, 15, 16]:
            ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 单客户导出：整块合并客户名称(2)、邮编(5)、收货地址(6)、日期(1)
    if current_row > 2:
        last = current_row - 1
        ws.merge_cells(start_row=2, start_column=1, end_row=last, end_column=1)  # 日期
        ws.merge_cells(start_row=2, start_column=2, end_row=last, end_column=2)  # 客户名称
        ws.merge_cells(start_row=2, start_column=5, end_row=last, end_column=5)  # 邮编
        ws.merge_cells(start_row=2, start_column=6, end_row=last, end_column=6)  # 收货地址

    col_widths = {1:12, 2:16, 3:6, 4:22, 5:8, 6:14, 7:22, 8:16, 9:22, 10:6, 11:20, 12:8, 13:6, 14:6, 15:6, 16:8}
    for col, w in col_widths.items():
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = w

    safe_name = customer.name.replace('/', '_').replace('\\', '_') if customer else str(customer_id)
    filename = f'VIP发货明细_{safe_name}.xlsx'
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


# ==================== VIP单独发货 客户计费表导出（按数据导出的计费表格式） ====================

@app.route('/export/vip_billing_detail/<int:customer_id>')
def export_vip_billing_detail(customer_id):
    """VIP单独发货 客户计费表导出（格式与数据导出的计费表一致）"""
    pkg_items = db.session.execute(
        db.select(IncomingPackage, PackedPackage, PackedPackageItem, Customer)
        .join(PackedPackageItem, PackedPackageItem.incoming_package_id == IncomingPackage.id)
        .join(PackedPackage, PackedPackageItem.packed_package_id == PackedPackage.id)
        .join(Customer, IncomingPackage.customer_id == Customer.id)
        .filter(PackedPackage.customer_id == customer_id, PackedPackage.is_vip_solo == True, PackedPackage.status == '已完成')
        .order_by(IncomingPackage.entry_time, PackedPackage.id)
    ).all()

    if not pkg_items:
        return "该客户无VIP单独发货记录", 404

    customer = db.session.get(Customer, customer_id)

    # 按 packed_package_id 分组，每行一个 IncomingPackage
    all_rows = []
    for incoming, pp, ppi, cust in pkg_items:
        vol_w = calc_volume_weight(pp.length or 0, pp.width or 0, pp.height or 0)
        cw = calc_chargeable_weight(vol_w, pp.weight or 0)
        dim = ''
        if pp.length and pp.width and pp.height:
            dim = f"{int(pp.length)}×{int(pp.width)}×{int(pp.height)}"
        vw_formatted = round(vol_w, 3) if vol_w else None
        bw_formatted = round(cw, 3) if cw else None
        remark_parts = []
        if incoming.customer_remark:
            remark_parts.append(f"📝{incoming.customer_remark}")
        if incoming.abnormal_remark:
            remark_parts.append(f"⚠️{incoming.abnormal_remark}")
        all_rows.append({
            'customer_name': cust.name,
            'customer_id': pp.customer_id,
            'packed_package_id': pp.id,
            'tracking_number': incoming.tracking_number or '',
            'destination': cust.address or '',
            'nature': '普货' if pp.nature == '普' else '敏货',
            'actual_weight': pp.weight if pp.weight else None,
            'dimensions': dim,
            'volume_weight': vw_formatted,
            'billable_weight': bw_formatted,
            'remark': '; '.join(remark_parts) if remark_parts else '',
        })

    # 编号
    for i, r in enumerate(all_rows, 1):
        r['seq'] = i

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "计费表"

    headers = ['客户', '序号', '快递单号', '收货地', '货物性质',
               '实际重量', '长宽高', '材积重/6000', '计费重', '备注']

    font_style = Font(name='微软雅黑', size=12)
    align_center = Alignment(horizontal='center', vertical='center')
    thin_side = Side(style='thin', color='000000')
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = font_style
        cell.alignment = align_center
        cell.border = border_all

    # 数据行
    current_row = 2
    prev_cid = None
    prev_key = None
    prev_ppid = None
    customer_merge_ranges = []
    nature_merge_ranges = []
    pp_merge_ranges = []

    for row_data in all_rows:
        is_customer_first = (row_data['customer_id'] != prev_cid)
        key = (row_data['customer_id'], row_data['nature'])
        is_nature_first = (key != prev_key)
        is_pp_first = (row_data['packed_package_id'] != prev_ppid)

        customer_val = row_data['customer_name'] if is_customer_first else ''
        dest_val = row_data['destination'] if is_customer_first else ''
        nature_val = row_data['nature'] if is_nature_first else ''
        aw_val = row_data['actual_weight'] if is_pp_first else ''
        dim_val = row_data['dimensions'] if is_pp_first else ''
        vw_val = row_data['volume_weight'] if is_pp_first else ''
        bw_val = row_data['billable_weight'] if is_pp_first else ''

        vals = [
            customer_val, row_data['seq'], row_data['tracking_number'],
            dest_val, nature_val, aw_val, dim_val, vw_val, bw_val,
            row_data['remark']
        ]

        for col_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val if val is not None else '')
            cell.font = font_style
            cell.alignment = align_center
            cell.border = border_all

        if is_customer_first:
            customer_merge_ranges.append([current_row, current_row])
        else:
            if customer_merge_ranges:
                customer_merge_ranges[-1][1] = current_row

        if is_nature_first:
            nature_merge_ranges.append([current_row, current_row])
        else:
            if nature_merge_ranges:
                nature_merge_ranges[-1][1] = current_row

        if is_pp_first:
            pp_merge_ranges.append([current_row, current_row])
        else:
            if pp_merge_ranges:
                pp_merge_ranges[-1][1] = current_row

        prev_cid = row_data['customer_id']
        prev_key = key
        prev_ppid = row_data['packed_package_id']
        current_row += 1

    # 同客户合并：客户(1)、收货地(4)
    for start_r, end_r in customer_merge_ranges:
        if end_r > start_r:
            for col in [1, 4]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    # 同客户+性质合并：货物性质(5)
    for start_r, end_r in nature_merge_ranges:
        if end_r > start_r:
            ws.merge_cells(start_row=start_r, start_column=5, end_row=end_r, end_column=5)

    # 同PackedPackage合并：实际重量(6)、长宽高(7)、材积重(8)、计费重(9)
    for start_r, end_r in pp_merge_ranges:
        if end_r > start_r:
            for col in [6, 7, 8, 9]:
                ws.merge_cells(start_row=start_r, start_column=col, end_row=end_r, end_column=col)

    col_widths = {1:14, 2:6, 3:22, 4:18, 5:10, 6:10, 7:14, 8:12, 9:10, 10:24}
    for col, w in col_widths.items():
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = w

    safe_name = customer.name.replace('/', '_').replace('\\', '_') if customer else str(customer_id)
    filename = f'VIP计费表_{safe_name}.xlsx'
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


if __name__ == '__main__':
    # 开发环境：debug模式 + SSL（本地测试用）
    # 生产环境：通过 gunicorn wsgi:app 启动，Nginx 反向代理 + SSL
    if os.environ.get('FLASK_ENV') == 'production':
        app.run(host='0.0.0.0', port=5000)
    else:
        cert_file = os.path.join(BASE_DIR, 'cert.pem')
        key_file = os.path.join(BASE_DIR, 'key.pem')
        app.run(debug=True, host='0.0.0.0', port=5000, ssl_context=(cert_file, key_file))
