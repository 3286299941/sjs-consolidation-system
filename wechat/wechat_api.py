# -*- coding: utf-8 -*-
"""
速捷盛集运 · 小程序 API 蓝图（Flask + SQLAlchemy 2.0 + SQLite）
================================================================
集成方式（在现有 app.py 中加两行，不改任何现有模型/表结构）：

    from wechat_api import create_wechat_blueprint
    app.register_blueprint(create_wechat_blueprint(db))

设计约束（2026-08 初版，2026-11-06 用户要求调整）：
  1. 客户端接口对现有业务表只读；唯一写操作是客户发货意向确认（shipment_intents）。
  2. 【2026-11-06 起】管理端按用户明确要求开放写操作：新建/删除客户、
     入库单删除/退货/转无主件、无主件归户、解绑、批次物流轨迹。
     这些操作严格复用主系统 app.py 的字段与状态口径，见各接口注释。
  3. 批次锁定/解锁不在小程序提供（只能主系统端操作）。
  4. 客户发货意向只是操作端的输入参考，确认意向只生成「待打包清单」，不联动改状态。

实现方式说明：
  用原生 SQL（SQLAlchemy text）访问现有表，不 import 现有 ORM 模型类，
  因此不需要知道线上 app.py 的模型命名，也不会与现有代码产生耦合。
  仅依赖 Flask / SQLAlchemy（现有环境已有）+ Python 标准库，零新增第三方依赖。

⚠️ 上线前必须核对：
  - 现有表的真实列名/表名（策划案称来自线上实测，仍建议 PRAGMA table_info 复核）
  - SQLite 版本、batches 表是否允许新增 normal_channel / sensitive_channel 列
  - 服务器域名、HTTPS 证书、环境变量（密钥绝不写进代码或仓库）
"""

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

# ---------------------------------------------------------------------------
# 配置：全部来自服务端环境变量；小程序前端永远拿不到 appsecret / session_key
# ---------------------------------------------------------------------------
WX_APPID = os.environ.get('WX_APPID', '')
WX_APPSECRET = os.environ.get('WX_APPSECRET', '')
TOKEN_SECRET = os.environ.get('WX_TOKEN_SECRET', '')
ADMIN_OPENIDS = set(filter(None, os.environ.get('WX_ADMIN_OPENIDS', '').split(',')))
# audit = 提交后由管理端审核（默认，最稳）；phone = 校验手机尾号后直接绑定
BIND_POLICY = os.environ.get('WX_BIND_POLICY', 'audit')
TOKEN_TTL = int(os.environ.get('WX_TOKEN_TTL', '7200'))
RATE_LIMIT_PER_MIN = int(os.environ.get('WX_RATE_LIMIT', '120'))

# 需求变更 2026-11-05：管理端二重验证 + 客户端待确认隐藏普敏
# 管理密码只在服务端环境变量，前端永远拿不到
ADMIN_PIN = os.environ.get('WX_ADMIN_PIN', '')
ADMIN_SESSION_TTL = int(os.environ.get('WX_ADMIN_SESSION_TTL', '1800'))

# 微信云开发信任：云函数已用微信下发的真实 openid 调本服务，
# 通过 HMAC 签名头（WX_TOKEN_SECRET 为密钥）传递，避免客户端伪造。
WX_CLOUD_TRUST = os.environ.get('WX_CLOUD_TRUST') == '1'

# 微信头像存储：存在 wechat 服务同目录的 avatars/ 下，通过 /static/avatars/<file> 公开访问
AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'avatars')
AVATAR_MAX_BYTES = 2 * 1024 * 1024

# 「有记录客户」口径（2026-11-06 用户定义）：
#   只要系统在 incoming_packages / packed_packages 里有过该客户的任一记录，就算有记录；
#   完全没有记录的客户归入「意向池」，有货入库时自动摘出（动态判定，无需标记字段）。
HAS_RECORD_SQL = (
    '(EXISTS (SELECT 1 FROM incoming_packages ip WHERE ip.customer_id = c.id) '
    ' OR EXISTS (SELECT 1 FROM packed_packages pp WHERE pp.customer_id = c.id))'
)
NO_RECORD_SQL = 'NOT ' + HAS_RECORD_SQL

# 无主件口径与主系统 /anonymous 页面完全一致
ORPHAN_WHERE = "is_anonymous = 1 AND customer_id IS NULL AND status != '已退货'"

# 批次物流轨迹（小程序侧独立数据，主系统端不展示/不同步）
TRACK_GROUPS = ('普货', '敏货')
# current_node：管理端设置的「当前物流节点」key，客户端据此点亮进度（不是按填了几个日期推算）
TRACK_DDL = """
CREATE TABLE IF NOT EXISTS batch_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    internal_no TEXT DEFAULT '',
    transfer_no TEXT DEFAULT '',
    carton_count INTEGER DEFAULT 0,
    sail_date TEXT DEFAULT '',
    arrive_date TEXT DEFAULT '',
    pickup_date TEXT DEFAULT '',
    sign_date TEXT DEFAULT '',
    progress TEXT DEFAULT '',
    current_node TEXT DEFAULT '',
    node_data TEXT DEFAULT '',
    updated_at TEXT,
    updated_by TEXT,
    UNIQUE(batch_id, group_name)
)
"""
# 已有表的幂等加列（生产 batch_tracks 早于这些字段创建）
# node_data：JSON，按节点 key 存 {date, note}，与客户端 12 节点一一对应
TRACK_ADD_COLUMNS = (
    'ALTER TABLE batch_tracks ADD COLUMN current_node TEXT DEFAULT \'\'',
    'ALTER TABLE batch_tracks ADD COLUMN node_data TEXT DEFAULT \'\'',
)

# 批次账单「手动计费重覆盖」：管理端可对某个 客户×普/敏 组设置最终计费重，
# 覆盖系统逐包裹计算值；chargeable_weight 为 NULL/无记录 = 用系统计算值。
FREIGHT_OVERRIDE_DDL = """
CREATE TABLE IF NOT EXISTS batch_freight_override (
    batch_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    nature TEXT NOT NULL,
    chargeable_weight REAL,
    updated_at TEXT,
    PRIMARY KEY (batch_id, customer_id, nature)
)
"""
# 批次账单「派发」状态：管理端确认无误后点「派发账单」才推送给客户端查看
BATCH_BILL_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS batch_bill_dispatch (
    batch_id INTEGER PRIMARY KEY,
    dispatched INTEGER DEFAULT 0,
    dispatched_at TEXT
)
"""
# 批次「尺寸信息推送确认」：管理端推送包裹尺寸给客户，客户逐一确认无误后才允许派发账单
BATCH_DIM_PUSH_DDL = """
CREATE TABLE IF NOT EXISTS batch_dim_push (
    batch_id INTEGER PRIMARY KEY,
    pushed INTEGER DEFAULT 0,
    pushed_at TEXT
)
"""
BATCH_DIM_CONFIRM_DDL = """
CREATE TABLE IF NOT EXISTS batch_dim_confirm (
    batch_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    confirmed INTEGER DEFAULT 0,
    confirmed_at TEXT,
    PRIMARY KEY (batch_id, customer_id)
)
"""


def _cloud_identity():
    """若开启云信任且签名合法，返回受信 openid；否则 None。
    签名：HMAC-SHA256(openid + '.' + ts, WX_TOKEN_SECRET)，5 分钟有效。"""
    if not WX_CLOUD_TRUST or not TOKEN_SECRET:
        return None
    openid = request.headers.get('X-Sjs-Cloud-Openid', '')
    ts = request.headers.get('X-Sjs-Cloud-Ts', '')
    sig = request.headers.get('X-Sjs-Cloud-Sig', '')
    if not openid or not ts or not sig:
        return None
    try:
        if abs(time.time() - int(ts)) > 300:
            return None
    except ValueError:
        return None
    msg = ('%s.%s' % (openid, ts)).encode('utf-8')
    expect = hmac.new(TOKEN_SECRET.encode('utf-8'), msg, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expect, sig):
        return openid
    return None
HIDE_NATURE = os.environ.get('WX_HIDE_NATURE_UNTIL_CONFIRMED', '1') == '1'

# 需求变更 2026-11-05：绑定下拉展示「城市/区域 + 邮编」。
# 收货地址只到区域粒度（工业市、蒙市这类），不展示门牌，原样返回即可。
# area = 区域级原样展示（默认）；full = 详细地址，门牌数字打码
ADDRESS_LEVEL = os.environ.get('WX_ADDRESS_LEVEL', 'area')
MASK_ADDRESS = ADDRESS_LEVEL != 'area'
# 现有 customers 表的区域列名未在策划案中列明：区域类字段优先，详细地址列兜底
ADDRESS_COLUMNS = ('city', 'city_name', 'area', 'region', 'destination_city',
                   'pickup_city', 'destination', 'address', 'shipping_address',
                   'address_line', 'addr', 'detail_address')
_ADDRESS_CACHE = {'done': False, 'col': None}

INTENT_TYPES = ('all', 'none', 'normal_only', 'sensitive_only')
# 第四轮：三大类配送方式 × 货物范围
DELIVERY_TYPES = ('pooled', 'direct', 'none')
CARGO_SCOPES = ('all', 'normal_only', 'sensitive_only')
# 单独地址发货最低重量（公斤），与前端 DIRECT_MIN_WEIGHT_KG 保持一致
DIRECT_MIN_WEIGHT = float(os.environ.get('WX_DIRECT_MIN_WEIGHT', '12'))
# incoming_packages 是否有重量列未在策划案中列明，启动时探测
WEIGHT_COLUMNS = ('weight', 'weight_kg', 'total_weight', 'package_weight')
_WEIGHT_CACHE = {'done': False, 'col': None}

# packed_packages 的件数 / 计费重 / 运费 / 确认时间列名未在策划案中列明，按候选名探测
PACKED_COLUMNS = {
    # 件数：total_packages = 包裹内快递件数
    'item_count': ('total_packages',),
    # 品名：包裹级人工回填，多品名空白/全角空格分隔
    'contents': ('contents_description',),
    # 实重(kg) 与 体积重(kg)：计费重由二者取大实时算，不落库
    'weight': ('weight',),
    'volume_weight': ('volume_weight',),
    # 打包时间 / 发货时间分开取
    'created_at': ('created_at',),
    'shipped_at': ('shipped_at',),
    'is_vip_solo': ('is_vip_solo',),
    # 尺寸：线上就是三列 length/width/height，单位 cm
    'dim_length': ('length',),
    'dim_width': ('width',),
    'dim_height': ('height',),
}
# 三列拼接时用的单位，线上若存英寸改成 in
DIMENSION_UNIT = os.environ.get('WX_DIMENSION_UNIT', 'cm')

# 状态分组（线上实测枚举，2026-11-05 技术回复）
#   packed_packages: 已打包 / 待合箱 / 已合箱 / 已发货 / 已完成
#   客户视角：未发货的三种都算「已打包」，已发货与已完成都算「已发货」
PACKED_TAB_STATUSES = {
    'packed': ('已打包', '待合箱', '已合箱'),
    'shipped': ('已发货', '已完成'),
}
# incoming_packages: 已入库 = 待发货；待打包 = 仓库打包中（此前客户端完全看不到，443 条）
PENDING_STATUSES = ('已入库', '待打包')
# 远程确认待打包（2026-11-05 技术回复：路线 A）
# shipment_intents.scope=3 在本期被复用为「本周选件确认」：
#   batch_id 固定 0（确认是客户级/周级，不绑批次）、package_id 固定 NULL
#   item_ids 存客户勾选的 incoming_packages.id 列表（JSON）
# 操作端只读本表；小程序绝不调用 assign_batch / batch_confirm。
CONFIRM_SCOPE = 3
CONFIRM_WEEK_BUCKET = 0
_SI_CACHE = {'done': False, 'cols': set()}

# 计费重规则（与系统 calc_chargeable_weight 一致）
MIN_CHARGEABLE_KG = 1.0
CHARGEABLE_STEP_KG = 0.5
# 运费单价（元/kg）：普货 25，敏货 35（用户确认的业务规则）
FREIGHT_RATE_NORMAL = 25.0
FREIGHT_RATE_SENSITIVE = 35.0
# 计入「待付运费」的包裹状态：一旦打包即产生运费，已发货/已完成同理
_FREIGHT_PACKED_STATUSES = ('已打包', '待合箱', '已合箱', '已发货', '已完成')
# 把状态元组拼成 SQL IN 字面量，避免 SQLAlchemy 绑定元组的问题
_FREIGHT_STATUS_IN = ", ".join("'%s'" % s for s in _FREIGHT_PACKED_STATUSES)
_PACKED_CACHE = {'done': False, 'cols': {}}


def _is_sensitive(nature):
    """统一判定：nature 含「敏」(或英文 sensitive/ming) 即敏货，其余按普货计。

    全系统运费分摊口径的唯一真相源——所有按普/敏取单价、做筛分的代码都应走这里，
    避免各处散落 '敏' in nature 判断导致不一致。
    """
    s = str(nature or '').strip().lower()
    return ('敏' in s) or (s in ('sensitive', 'ming'))

# 包裹「内含单号」：线上关联表是 packed_package_items(packed_package_id, incoming_package_id)。
# 探测顺序仍是「关联表 → incoming 外键 → packed ID 列表 → 降级」，但正常部署只会命中第一个；
# 后两个分支只用于兼容不同版本，绝不用 batch_id + customer_id 去猜（同批次多包裹会串包）。
# 注意：品名不在入库表，而是包裹级汇总 packed_packages.contents_description。
ITEM_LINK_TABLES = ('packed_package_items', 'packed_items', 'package_items',
                    'batch_package_items', 'packing_items')
ITEM_FK_COLUMNS = ('packed_package_id', 'packed_id', 'pack_id', 'packed_pk_id')
ITEM_IDS_COLUMNS = ('incoming_ids', 'package_ids', 'item_ids', 'package_id_list')
ITEM_MAX_PER_PACKAGE = 50
_ITEM_LINK_CACHE = {'done': False, 'link': {}}
IDENTIFIER_COLUMN = {
    'name': 'name',
    'customer_no': 'customer_no',
    'barcode': 'barcode'
}

# 普敏归一化表达式：脏值兜底顺序 = 原始 nature → packed_packages.nature（策划案 13.6）
# 普敏属性的权威来源是打包记录：操作员在打包时才判定并写入 packed_packages.nature。
# incoming_packages.nature 只是入库时的粗标（可能为空/脏值），仅作兜底。
NATURE_EXPR = """
CASE
  WHEN EXISTS (SELECT 1 FROM packed_packages pp
               WHERE pp.customer_id = p.customer_id AND pp.batch_id = p.batch_id
                 AND pp.nature LIKE '%敏%') THEN '敏货'
  WHEN EXISTS (SELECT 1 FROM packed_packages pp
               WHERE pp.customer_id = p.customer_id AND pp.batch_id = p.batch_id
                 AND pp.nature LIKE '%普%') THEN '普货'
  WHEN p.nature LIKE '%敏%' THEN '敏货'
  WHEN p.nature LIKE '%普%' THEN '普货'
  ELSE '待核实'
END
"""

# 打包前（已入库 / 待打包）属性一律未知；已打包及之后才有判定
NATURE_KNOWN_STATUSES = ('已打包', '已发货', '已完成')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _ok(data=None, message='ok'):
    return jsonify({'code': 0, 'message': message, 'data': data if data is not None else {}})


def _err(message, http_status=200, code=40000):
    return jsonify({'code': code, 'message': message, 'data': {}}), http_status


def _rows(db, sql, params=None):
    result = db.session.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


def _one(db, sql, params=None):
    rows = _rows(db, sql, params)
    return rows[0] if rows else None


def _scalar(db, sql, params=None):
    result = db.session.execute(text(sql), params or {})
    row = result.first()
    return row[0] if row else 0


def _exec(db, sql, params=None):
    db.session.execute(text(sql), params or {})
    db.session.commit()


# ---------------------------------------------------------------------------
# 自定义登录态：HMAC-SHA256 签名 token（不引入 PyJWT 等新依赖）
# token = base64url(payload).base64url(signature)，payload 里只有 openid + exp
# ---------------------------------------------------------------------------
def _address_column(db):
    """探测 customers 表的地址列名。
    策划案 2.1 只列了 zip_code，未确认 address 是否存在；
    找不到就返回 None，相关接口降级为只展示邮编，不阻塞绑定流程。"""
    if _ADDRESS_CACHE['done']:
        return _ADDRESS_CACHE['col']
    try:
        cols = [row['name'] for row in _rows(db, 'PRAGMA table_info(customers)')]
        for name in ADDRESS_COLUMNS:
            if name in cols:
                _ADDRESS_CACHE['col'] = name
                break
    except Exception:
        _ADDRESS_CACHE['col'] = None
    _ADDRESS_CACHE['done'] = True
    return _ADDRESS_CACHE['col']


def _display_address(addr):
    """区域展示：默认原样返回「城市/区域」（如 工业市、蒙市），不做掩码。
    只有显式配置 WX_ADDRESS_LEVEL=full 时，才按详细地址处理并把门牌数字打码。"""
    s = str(addr or '').strip()
    if not s:
        return ''
    if ADDRESS_LEVEL != 'area':
        return re.sub(r'\d+', '***', s)
    # 区域名本身不具定位性；这里只做长度上限，防止误存整段详细地址
    return s[:40]


def _packed_columns(db):
    """探测 packed_packages 的件数/计费重/运费/确认时间列；缺失的返回 None，接口降级为 NULL。"""
    if _PACKED_CACHE['done']:
        return _PACKED_CACHE['cols']
    try:
        cols = {row['name'] for row in _rows(db, 'PRAGMA table_info(packed_packages)')}
    except Exception:
        cols = set()
    found = {}
    for key, candidates in PACKED_COLUMNS.items():
        found[key] = next((c for c in candidates if c in cols), None)
    _PACKED_CACHE['done'] = True
    _PACKED_CACHE['cols'] = found
    return found


def _weight_column(db):
    """探测 incoming_packages 的重量列；找不到返回 None，前端降级为「仓库称重后确认」。"""
    if _WEIGHT_CACHE['done']:
        return _WEIGHT_CACHE['col']
    try:
        cols = [row['name'] for row in _rows(db, 'PRAGMA table_info(incoming_packages)')]
        for name in WEIGHT_COLUMNS:
            if name in cols:
                _WEIGHT_CACHE['col'] = name
                break
    except Exception:
        _WEIGHT_CACHE['col'] = None
    _WEIGHT_CACHE['done'] = True
    return _WEIGHT_CACHE['col']


def _table_columns(db, table):
    """取表列名列表；表不存在或查询失败返回空列表（调用方按缺失处理）。"""
    try:
        return [row['name'] for row in _rows(db, 'PRAGMA table_info(%s)' % table)]
    except Exception:
        return []


def _item_link(db):
    """探测「包裹 → 内含入库单」的关联方式，结果缓存在进程内。

    线上基准：关联表 packed_package_items(packed_package_id, incoming_package_id)，
    正常部署命中 link_table。fk / ids 两个分支仅为兼容不同版本保留。
    """
    if _ITEM_LINK_CACHE['done']:
        return _ITEM_LINK_CACHE['link']

    link = {'mode': 'none', 'fk': None, 'ids': None,
            'table': None, 'parent': None, 'child': None}

    inc_cols = _table_columns(db, 'incoming_packages')
    packed_cols = _table_columns(db, 'packed_packages')

    try:
        tables = {row['name'] for row in
                  _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        tables = set()

    # ① 关联表（线上就是这条）
    for t in ITEM_LINK_TABLES:
        if t not in tables:
            continue
        cols = _table_columns(db, t)
        parent = next((c for c in ('packed_package_id', 'packed_id', 'package_id')
                       if c in cols), None)
        child = next((c for c in ('incoming_package_id', 'incoming_id', 'package_item_id')
                      if c in cols), None)
        if parent and child:
            link.update({'mode': 'link_table', 'table': t, 'parent': parent, 'child': child})
            break

    if link['mode'] == 'none':
        fk = next((c for c in ITEM_FK_COLUMNS if c in inc_cols), None)
        if fk:
            link.update({'mode': 'fk', 'fk': fk})
        else:
            ids = next((c for c in ITEM_IDS_COLUMNS if c in packed_cols), None)
            if ids:
                link.update({'mode': 'ids', 'ids': ids})

    _ITEM_LINK_CACHE['done'] = True
    _ITEM_LINK_CACHE['link'] = link
    return link


def _split_ids(raw):
    """把 '1,2,3' / '[1,2,3]' / '1|2' / 列表 统一解析成正整数列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        seq = list(raw)
    else:
        text_value = str(raw).strip()
        if not text_value:
            return []
        seq = None
        if text_value.startswith('['):
            try:
                seq = json.loads(text_value)
            except Exception:
                seq = None
        if seq is None:
            seq = re.split(r'[^0-9]+', text_value)
    out = []
    for x in seq:
        t = str(x).strip()
        if t.isdigit() and int(t) > 0:
            out.append(int(t))
    return out


def _split_goods(raw):
    """品名拆分：系统约定多品名以空白或全角空格分隔（源码导出开票同规则）。"""
    if raw is None:
        return []
    text_value = str(raw).strip()
    if not text_value:
        return []
    return [p for p in re.split(r'[\s\u3000]+', text_value) if p]


def _fmt_num(value):
    """45.0 -> 45，45.50 -> 45.5，非数字原样返回。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if num == int(num):
        return str(int(num))
    return ('%.2f' % num).rstrip('0').rstrip('.')


def _dimension_display(row):
    """拼包裹尺寸：三列齐全优先（45 × 35 × 30 cm），否则用单列描述原文。"""
    parts = [row.get('dim_length'), row.get('dim_width'), row.get('dim_height')]
    if all(p not in (None, '') for p in parts):
        return ' × '.join(_fmt_num(p) for p in parts) + ' ' + DIMENSION_UNIT

    single = row.get('dimensions_raw')
    if single not in (None, ''):
        text_value = str(single).strip()
        if text_value:
            return text_value
    return ''


def _num_or_none(value):
    """把 DB 里的数值安全转 float，空/异常返回 None。"""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _chargeable_weight(row):
    """计费重 = max(体积重, 实重, 1.0)，向上进位到 0.5kg。

    与系统 calc_chargeable_weight 保持一致：math.ceil(x * 2) / 2，最小 1kg。
    两者都没有时返回 None（不显示 0，避免看起来像「免费」）。
    """
    values = []
    for key in ('volume_weight', 'weight'):
        v = _num_or_none(row.get(key))
        if v is not None:
            values.append(v)
    if not values:
        return None
    base = max([max(values), MIN_CHARGEABLE_KG])
    return math.ceil(base / CHARGEABLE_STEP_KG) * CHARGEABLE_STEP_KG


def _nature_sql_fragment(grp):
    """按普/敏生成 WHERE 片段（与 _is_sensitive 口径一致）。

    注意：内含 LIKE '%敏%' 的百分号，不能用 % 格式化拼接 SQL，否则 Python 会
    把 % 当格式符而报错；调用处一律用字符串相加拼接。
    """
    if grp == 'sensitive':
        return " AND (nature LIKE '%敏%' OR LOWER(nature) IN ('sensitive','ming'))"
    return " AND (nature IS NULL OR (nature NOT LIKE '%敏%' AND LOWER(nature) NOT IN ('sensitive','ming')))"


def _group_computed_chargeable(db, bid, cid, grp):
    """某客户在某批次、指定普/敏组、未结算包裹的计费重之和（系统逐包裹计算口径）。"""
    rows = _rows(db,
        "SELECT weight, volume_weight FROM packed_packages "
        "WHERE batch_id = :b AND customer_id = :c AND status IN (" + _FREIGHT_STATUS_IN + ") "
        "AND (freight_settled IS NULL OR freight_settled = 0)" + _nature_sql_fragment(grp),
        {'b': bid, 'c': cid})
    total = 0.0
    for r in rows:
        cw = _chargeable_weight(r)
        if cw is not None:
            total += cw
    return round(total, 1)


def _group_effective_chargeable(db, bid, cid, grp):
    """返回 (effective, overridden)。

    effective = 手动覆盖值（若有，来自 batch_freight_override）否则系统计算值。
    这样「管理端手动改计费重 → 运费随之更新 → 派发后客户端看到一致金额」。
    """
    ov = _one(db,
        "SELECT chargeable_weight FROM batch_freight_override "
        "WHERE batch_id = :b AND customer_id = :c AND nature = :n",
        {'b': bid, 'c': cid, 'n': grp})
    if ov is not None and ov['chargeable_weight'] is not None:
        return float(ov['chargeable_weight']), True
    return _group_computed_chargeable(db, bid, cid, grp), False


def _freight_bills(db, cid, limit=20):
    """客户运费账单列表（按 customer_id 关联，不归因到包裹）。

    实测 freight_records.carton_id 全为 NULL，无法经 carton_items 找到对应箱子，
    因此包裹级金额算不出来——只做客户级展示，绝不给每张卡片编一个金额。
    """
    try:
        rows = _rows(db, """
            SELECT id, amount, status, carton_id, created_at, settled_at
            FROM freight_records
            WHERE customer_id = :cid
            ORDER BY id DESC
            LIMIT :lim
        """, {'cid': cid, 'lim': limit})
    except Exception:
        return []
    for r in rows:
        r['amount'] = _num_or_none(r.get('amount'))
    return rows


def _unsettled_total(db, cid):
    """未结算运费合计：freight_records 里 status='待结算' 的金额之和。"""
    try:
        value = _scalar(db, """
            SELECT SUM(amount) FROM freight_records
            WHERE customer_id = :cid AND status = '待结算'
        """, {'cid': cid})
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _clean_nature(raw):
    """普/敏归一化。

    线上 incoming_packages.nature 有「已入库」11 条、「已打包」979 条这类脏值
    （历史把状态写进了属性列）。只认「普」「敏」，脏值返回空串，
    前端不显示标签，而不是把状态当作属性展示给客户。
    """
    if raw is None:
        return ''
    text_value = str(raw).strip()
    if not text_value:
        return ''
    if '敏' in text_value:
        return '敏货'
    if '普' in text_value:
        return '普货'
    return ''


def _now_dt():
    return datetime.now()


def _week_label(when=None):
    """ISO 周标签，如 2026-W45。确认按周覆盖，跨周自然失效。"""
    dt = when or _now_dt()
    iso = dt.isocalendar()
    return '%04d-W%02d' % (iso[0], iso[1])


def _confirm_deadline():
    """本周确认截止提示：周三 24:00（系统周三建批次）。"""
    now = _now_dt()
    days = (2 - now.weekday()) % 7          # weekday: 周三 = 2
    if days == 0 and now.hour >= 12:        # 周三中午后顺延一周
        days = 7
    target = now + timedelta(days=days)
    return target.strftime('%m-%d') + ' 24:00'


def _si_columns(db):
    """探测 shipment_intents 是否已升级到周确认版（新列存在与否）。"""
    if _SI_CACHE['done']:
        return _SI_CACHE['cols']
    try:
        cols = {r['name'] for r in _rows(db, 'PRAGMA table_info(shipment_intents)')}
    except Exception:
        cols = set()
    _SI_CACHE['done'] = True
    _SI_CACHE['cols'] = cols
    return cols


def _get_week_intent(db, cid, week):
    """取该客户本周的确认记录；表未升级或查询失败返回 None。"""
    if 'week_label' not in _si_columns(db):
        return None
    try:
        return _one(db, """
            SELECT id, customer_id, week_label, item_ids, item_count,
                   intent_status, confirmed_at, operator_locked_at, remark,
                   sync_snapshot
            FROM shipment_intents
            WHERE customer_id = :cid AND scope = :sc AND week_label = :wk
            LIMIT 1
        """, {'cid': cid, 'sc': CONFIRM_SCOPE, 'wk': week})
    except Exception:
        return None


def _upsert_week_intent(db, cid, week, item_ids, remark):
    """写入/覆盖本周确认（同客户同周一条）。"""
    stamp = _now()
    payload = json.dumps(item_ids, ensure_ascii=False)
    existing = _get_week_intent(db, cid, week)
    if existing:
        _exec(db, """
            UPDATE shipment_intents
            SET item_ids = :ids, item_count = :cnt, intent_status = 'active',
                confirmed_at = :t, remark = :rm, updated_at = :t
            WHERE id = :id
        """, {'ids': payload, 'cnt': len(item_ids), 't': stamp,
              'rm': remark, 'id': existing['id']})
        return existing['id']

    _exec(db, """
        INSERT INTO shipment_intents
            (customer_id, batch_id, scope, package_id, delivery_type, cargo_scope,
             intent_type, remark, status, created_at, updated_at,
             week_label, item_ids, item_count, confirmed_at, intent_status)
        VALUES
            (:cid, :bk, :sc, NULL, 'pooled', 'all',
             'all', :rm, 0, :t, :t,
             :wk, :ids, :cnt, :t, 'active')
    """, {'cid': cid, 'bk': CONFIRM_WEEK_BUCKET, 'sc': CONFIRM_SCOPE,
          'rm': remark, 't': stamp, 'wk': week,
          'ids': payload, 'cnt': len(item_ids)})
    row = _one(db, 'SELECT last_insert_rowid() AS id')
    return row['id'] if row else None


def _try_log(db, action, note):
    """尽力写 operation_logs；表结构不符时静默跳过，不阻塞主流程。"""
    try:
        cols = {r['name'] for r in _rows(db, 'PRAGMA table_info(operation_logs)')}
        if not cols or 'action' not in cols:
            return
        fields = ['action']
        holders = [':action']
        params = {'action': action}
        if 'detail' in cols:
            fields.append('detail')
            holders.append(':note')
            params['note'] = note
        if 'created_at' in cols:
            fields.append('created_at')
            holders.append(':t')
            params['t'] = _now()
        _exec(db, 'INSERT INTO operation_logs (%s) VALUES (%s)'
              % (', '.join(fields), ', '.join(holders)), params)
    except Exception:
        pass


def _pending_freight(db, cid):
    """客户待付运费合计（实时按包裹核算）。

    根因修复：原先直接读 customers.pending_freight——该字段是初始化时批量刷成的
    占位值（全库大量客户均为 75.0），并非按包裹计算，导致小程序展示的「待付运费」
    与真实货量严重不符。现改为实时汇算：

        遍历客户所有已打包/已发货/已完成包裹，
        单件运费 = 计费重 × 单价（普货 25 元/kg、敏货 35 元/kg），
        计费重   = max(体积重, 实重, 1kg) 向上进位 0.5kg（复用 _chargeable_weight）。

    运费账单（freight_records）一律不动，仅修正「待付运费」的展示取数逻辑。
    出错时降级为读原字段，避免页面崩溃。
    """
    try:
        rows = _rows(db,
            "SELECT nature, weight, volume_weight FROM packed_packages "
            "WHERE customer_id = :cid AND status IN (%s) "
            "AND (freight_settled IS NULL OR freight_settled = 0)" % _FREIGHT_STATUS_IN,
            {'cid': cid})
        total = 0.0
        for r in rows:
            cw = _chargeable_weight(r)
            if cw is None:
                continue
            nature = str(r.get('nature') or '')
            rate = FREIGHT_RATE_SENSITIVE if ('敏' in nature or nature.lower() in ('sensitive', 'ming')) \
                else FREIGHT_RATE_NORMAL
            total += cw * rate
        return round(total, 2)
    except Exception:
        # 降级：读原字段，避免展示中断
        try:
            v = _scalar(db, 'SELECT pending_freight FROM customers WHERE id = :cid', {'cid': cid})
            return round(float(v or 0), 2)
        except Exception:
            return 0.0


def _freight_map(db):
    """全客户待付运费映射（一次查询，供管理端客户总览列表用）。

    口径与 _pending_freight 完全一致：计费重 ×（普 25 / 敏 35）。
    """
    try:
        rows = _rows(db,
            "SELECT customer_id, nature, weight, volume_weight FROM packed_packages "
            "WHERE customer_id IS NOT NULL AND status IN (%s) "
            "AND (freight_settled IS NULL OR freight_settled = 0)" % _FREIGHT_STATUS_IN)
        total = {}
        for r in rows:
            cw = _chargeable_weight(r)
            if cw is None:
                continue
            key = int(r['customer_id'])
            nature = str(r.get('nature') or '')
            rate = FREIGHT_RATE_SENSITIVE if ('敏' in nature or nature.lower() in ('sensitive', 'ming')) \
                else FREIGHT_RATE_NORMAL
            total[key] = round(total.get(key, 0.0) + cw * rate, 2)
        return total
    except Exception:
        return {}


def _strip_serial(raw):
    """serial_label 内部形如 '1-1#8'（# 后是自增 id 用于去重），对外只显示前半段。"""
    if raw is None:
        return ''
    return str(raw).split('#')[0].strip()


def _packed_tab_where(db, tab, params):
    """构造 packed_packages 的 tab 筛选条件（不含客户与关键词条件）。

    关键：「已打包」是 incoming_packages.status（线上 18,834 条），
    packed_packages.status 几乎不写这个值（实测只有 已发货/已完成/待合箱）。
    所以状态以内含件的状态为准，按包裹聚合，避免按件重复展示。
    """
    where = []
    link = _item_link(db)

    if link['mode'] == 'link_table':
        # 只替换表名/列名，保留 {op} 供后续 format 使用
        tpl = ('EXISTS (SELECT 1 FROM {table} pi '
               'JOIN incoming_packages ip ON ip.id = pi.{child} '
               'WHERE pi.{parent} = pp.id AND ip.status {op})')
        for key, value in (('{table}', link['table']), ('{parent}', link['parent']),
                           ('{child}', link['child'])):
            tpl = tpl.replace(key, value)
        if tab == 'packed':
            where.append("(pp.status IS NULL OR pp.status NOT IN ('已发货', '已完成'))")
            where.append(tpl.format(op="= '已打包'"))
        else:
            where.append("(pp.status IN ('已发货', '已完成') OR "
                         + tpl.format(op="IN ('已发货', '已完成')") + ")")
        return where

    # 关联表缺失时退回 packed.status，会漏数据但不会报错
    statuses = PACKED_TAB_STATUSES[tab]
    holders = []
    for i, value in enumerate(statuses):
        key = 'st%d' % i
        params[key] = value
        holders.append(':' + key)
    where.append('pp.status IN (%s)' % ', '.join(holders))
    return where


def _attach_items(db, rows, tab):
    """给已打包 / 已发货的包裹补「内含快递单号」：items=[{id, tracking_number}]。

    品名是包裹级汇总（contents_description），不逐件返回；这里只解决"箱子里有哪几张单"。
    """
    for r in rows:
        r['items'] = []
        r['items_available'] = False
        r['items_truncated'] = False
    if not rows:
        return

    link = _item_link(db)
    if link['mode'] == 'none':
        return

    params = {}
    holders = []
    for i, r in enumerate(rows):
        key = 'pk%d' % i
        params[key] = r['id']
        holders.append(':' + key)
    in_clause = ', '.join(holders)

    detail_rows = []
    try:
        if link['mode'] == 'link_table':
            detail_rows = _rows(db, """
                SELECT l.{parent} AS pp_id, p.id, p.tracking_number
                FROM {table} l
                JOIN incoming_packages p ON p.id = l.{child}
                WHERE l.{parent} IN ({in_clause})
                ORDER BY p.id
            """.format(parent=link['parent'], child=link['child'],
                       table=link['table'], in_clause=in_clause), params)
        elif link['mode'] == 'fk':
            detail_rows = _rows(db, """
                SELECT p.{fk} AS pp_id, p.id, p.tracking_number
                FROM incoming_packages p
                WHERE p.{fk} IN ({in_clause})
                ORDER BY p.id
            """.format(fk=link['fk'], in_clause=in_clause), params)
        else:
            holding = _rows(db, """
                SELECT pp.id AS pp_id, pp.{ids} AS ids
                FROM packed_packages pp
                WHERE pp.id IN ({in_clause})
            """.format(ids=link['ids'], in_clause=in_clause), params)
            mapping = {}
            all_ids = set()
            for h in holding:
                seq = _split_ids(h.get('ids'))
                mapping[h['pp_id']] = seq
                all_ids.update(seq)
            if all_ids:
                p2 = {}
                h2 = []
                for i, v in enumerate(sorted(all_ids)):
                    k = 'ip%d' % i
                    p2[k] = v
                    h2.append(':' + k)
                found = _rows(db, """
                    SELECT p.id, p.tracking_number
                    FROM incoming_packages p
                    WHERE p.id IN ({ph})
                    ORDER BY p.id
                """.format(ph=', '.join(h2)), p2)
                by_id = {d['id']: d for d in found}
                for pp_id, seq in mapping.items():
                    for iid in seq:
                        d = by_id.get(iid)
                        if d:
                            detail_rows.append(dict(d, pp_id=pp_id))
    except Exception:
        # 探测偏差不该让列表 500，降级为空明细
        detail_rows = []

    bucket = defaultdict(list)
    for d in detail_rows:
        bucket[d.get('pp_id')].append({
            'id': d.get('id'),
            'tracking_number': d.get('tracking_number') or ''
        })

    for r in rows:
        items = bucket.get(r['id'], [])
        r['items_available'] = True
        if len(items) > ITEM_MAX_PER_PACKAGE:
            items = items[:ITEM_MAX_PER_PACKAGE]
            r['items_truncated'] = True
        r['items'] = items


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64decode(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _sign_token(openid):
    if not TOKEN_SECRET:
        raise RuntimeError('WX_TOKEN_SECRET 未配置')
    payload = json.dumps(
        {'openid': openid, 'exp': int(time.time()) + TOKEN_TTL},
        separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')
    body = _b64encode(payload)
    sig = _b64encode(hmac.new(TOKEN_SECRET.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    return body + '.' + sig


def _verify_token(token):
    try:
        body, sig = token.split('.', 1)
        expected = _b64encode(hmac.new(TOKEN_SECRET.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64decode(body).decode('utf-8'))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _sign_scoped_token(openid, scope, ttl):
    """管理端二重验证通过后签发的短时效 token，scope 防止被当普通登录态使用"""
    if not TOKEN_SECRET:
        raise RuntimeError('WX_TOKEN_SECRET 未配置')
    payload = json.dumps(
        {'openid': openid, 'scope': scope, 'exp': int(time.time()) + ttl},
        separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')
    body = _b64encode(payload)
    sig = _b64encode(hmac.new(TOKEN_SECRET.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    return body + '.' + sig


def _verify_scoped_token(token, scope):
    payload = _verify_token(token)
    if not payload or payload.get('scope') != scope:
        return None
    return payload


def _code2session(code):
    """服务端用 code 换 openid。session_key 只留在服务端内存，不下发、不落库。"""
    if not WX_APPID or not WX_APPSECRET:
        # 本地演示回退：WX_DEV_FALLBACK=1 且未配 appid 时，用固定 dev 身份
        # （openid 稳定，绑定关系跨重启保留；生产必须配置真实 appid，不会走到这里）
        if os.environ.get('WX_DEV_FALLBACK') == '1':
            return 'wxdev_local', ''
        raise RuntimeError('WX_APPID / WX_APPSECRET 未配置')
    # 接口地址与参数以微信官方文档「小程序登录 / code2Session」为准
    qs = urllib.parse.urlencode({
        'appid': WX_APPID,
        'secret': WX_APPSECRET,
        'js_code': code,
        'grant_type': 'authorization_code'
    })
    url = 'https://api.weixin.qq.com/sns/jscode2session?' + qs
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if data.get('errcode'):
        raise RuntimeError('微信登录失败：' + str(data.get('errmsg') or data.get('errcode')))
    if not data.get('openid'):
        raise RuntimeError('微信登录失败：未返回 openid')
    return data['openid'], data.get('unionid', '')


def _profile_of(db, user):
    """组装前端需要的客户资料；手机号只返回掩码，不返回完整号码。
    管理员身份由 WX_ADMIN_OPENIDS 白名单动态判定（不依赖入库时写死的 role 列），
    这样新增/移除白名单会立即反映在客户端，无需改库。"""
    # 管理员身份：仅由 WX_ADMIN_OPENIDS 白名单判定（2026-11-06 收紧，
    # 不再回退到入库时写入的 role 列，白名单是唯一门槛）
    _role = 1 if (user and user.get('openid') in ADMIN_OPENIDS) else 0
    if not user or not user.get('customer_id'):
        return {'bind_status': int(user.get('bind_status', 0)) if user else 0,
                'role': _role,
                'nickname': (user.get('nickname') or '') if user else '',
                'avatar': (user.get('avatar') or '') if user else '',
                'customer': None}
    c = _one(db, """
        SELECT id, name, customer_no, barcode, zip_code, is_vip, phone,
               invoice_contact, invoice_phone, invoice_address, invoice_zip
        FROM customers WHERE id = :cid LIMIT 1
    """, {'cid': user['customer_id']}) or {}
    phone = str(c.get('phone') or '')
    is_vip = int(c.get('is_vip') or 0)
    # VIP 单独发货地址：系统操作员在后台维护的收件人/电话/详细地址/邮编，
    # 推送给客户端，选「单独地址发货」时自动预填，无需重复录入。
    vip_address = None
    if is_vip:
        vip_address = {
            'name': str(c.get('invoice_contact') or ''),
            'phone': str(c.get('invoice_phone') or ''),
            'line': str(c.get('invoice_address') or ''),
            'zip': str(c.get('invoice_zip') or '')
        }
    return {
        'bind_status': int(user.get('bind_status', 0)),
        'role': _role,
        'nickname': user.get('nickname') or '',
        'avatar': user.get('avatar') or '',
        'customer': {
            'id': c.get('id'),
            'name': c.get('name'),
            'customer_no': c.get('customer_no'),
            'barcode': c.get('barcode'),
            'zip_code': c.get('zip_code'),
            'is_vip': is_vip,
            'phone_mask': ('****' + phone[-4:]) if len(phone) >= 4 else '',
            'vip_address': vip_address
        }
    }


def create_wechat_blueprint(db):
    """工厂函数：闭包持有现有项目的 db 对象，避免与 app.py 的模型命名耦合"""
    bp = Blueprint('wechat_api', __name__)
    hits = defaultdict(list)
    admin_fails = defaultdict(list)   # openid -> 最近失败时间戳
    admin_lock = {}                   # openid -> 锁定截止时间

    # 幂等建表：小程序侧独立表（服务重启自动补齐，不依赖手工迁移脚本）
    try:
        _exec(db, TRACK_DDL)
    except Exception:
        db.session.rollback()
    # 老表补列（重复加列会报错，忽略即可）
    for ddl in TRACK_ADD_COLUMNS:
        try:
            _exec(db, ddl)
        except Exception:
            db.session.rollback()
    # 批次账单：手动计费重覆盖 + 派发状态 + 尺寸信息推送确认（幂等建表）
    for ddl in (FREIGHT_OVERRIDE_DDL, BATCH_BILL_DISPATCH_DDL, BATCH_DIM_PUSH_DDL, BATCH_DIM_CONFIRM_DDL):
        try:
            _exec(db, ddl)
        except Exception:
            db.session.rollback()
    # VIP 单独发货批次：在 batches 表加标记列（幂等加列，重复加列忽略）
    for ddl in (
        'ALTER TABLE batches ADD COLUMN vip_customer_id INTEGER DEFAULT NULL',
        "ALTER TABLE batches ADD COLUMN vip_day TEXT DEFAULT ''",
    ):
        try:
            _exec(db, ddl)
        except Exception:
            db.session.rollback()

    @bp.before_request
    def _rate_limit():
        # 单进程内存限流，够开发/灰度用；生产请放到 Nginx limit_req（策划案 13.4）
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0]
        key = ip + '|' + request.path
        now = time.time()
        recent = [t for t in hits[key] if now - t < 60]
        if len(recent) >= RATE_LIMIT_PER_MIN:
            return _err('请求过于频繁，请稍后再试', 429, 40029)
        recent.append(now)
        hits[key] = recent
        return None

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            cloud_oid = _cloud_identity()
            if cloud_oid:
                user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': cloud_oid})
                if not user:
                    return _err('用户不存在', 401, 40001)
                g.openid = cloud_oid
                g.user = user
                return fn(*args, **kwargs)
            header = request.headers.get('Authorization', '')
            if not header.startswith('Bearer '):
                return _err('请先登录', 401, 40001)
            payload = _verify_token(header[7:])
            if not payload:
                return _err('登录已过期', 401, 40001)
            user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': payload['openid']})
            if not user:
                return _err('用户不存在', 401, 40001)
            g.openid = payload['openid']
            g.user = user
            return fn(*args, **kwargs)
        return wrapper

    def require_admin(fn):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            # 2026-11-06 起管理端只保留一道门槛：openid 白名单。
            # 原第二重「管理密码解锁」（X-Admin-Token 短时效 token）已下线——
            # 管理员打开小程序直接进管理端，不再需要输 <DEV_ADMIN_PWD_HASH> 解锁。
            if g.openid not in ADMIN_OPENIDS:
                return _err('无管理端权限', 200, 40003)
            return fn(*args, **kwargs)
        return wrapper

    def require_bound_customer():
        if int(g.user.get('bind_status') or 0) != 2 or not g.user.get('customer_id'):
            return None
        return int(g.user['customer_id'])

    def _audit_log(action, detail):
        """复用现有日志表；表结构未知时静默跳过，不影响主流程。
        注意：生产 operation_logs 的正文列名是 content（原写 detail 会被静默吞掉）。"""
        try:
            _exec(db, """
                INSERT INTO operation_logs (action, content, operator, created_at)
                VALUES (:a, :d, :o, :t)
            """, {'a': action, 'd': detail, 'o': g.openid, 't': _now()})
        except Exception:
            db.session.rollback()

    # =======================================================================
    # 客户端 API
    # =======================================================================
    @bp.route('/healthz', methods=['GET'])
    def healthz():
        return _ok({'status': 'up', 'cloud_trust': WX_CLOUD_TRUST})

    @bp.route('/api/client/login', methods=['POST'])
    def client_login():
        body = request.get_json(silent=True) or {}
        cloud_oid = _cloud_identity()
        if cloud_oid:
            openid, unionid = cloud_oid, ''
        else:
            code = str(body.get('code') or '').strip()
            if not code:
                return _err('缺少 code')
            try:
                openid, unionid = _code2session(code)
            except Exception as e:
                return _err(str(e), 200, 40010)

        user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': openid})
        if not user:
            role = 1 if openid in ADMIN_OPENIDS else 0
            _exec(db, """
                INSERT INTO client_users (openid, unionid, bind_status, role, created_at, updated_at)
                VALUES (:o, :u, 0, :r, :t, :t)
            """, {'o': openid, 'u': unionid, 'r': role, 't': _now()})
            user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': openid})

        token = _sign_token(openid)
        return _ok({'token': token, 'profile': _profile_of(db, user)})

    @bp.route('/api/client/customers/search', methods=['GET'])
    @require_auth
    def client_customer_search():
        """按客户名模糊搜索，结果脱敏；需登录 + 限流 + 最多 10 条，防客户档案枚举"""
        kw = str(request.args.get('keyword') or '').strip()
        if not kw:
            return _err('请输入客户名、客户号或邮编')
        addr_col = _address_column(db)
        addr_expr = ('c.' + addr_col) if addr_col else "''"
        rows = _rows(db, """
            SELECT c.id, c.name, c.customer_no, c.zip_code, {addr} AS address
            FROM customers c
            WHERE c.name LIKE :kw OR c.customer_no LIKE :kw OR c.barcode LIKE :kw
                  OR c.zip_code = :zip
            ORDER BY c.name LIMIT 10
        """.format(addr=addr_expr), {'kw': '%' + kw + '%', 'zip': kw})

        # 客户匹配不再脱敏，直接返回真实信息（便于客户自行确认身份）
        return _ok({'list': [
            {'id': r['id'],
             'name': r.get('name') or '',
             'customer_no': r.get('customer_no') or '',
             'name_mask': r.get('name') or '',
             'customer_no_mask': r.get('customer_no') or '',
             'zip_code': r.get('zip_code') or '',
             'zip_code': r.get('zip_code') or '',
             # 区域级：如「工业市」「蒙市」，配合邮编供客户自行匹配
             'area': _display_address(r.get('address')),
             'address': _display_address(r.get('address')),
             'address_masked': bool(MASK_ADDRESS)}
            for r in rows
        ]})

    @bp.route('/api/client/bind', methods=['POST'])
    @require_auth
    def client_bind():
        body = request.get_json(silent=True) or {}
        # 需求变更：不再校验手机号；客户按「收货名 + 地址 + 邮编」自行匹配，最终仍走管理端审核
        customer_id = body.get('customer_id')
        if not customer_id:
            return _err('请先搜索并选择客户名')

        customer = _one(db, """
            SELECT id, name, customer_no, barcode, zip_code, is_vip
            FROM customers WHERE id = :cid LIMIT 1
        """, {'cid': customer_id})
        if not customer:
            return _err('客户档案不存在，请重新搜索')

        # 手机号不再作为校验项：防冒绑依赖「地址/邮编人工核对 + 管理端审核」
        # 若后续要恢复二次校验，在此加校验并同步前端表单即可

        # 一个客户档案 MVP 只允许绑定一个微信
        occupied = _one(db, """
            SELECT openid FROM client_users
            WHERE customer_id = :cid AND bind_status = 2 AND openid <> :o LIMIT 1
        """, {'cid': customer['id'], 'o': g.openid})
        if occupied:
            return _err('该客户档案已绑定其他微信，请联系仓库处理')

        # 明确要求：即使不校验手机号，也必须经管理端审核，因此固定为待审核
        status = 1
        _exec(db, """
            UPDATE client_users
            SET customer_id = :cid, bind_status = :s, updated_at = :t
            WHERE openid = :o
        """, {'cid': customer['id'], 's': status, 't': _now(), 'o': g.openid})
        _audit_log('client_bind', 'openid={} customer_id={} status={}'.format(g.openid, customer['id'], status))

        user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': g.openid})
        return _ok({'bind_status': status, 'profile': _profile_of(db, user)},
                   '已绑定' if status == 2 else '已提交，等待仓库审核')

    @bp.route('/api/client/customer/register', methods=['POST'])
    @require_auth
    def client_customer_register():
        """新客户自助建档：填写客户名/邮编/地址（可选单独发货 VIP 详细地址），
        实时同步到系统 customers 表，并立即绑定当前微信（待仓库审核，与搜索绑定同口径）。"""
        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or '').strip()
        zip_code = str(body.get('zip_code') or '').strip()
        address = str(body.get('address') or '').strip()
        phone = str(body.get('phone') or '').strip()
        remark = str(body.get('remark') or '').strip()
        is_vip = bool(body.get('is_vip'))
        invoice_contact = str(body.get('invoice_contact') or '').strip()
        invoice_phone = str(body.get('invoice_phone') or '').strip()
        invoice_address = str(body.get('invoice_address') or '').strip()
        invoice_zip = str(body.get('invoice_zip') or '').strip()
        company_name = str(body.get('company_name') or '').strip()

        if not name:
            return _err('请填写客户名')
        if len(name) > 50:
            return _err('客户名不能超过 50 字')
        if not zip_code:
            return _err('请填写收货邮编')
        if not address:
            return _err('请填写收货地址')
        if len(address) > 200:
            return _err('地址不能超过 200 字')
        if is_vip:
            if not invoice_contact:
                return _err('单独发货请填写收货人姓名')
            if not invoice_address:
                return _err('单独发货请填写详细地址')
            if not invoice_zip:
                return _err('单独发货请填写详细地址邮编')
            # 系统约定：VIP 用详址覆盖主地址/主邮编（详址更权威）
            if not address or len(invoice_address) >= len(address):
                address = invoice_address
            if not zip_code or len(invoice_zip) >= len(zip_code):
                zip_code = invoice_zip
        if _scalar(db, 'SELECT COUNT(*) FROM customers WHERE name = :n', {'n': name}):
            return _err('客户名已存在：' + name)

        cno = _new_customer_no()
        barcode = _new_barcode()
        _exec(db, """
            INSERT INTO customers
              (customer_no, name, barcode, address, zip_code, remark, is_vip, phone,
               invoice_contact, invoice_phone, invoice_address, invoice_zip,
               company_name, shipment_count, pending_freight, has_box, has_bulk, created_at)
            VALUES (:no, :n, :bc, :ad, :zp, :rk, :vip, :ph,
                    :ic, :iph, :iad, :iz, :cn, 0, 0, 0, 0, :t)
        """, {'no': cno, 'n': name, 'bc': barcode, 'ad': address, 'zp': zip_code,
              'rk': remark, 'vip': 1 if is_vip else 0, 'ph': phone,
              'ic': invoice_contact, 'iph': invoice_phone, 'iad': invoice_address,
              'iz': invoice_zip, 'cn': company_name, 't': _now()})
        row = _one(db, 'SELECT id FROM customers WHERE customer_no = :no', {'no': cno})
        cid = int(row['id']) if row else 0
        _audit_log('CLIENT_REGISTER_CUSTOMER',
                   'openid={} customer_id={} no={} vip={}'.format(g.openid, cid, cno, is_vip))

        # 立即绑定当前微信（待仓库审核，与搜索绑定同口径）
        _exec(db, """
            UPDATE client_users SET customer_id = :cid, bind_status = 1, updated_at = :t
            WHERE openid = :o
        """, {'cid': cid, 't': _now(), 'o': g.openid})
        user = _one(db, 'SELECT * FROM client_users WHERE openid = :o LIMIT 1', {'o': g.openid})
        return _ok({'bind_status': 1, 'profile': _profile_of(db, user),
                    'customer_no': cno, 'barcode': barcode, 'is_vip': 1 if is_vip else 0},
                   '已建档并绑定，等待仓库审核')

    @bp.route('/api/client/profile', methods=['GET'])
    @require_auth
    def client_profile():
        return _ok(_profile_of(db, g.user))

    @bp.route('/api/client/avatar', methods=['POST'])
    @require_auth
    def client_avatar_upload():
        """接收微信 chooseAvatar 选中的头像文件，落盘到 avatars/ 并返回可访问 URL。"""
        f = request.files.get('file')
        if not f or not f.filename:
            return _err('缺少头像文件')
        if not (f.mimetype or '').startswith('image/'):
            return _err('头像必须是图片文件')
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > AVATAR_MAX_BYTES:
            return _err('头像不能超过 2MB')
        os.makedirs(AVATAR_DIR, exist_ok=True)
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            ext = '.png'
        # openid 唯一且不含路径字符，叠加时间戳防覆盖；basename 兜底防穿越
        fname = os.path.basename('{}_{}{}'.format(g.openid, int(time.time()), ext))
        f.save(os.path.join(AVATAR_DIR, fname))
        url = '/static/avatars/' + fname
        _exec(db, 'UPDATE client_users SET avatar=:a, updated_at=:t WHERE openid=:o',
              {'a': url, 't': _now(), 'o': g.openid})
        user = _one(db, 'SELECT * FROM client_users WHERE openid=:o LIMIT 1', {'o': g.openid})
        return _ok({'avatar': url, 'profile': _profile_of(db, user)}, '头像已更新')

    @bp.route('/api/client/nickname', methods=['POST'])
    @require_auth
    def client_nickname_save():
        """保存微信昵称（来自微信「头像昵称填写」组件）。"""
        body = request.get_json(silent=True) or {}
        nick = str(body.get('nickname') or '').strip()
        if not nick:
            return _err('昵称不能为空')
        if len(nick) > 30:
            return _err('昵称过长（最多 30 字）')
        _exec(db, 'UPDATE client_users SET nickname=:n, updated_at=:t WHERE openid=:o',
              {'n': nick, 't': _now(), 'o': g.openid})
        user = _one(db, 'SELECT * FROM client_users WHERE openid=:o LIMIT 1', {'o': g.openid})
        return _ok({'nickname': nick, 'profile': _profile_of(db, user)}, '昵称已更新')

    @bp.route('/static/avatars/<path:f>')
    def serve_avatar(f):
        """公开访问已上传头像；basename 防目录穿越。"""
        from flask import send_from_directory
        return send_from_directory(AVATAR_DIR, os.path.basename(f))

    def _shipments_payload(cid, tab, keyword, page, size, as_client=True):
        """客户三态包裹查询（共享实现）。

        客户端 /api/client/shipments 与管理端「按客户查看包裹」共用同一实现，
        保证管理端看到的与客户自己看到的完全一致（代客视角）。
        """
        tab = str(tab or 'pending')
        keyword = str(keyword or '').strip()
        page = max(1, int(page or 1))
        size = min(50, max(1, int(size or 20)))

        # 运费可见性：仅当该包裹所属批次已被管理端「派发账单」后才向客户端展示运费
        # （未派发前客户端看不到任何运费数字；VIP 单独发货无批次也不展示）
        _disp_rows = _rows(db, """
            SELECT DISTINCT pp.batch_id FROM packed_packages pp
            JOIN batch_bill_dispatch d ON d.batch_id = pp.batch_id
            WHERE pp.customer_id = :cid AND pp.batch_id IS NOT NULL AND d.dispatched = 1
        """, {'cid': cid})
        dispatched = set(int(r['batch_id']) for r in _disp_rows if r.get('batch_id') is not None)
        bill_dispatched = len(dispatched) > 0

        # 待发货：已入库（待发）+ 待打包（仓库打包中），后者此前客户完全看不到
        if tab == 'pending':
            where = ["p.customer_id = :cid",
                     "p.status IN ('已入库', '待打包')"]
            params = {'cid': cid}
            if keyword:
                where.append('p.tracking_number LIKE :kw')
                params['kw'] = '%' + keyword + '%'
            clause = ' AND '.join(where)
            total = _scalar(db, 'SELECT COUNT(*) FROM incoming_packages p WHERE ' + clause, params)
            rows = _rows(db, """
                SELECT p.id, p.tracking_number, p.entry_time, p.status,
                       p.customer_remark,
                       CASE WHEN p.status = '待打包' THEN '打包中' ELSE '待发货' END AS stage
                FROM incoming_packages p
                WHERE {clause}
                ORDER BY p.entry_time DESC, p.id DESC
                LIMIT :size OFFSET :offset
            """.format(clause=clause), dict(params, size=size, offset=(page - 1) * size))
            return {'tab': tab, 'list': rows, 'total': int(total), 'page': page,
                    'size': size, 'pending_freight': (_pending_freight(db, cid) if (bill_dispatched or not as_client) else 0.0)}

        if tab not in ('packed', 'shipped'):
            return _err('未知的状态分类')

        cols = _packed_columns(db)

        def field(key):
            col = cols.get(key)
            return ('pp.' + col) if col else 'NULL'

        # 已打包看打包时间，已发货看发货时间
        time_expr = field('created_at' if tab == 'packed' else 'shipped_at')

        params = {'cid': cid}
        link = _item_link(db)
        where = ['pp.customer_id = :cid'] + _packed_tab_where(db, tab, params)
        # VIP 单独发货（按单独地址）已迁移到客户端「单独地址」专属 tab，
        # 常规的 已打包/已发货 列表不再混入这些包裹，避免重复展示
        where.append('(pp.is_vip_solo IS NULL OR pp.is_vip_solo = 0)')
        where.append('(pp.batch_id IS NULL OR b.vip_customer_id IS NULL)')

        if keyword:
            conds = ['pp.serial_label LIKE :kw', 'b.batch_no LIKE :kw']
            # 客户手上往往只有快递单号：支持用内含单号反查属于哪个包裹
            if link['mode'] == 'link_table':
                conds.append(
                    'EXISTS (SELECT 1 FROM {table} ci '
                    'JOIN incoming_packages ip ON ip.id = ci.{child} '
                    'WHERE ci.{parent} = pp.id AND ip.tracking_number LIKE :kw)'.format(
                        table=link['table'], parent=link['parent'], child=link['child']))
            where.append('(' + ' OR '.join(conds) + ')')
            params['kw'] = '%' + keyword + '%'

        clause = ' AND '.join(where)

        total = _scalar(db, """
            SELECT COUNT(*) FROM packed_packages pp
            LEFT JOIN batches b ON b.id = pp.batch_id
            WHERE {clause}
        """.format(clause=clause), params)

        rows = _rows(db, """
            SELECT pp.id, pp.batch_id, pp.serial_label, pp.nature, pp.is_full_box,
                   pp.freight_settled, pp.freight_paid_by, pp.freight_paid_at,
                   b.batch_no,
                   {contents} AS contents_description,
                   {vip} AS is_vip_solo,
                   {dim_length} AS dim_length,
                   {dim_width} AS dim_width,
                   {dim_height} AS dim_height,
                   {weight} AS actual_weight,
                   {volume} AS volume_weight,
                   {item_count} AS item_count,
                   {time_expr} AS time_at
            FROM packed_packages pp
            LEFT JOIN batches b ON b.id = pp.batch_id
            WHERE {clause}
            ORDER BY pp.id DESC
            LIMIT :size OFFSET :offset
        """.format(clause=clause,
                   contents=field('contents'),
                   vip=field('is_vip_solo'),
                   dim_length=field('dim_length'),
                   dim_width=field('dim_width'),
                   dim_height=field('dim_height'),
                   weight=field('weight'),
                   volume=field('volume_weight'),
                   item_count=field('item_count'),
                   time_expr=time_expr),
            dict(params, size=size, offset=(page - 1) * size))

        for r in rows:
            # serial_label 内部含 #id 去重后缀，对外一律去掉
            label = _strip_serial(r.get('serial_label'))
            parts = [str(x) for x in (r.get('batch_no'), label) if x]
            r['package_label'] = ' '.join(parts) if parts else ('包裹 #' + str(r['id']))
            r['serial_display'] = label
            # 脏值不当作属性展示（已入库/已打包 11+979 条）
            r['nature_display'] = _clean_nature(r.get('nature'))
            r['goods_names'] = _split_goods(r.get('contents_description'))
            r['goods_text'] = ' / '.join(r['goods_names'])
            r['has_goods'] = bool(r['goods_names'])
            r['dimension_display'] = _dimension_display(r)
            r['has_dimension'] = bool(r['dimension_display'])
            r['actual_weight'] = _num_or_none(r.get('actual_weight'))
            r['volume_weight'] = _num_or_none(r.get('volume_weight'))
            r['chargeable_weight'] = _chargeable_weight(r)
            # 每个包裹的独立运费 = 计费重 × 单价（普货 25 / 敏货 35），
            # 与 _pending_freight 的合计口径完全一致，卡片级展示用。
            # 仅当该包裹所属批次已被派发账单时才向客户端展示运费（未派发则隐藏）。
            _nat = str(r.get('nature') or '')
            _rate = FREIGHT_RATE_SENSITIVE if ('敏' in _nat or _nat.lower() in ('sensitive', 'ming')) \
                else FREIGHT_RATE_NORMAL
            _visible = (not as_client) or (r.get('batch_id') is not None and int(r['batch_id']) in dispatched)
            r['freight_visible'] = _visible
            r['freight'] = round(r['chargeable_weight'] * _rate, 2) \
                if (_visible and r['chargeable_weight'] is not None) else None
            # 「已付款」标记：freight_settled 即已收款标志；paid_by/at 记录谁何时标记
            r['freight_settled'] = int(r.get('freight_settled') or 0)
            r['freight_paid'] = bool(r['freight_settled'])
            r['freight_paid_by'] = str(r.get('freight_paid_by') or '')
            r['freight_paid_at'] = str(r.get('freight_paid_at') or '')
            if not r.get('batch_no') and r.get('is_vip_solo'):
                r['package_note'] = 'VIP 单独发货'
            else:
                r['package_note'] = ''

        # 内含快递单号：一次批量查完，避免 N+1
        _attach_items(db, rows, tab)

        # 待付运费合计：只累计「已派发账单批次」内、未结算包裹的运费（未派发不展示）
        _visible_pending = 0.0
        for r in rows:
            if r.get('freight_visible') and not r.get('freight_settled'):
                _visible_pending += (r.get('freight') or 0)
        _visible_pending = round(_visible_pending, 2)

        payload = {'tab': tab, 'list': rows, 'total': int(total), 'page': page,
                   'size': size, 'pending_freight': _visible_pending}
        if tab == 'shipped':
            # 运费账单/未结算合计同样受「派发账单后才可见」约束（管理端 as_client=False 不受限）
            payload['bills'] = _freight_bills(db, cid) if (bill_dispatched or not as_client) else []
            payload['unsettled_total'] = _unsettled_total(db, cid) if (bill_dispatched or not as_client) else 0.0
        return payload

    @bp.route('/api/client/shipments', methods=['GET'])
    @require_auth
    def client_shipments():
        """我的货物三态：待发货（incoming）／已打包、已发货（packed_packages，仓库确认后写入）

        字段基准（2026-11-05 源码核对）：
          packed_packages.serial_label          包裹单号，形如 1-1#8，对外显示去掉 #id
          packed_packages.contents_description  品名（包裹级汇总，空白分隔）
          packed_packages.total_packages        包裹内快递件数
          packed_package_items                  包裹 ↔ 快递单号关联
          packed_packages.created_at/shipped_at 打包 / 发货时间
        """
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        return _ok(_shipments_payload(
            cid,
            request.args.get('tab'),
            request.args.get('keyword'),
            request.args.get('page', 1),
            request.args.get('size', 20)))

    @bp.route('/api/client/packages', methods=['GET'])
    @require_auth
    def client_packages():
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)

        where = ['p.customer_id = :cid']
        params = {'cid': cid}
        if request.args.get('status'):
            where.append('p.status = :st')
            params['st'] = request.args['status']
        if request.args.get('batch_id'):
            where.append('p.batch_id = :bid')
            params['bid'] = request.args['batch_id']
        if request.args.get('keyword'):
            where.append('p.tracking_number LIKE :kw')
            params['kw'] = '%' + request.args['keyword'].strip() + '%'
        # 待确认隐藏期：忽略 nature 过滤，否则筛选结果本身会泄露普敏
        if request.args.get('nature') and not HIDE_NATURE:
            where.append('({expr}) = :nature'.format(expr=NATURE_EXPR))
            params['nature'] = request.args['nature']
        clause = ' AND '.join(where)

        # 2 万+ 存量数据：先 COUNT 再分页，索引见 migration 文件（策划案 13.5）
        total = _scalar(db, 'SELECT COUNT(*) FROM incoming_packages p WHERE ' + clause, params)
        page = max(1, int(request.args.get('page', 1)))
        size = min(50, max(1, int(request.args.get('size', 20))))
        params.update({'size': size, 'offset': (page - 1) * size})

        rows = _rows(db, """
            SELECT p.id, p.tracking_number, p.status, p.entry_time,
                   p.batch_id, p.is_danger, p.customer_remark, p.shelf_slot_id,
                   b.batch_no,
                   CASE WHEN p.status IN ('已入库', '待打包') THEN 0 ELSE 1 END AS nature_visible,
                   CASE WHEN p.status IN ('已入库', '待打包') THEN '待确认'
                        ELSE ({expr}) END AS nature_display
            FROM incoming_packages p
            LEFT JOIN batches b ON b.id = p.batch_id
            WHERE {clause}
            ORDER BY p.entry_time DESC, p.id DESC
            LIMIT :size OFFSET :offset
        """.format(expr=NATURE_EXPR, clause=clause, hide=1 if HIDE_NATURE else 0), params)
        return _ok({'list': rows, 'total': int(total), 'page': page, 'size': size})

    @bp.route('/api/client/package/remark', methods=['POST'])
    @require_auth
    def client_package_remark():
        """客户给单个入库包裹加/改备注（按包裹维度）。

        写入 incoming_packages.customer_remark，仓库系统（5100）在
        周确认/入库/打包/发货等页面已支持展示该字段，因此备注自动同步可见。
        仅「已入库/待打包」可编辑，打包后只读，避免覆盖仓库作业信息。
        """
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)

        body = request.get_json(silent=True) or {}
        try:
            pid = int(body.get('package_id') or 0)
        except (TypeError, ValueError):
            return _err('参数格式错误')
        if pid <= 0:
            return _err('缺少包裹 ID')

        # 校验归属 + 可编辑状态：防越权改他人包裹、防改已打包包裹
        pkg = _one(db, """
            SELECT id, status, customer_remark
            FROM incoming_packages
            WHERE id = :pid AND customer_id = :cid
              AND status IN ('已入库', '待打包')
        """, {'pid': pid, 'cid': cid})
        if not pkg:
            return _err('该包裹不存在或已不可编辑', 200, 40004)

        remark = str(body.get('remark') or '')[:200]
        _exec(db, """
            UPDATE incoming_packages SET customer_remark = :rm
            WHERE id = :pid AND customer_id = :cid
        """, {'rm': remark, 'pid': pid, 'cid': cid})
        _try_log(db, 'CLIENT_PACKAGE_REMARK', 'pkg=%d len=%d' % (pid, len(remark)))
        return _ok({'id': pid, 'customer_remark': remark})

    @bp.route('/api/client/batches/pending', methods=['GET'])
    @require_auth
    def client_pending_batches():
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        customer = _one(db, 'SELECT zip_code FROM customers WHERE id = :cid', {'cid': cid}) or {}
        zip_code = customer.get('zip_code') or ''
        batch_rows = _rows(db, """
            SELECT id, batch_no, zip_code, batch_type, is_locked, shipment_status
            FROM batches
            WHERE zip_code = :zip AND is_locked = 0
            ORDER BY id DESC LIMIT 10
        """, {'zip': zip_code})

        # 未打包的包裹没有重量/尺寸，客户端不展示重量进度；
        # 12kg 由仓库打包称重后核验（操作端流程），这里只回传门槛值供文案使用
        # 打包前普敏属性未知：这里只统计件数，不做普/敏拆分
        total_pending = int(_scalar(db, """
            SELECT COUNT(*) FROM incoming_packages p
            WHERE p.customer_id = :cid AND p.status = '已入库'
        """, {'cid': cid}) or 0)

        result = []
        for b in batch_rows:
            normal = 0
            sensitive = 0
            intent = _one(db, """
                SELECT customer_id, batch_id, intent_type, status, updated_at
                FROM shipment_intents
                WHERE customer_id = :cid AND batch_id = :bid AND scope = 1
                LIMIT 1
            """, {'cid': cid, 'bid': b['id']})
            item = {
                'id': b['id'], 'batch_no': b['batch_no'], 'zip_code': b['zip_code'],
                'batch_type': b['batch_type'], 'is_locked': b['is_locked'],
                'shipment_status': b['shipment_status'],
                'package_count': total_pending,
                'counts_hidden': True,
                'nature_known': False,
                'min_weight_kg': DIRECT_MIN_WEIGHT,
                'intent': intent
            }
            # 打包前属性未知，客户端任何情况下都不返回普敏拆分
            result.append(item)
        # 本周批次：该邮编当前唯一未锁定批次（用于页面只读展示）
        week_batch = None
        if batch_rows:
            b0 = batch_rows[0]
            week_batch = {
                'id': b0['id'], 'batch_no': b0['batch_no'],
                'zip_code': b0['zip_code'], 'is_locked': b0['is_locked']
            }
        return _ok({
            'zip_code': zip_code,
            'week_batch': week_batch,
            'pending_count': total_pending,
            'batches': result
        })

    @bp.route('/api/client/intent', methods=['GET'])
    @require_auth
    def client_get_intent():
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        # 本周意向：按客户取最新一条（配送方式切换后 batch_id 可能为 0，不再按批次查）
        row = _one(db, """
            SELECT customer_id, batch_id, delivery_type, cargo_scope, intent_type,
                   address_name, address_phone, address_line, address_zip,
                   remark, status, updated_at
            FROM shipment_intents
            WHERE customer_id = :cid AND scope = 1
            ORDER BY id DESC LIMIT 1
        """, {'cid': cid})
        return _ok({'intent': row})

    @bp.route('/api/client/intent', methods=['POST'])
    @require_auth
    def client_submit_intent():
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        body = request.get_json(silent=True) or {}
        delivery_type = str(body.get('delivery_type') or '')
        cargo_scope = str(body.get('cargo_scope') or 'all')
        address = body.get('address') or {}
        if delivery_type not in DELIVERY_TYPES:
            return _err('请选择发货方式')
        if delivery_type == 'none':
            cargo_scope = 'all'
        if cargo_scope not in CARGO_SCOPES:
            return _err('请选择货物范围')
        if delivery_type == 'direct':
            for field in ('name', 'phone', 'line', 'zip'):
                if not str(address.get(field) or '').strip():
                    return _err('请完整填写收货地址')

        customer = _one(db, 'SELECT zip_code FROM customers WHERE id = :cid', {'cid': cid}) or {}
        zip_code = customer.get('zip_code') or ''

        # 批次自动匹配：
        #   拼邮 → 该邮编当前唯一未锁定批次（系统保证同邮编只有一个未锁定批次）；
        #   单独地址 / 暂不发货 → 无需批次，batch_id 置 0。
        # 绝不回写 incoming_packages.batch_id，单号批次由仓库打包环节处理。
        batch_id = 0
        if delivery_type == 'pooled':
            week_batch = _one(db, """
                SELECT id, batch_no, is_locked FROM batches
                WHERE zip_code = :z AND is_locked = 0
                ORDER BY id DESC LIMIT 1
            """, {'z': zip_code})
            if not week_batch:
                return _err('本周该邮编暂无未锁定批次，无法拼邮发货')
            batch_id = week_batch['id']

        # 兼容旧字段：intent_type 只表达货物范围
        intent_type = 'none' if delivery_type == 'none' else cargo_scope

        # 同客户同周一条（scope=1）：按最新一条更新，避免产生多条
        existing = _one(db, """
            SELECT id, status FROM shipment_intents
            WHERE customer_id = :cid AND scope = 1
            ORDER BY id DESC LIMIT 1
        """, {'cid': cid})
        if existing and int(existing['status']) == 1:
            return _err('仓库已确认该意向，请联系操作员修改')

        params = {
            'cid': cid, 'bid': batch_id, 't': intent_type,
            'dt': delivery_type, 'cs': cargo_scope,
            'an': str(address.get('name') or '').strip(),
            'ap': str(address.get('phone') or '').strip(),
            'al': str(address.get('line') or '').strip(),
            'az': str(address.get('zip') or '').strip(),
            'rm': str(body.get('remark') or '').strip()[:200],
            'now': _now(), 'uid': g.user['id']
        }
        if existing:
            # 只改新增表里的意向字段；绝不触碰 incoming_packages
            _exec(db, """
                UPDATE shipment_intents
                SET batch_id = :bid, delivery_type = :dt, cargo_scope = :cs, intent_type = :t,
                    address_name = :an, address_phone = :ap,
                    address_line = :al, address_zip = :az, remark = :rm,
                    status = 0, updated_at = :now, updated_by = :uid
                WHERE id = :id
            """, dict(params, id=existing['id']))
        else:
            _exec(db, """
                INSERT INTO shipment_intents
                  (customer_id, batch_id, scope, package_id, delivery_type, cargo_scope,
                   intent_type, address_name, address_phone, address_line, address_zip,
                   remark, status, created_at, updated_at, updated_by)
                VALUES (:cid, :bid, 1, NULL, :dt, :cs, :t, :an, :ap, :al, :az,
                        :rm, 0, :now, :now, :uid)
            """, params)
        _audit_log('submit_intent', 'customer={} batch={} delivery={} scope={}'.format(
            cid, batch_id, delivery_type, cargo_scope))

        row = _one(db, """
            SELECT customer_id, batch_id, delivery_type, cargo_scope, intent_type,
                   address_name, address_phone, address_line, address_zip, remark,
                   status, updated_at
            FROM shipment_intents
            WHERE customer_id = :cid AND scope = 1
            ORDER BY id DESC LIMIT 1
        """, {'cid': cid})
        return _ok({'intent': row}, '已提交，等待仓库确认')

    # =======================================================================
    # 管理端 API
    # =======================================================================
    @bp.route('/api/client/confirm-candidates', methods=['GET'])
    @require_auth
    def client_confirm_candidates():
        """本周待确认的货件 = 客户在系统里的「已入库」包裹，附带当前确认记录。

        只返回已入库：待打包/打包中是操作端动作后的状态，不再让客户勾选。
        批次归属由仓库按邮编自动处理，页面对客户不展示批次概念。
        """
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)

        week = _week_label()
        rows = _rows(db, """
            SELECT p.id, p.tracking_number, p.entry_time, p.status
            FROM incoming_packages p
            WHERE p.customer_id = :cid AND p.status = '已入库'
            ORDER BY p.entry_time DESC, p.id DESC
        """, {'cid': cid})

        intent = _get_week_intent(db, cid, week)
        selected = set()
        if intent and intent.get('item_ids'):
            try:
                selected = {int(x) for x in json.loads(intent['item_ids'])}
            except Exception:
                selected = set()

        for r in rows:
            r['selected'] = r['id'] in selected

        return _ok({
            'week_label': week,
            'deadline': _confirm_deadline(),
            'list': rows,
            'total': len(rows),
            'intent': {
                'status': intent.get('intent_status') or '',
                'confirmed_at': intent.get('confirmed_at') or '',
                'item_count': intent.get('item_count') or 0,
                'remark': intent.get('remark') or '',
                'locked': (intent.get('intent_status') == 'locked'),
                'locked_at': intent.get('operator_locked_at') or '',
            } if intent else None,
        })

    def _matched_unlocked_batch(db, zip_code):
        """同邮编当前唯一未锁定批次（业务约束：同邮编仅一个未锁定批次）。"""
        if not zip_code:
            return None
        return _one(db, """
            SELECT id, batch_no FROM batches
            WHERE zip_code = :z AND is_locked = 0
            ORDER BY id DESC LIMIT 1
        """, {'z': str(zip_code)})

    def _get_or_create_vip_batch(db, cid, day):
        """VIP 单独发货批次：取 (客户, 当天建立日) 的未锁定 VIP 批次，没有则新建。

        批次号 V{客户ID}{YYYYMMDD}，冲突尾号递增；zip_code 取单独发货邮编。
        返回批次 id，供 incoming_packages.batch_id 指向——从而自动复用账单/轨迹等全部批次能力。
        """
        row = _one(db, """
            SELECT id FROM batches
            WHERE vip_customer_id = :c AND vip_day = :d AND is_locked = 0
            ORDER BY id DESC LIMIT 1
        """, {'c': cid, 'd': day})
        if row:
            return int(row['id'])
        info = _one(db, 'SELECT customer_no, zip_code, invoice_zip FROM customers WHERE id = :c',
                    {'c': cid}) or {}
        zip_code = info.get('invoice_zip') or info.get('zip_code') or ''
        base = 'V%d%s' % (cid, day.replace('-', ''))
        batch_no = base
        n = 0
        while _one(db, 'SELECT 1 FROM batches WHERE batch_no = :b', {'b': batch_no}):
            n += 1
            batch_no = '%s_%d' % (base, n)
        _exec(db, """
            INSERT INTO batches
              (batch_no, batch_type, zip_code, is_locked, shipment_status,
               vip_customer_id, vip_day)
            VALUES (:b, 'vip', :z, 0, '待打包', :c, :d)
        """, {'b': batch_no, 'z': zip_code, 'c': cid, 'd': day})
        return int(_one(db, 'SELECT id FROM batches WHERE batch_no = :b', {'b': batch_no})['id'])

    def _apply_sync_to_system(db, cid, item_ids, delivery_type):
        """客户确认即同步：确认件 已入库/待打包 -> 待打包，并按配送方式归批。

        - pooled 拼邮：未归批件(batch_id IS NULL)自动归入匹配邮编的未锁定批次
        - direct 单独地址(VIP)：无需批次，batch_id 置 NULL
        - cargo_scope(只发普货/敏货)仅记录意向，不在入库层预过滤
          （打包前普/敏属性未知，由仓库打包时挑件，与既有清单逻辑一致）

        ⚠️ 冲突防护（与仓库手动操作共用同一份库）：
        每条 UPDATE 前重新读取当前状态，若操作员在确认间隙已打包/改状态，
        则跳过该件，绝不把已打包件回退成待打包；direct 仅在操作员尚未另行
        指定批次时才置空，否则保留其批次。返回快照（含本批实际改动了哪些
        字段），供取消时精确回滚。
        """
        if not item_ids:
            return []
        params = {'cid': cid}
        holders = []
        for i, v in enumerate(item_ids):
            k = 'i%d' % i
            params[k] = v
            holders.append(':' + k)
        ph = ', '.join(holders)
        zip_code = (_one(db, 'SELECT zip_code FROM customers WHERE id = :cid',
                        {'cid': cid}) or {}).get('zip_code')
        matched = _matched_unlocked_batch(db, zip_code) if delivery_type == 'pooled' else None
        # VIP 客户单独发货：归并到「当天」VIP 批次（与常规批次一致的账单/轨迹能力）
        is_vip = bool((_one(db, 'SELECT is_vip FROM customers WHERE id = :c',
                           {'c': cid}) or {}).get('is_vip'))
        today = _now_dt().strftime('%Y-%m-%d')
        snapshot = []
        rows = _rows(db, """
            SELECT id, status, batch_id FROM incoming_packages
            WHERE customer_id = :cid AND status IN ('已入库', '待打包')
              AND id IN ({ph})
        """.format(ph=ph), params)
        for r in rows:
            rid = r['id']
            # —— 写前复核：操作员已处理该件则跳过，避免覆盖手动操作 ——
            cur = _one(db, "SELECT status, batch_id FROM incoming_packages WHERE id=:id",
                       {'id': rid})
            if not cur or cur['status'] not in ('已入库', '待打包'):
                continue
            changed = {'status': False, 'batch': False}
            new_batch = None
            if delivery_type == 'pooled':
                if cur['batch_id'] is None and matched:
                    _exec(db, "UPDATE incoming_packages SET status='待打包', batch_id=:b WHERE id=:id",
                          {'b': matched['id'], 'id': rid})
                    changed = {'status': True, 'batch': True}
                    new_batch = matched['id']
                else:
                    _exec(db, "UPDATE incoming_packages SET status='待打包' WHERE id=:id", {'id': rid})
                    changed = {'status': True, 'batch': False}
            elif delivery_type == 'direct':
                if is_vip:
                    # VIP 单独发货：归并入 (客户, 当天) 的 VIP 批次，复用账单/轨迹
                    vb = _get_or_create_vip_batch(db, cid, today)
                    if cur['batch_id'] is None or cur['batch_id'] == r['batch_id']:
                        _exec(db, "UPDATE incoming_packages SET status='待打包', batch_id=:b WHERE id=:id",
                              {'b': vb, 'id': rid})
                        changed = {'status': True, 'batch': True}
                        new_batch = vb
                    else:
                        # 操作员已另行指定批次：保留，不覆盖
                        _exec(db, "UPDATE incoming_packages SET status='待打包' WHERE id=:id", {'id': rid})
                        changed = {'status': True, 'batch': False}
                        new_batch = cur['batch_id']
                else:
                    # 非 VIP 单独地址：无需批次，batch_id 置空（保留操作员已指定的批次）
                    if cur['batch_id'] is None or cur['batch_id'] == r['batch_id']:
                        _exec(db, "UPDATE incoming_packages SET status='待打包', batch_id=NULL WHERE id=:id",
                              {'id': rid})
                        changed = {'status': True, 'batch': True}
                        new_batch = None
                    else:
                        _exec(db, "UPDATE incoming_packages SET status='待打包' WHERE id=:id", {'id': rid})
                        changed = {'status': True, 'batch': False}
                        new_batch = cur['batch_id']
            else:
                _exec(db, "UPDATE incoming_packages SET status='待打包' WHERE id=:id", {'id': rid})
                changed = {'status': True, 'batch': False}
            snapshot.append({
                'id': rid, 'prev_status': r['status'], 'prev_batch': r['batch_id'],
                'set_status': '待打包', 'set_batch': new_batch, 'changed': changed
            })
        return snapshot

    def _rollback_sync_from_system(db, snapshot):
        """取消确认时回滚：仅回滚仍处于 待打包 且本批同步实际改动过的字段。

        若操作员在确认后已进一步处理（打包/改批次），则跳过对应件，
        仅还原我们写入的值，绝不覆盖操作员的手动改动。
        """
        if not snapshot:
            return 0
        n = 0
        for it in snapshot:
            cur = _one(db, "SELECT status, batch_id FROM incoming_packages WHERE id=:id",
                       {'id': it['id']})
            if not cur:
                continue
            # 操作员已把件打包/发货（状态离开待打包）：不回滚，保护手动操作结果
            if cur['status'] != '待打包':
                continue
            changed = it.get('changed') or {}
            sets = []
            params = {'id': it['id']}
            if changed.get('status'):
                sets.append("status=:s")
                params['s'] = it['prev_status']
            if changed.get('batch'):
                # 仅当批次仍是我们当时写入的值时才还原；操作员后续改了则保留
                if cur['batch_id'] == it.get('set_batch'):
                    sets.append("batch_id=:b")
                    params['b'] = it['prev_batch']
            if sets:
                _exec(db, "UPDATE incoming_packages SET " + ", ".join(sets) + " WHERE id=:id", params)
                n += 1
        return n

    def _do_confirm_ship(cid, body, user_id):
        """（共享）提交本周发货确认：客户端提交与管理员代客提交共用同一实现与约束。

        返回 (payload, None) 成功；返回 (None, 错误消息) 失败。
        """
        body = body or {}
        raw = body.get('item_ids') or []
        if not isinstance(raw, list):
            return (None, '参数格式错误')

        ids = []
        for value in raw:
            try:
                num = int(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                ids.append(num)
        ids = sorted(set(ids))

        week = _week_label()
        current = _get_week_intent(db, cid, week)
        if current and current.get('intent_status') == 'locked':
            return (None, '仓库已接单，如需修改请联系仓库')

        # —— 合并入口（原「发货意向」页职责）：发货方式随本次确认一并提交 ——
        # 前端一次请求同时带：item_ids + delivery_type + cargo_scope + address + remark。
        # 发货方式写 shipment_intents(scope=1) 供操作端读取；不带 delivery_type 时
        # 回退沿用已存的 scope=1 意向（兼容旧调用）。
        delivery_type = str(body.get('delivery_type') or '')
        cargo_scope = str(body.get('cargo_scope') or 'all')
        address = body.get('address') or {}
        if delivery_type:
            scope1_row = _one(db, """
                SELECT id, status FROM shipment_intents
                WHERE customer_id = :cid AND scope = 1
                ORDER BY id DESC LIMIT 1
            """, {'cid': cid})
            if scope1_row and int(scope1_row['status']) == 1:
                return (None, '仓库已确认发货方式，请联系操作员修改')
            if delivery_type not in DELIVERY_TYPES:
                return (None, '请选择发货方式')
            if delivery_type == 'none':
                cargo_scope = 'all'
            if cargo_scope not in CARGO_SCOPES:
                return (None, '请选择货物范围')
            if delivery_type == 'direct':
                for field in ('name', 'phone', 'line', 'zip'):
                    if not str(address.get(field) or '').strip():
                        return (None, '请完整填写收货地址')
            if delivery_type == 'none':
                ids = []  # 本周暂不发货 = 全部留仓，忽略已勾选的件
            elif delivery_type == 'pooled':
                zip_code = (_one(db, 'SELECT zip_code FROM customers WHERE id = :cid',
                                 {'cid': cid}) or {}).get('zip_code') or ''
                if not _matched_unlocked_batch(db, zip_code):
                    return (None, '本周该邮编暂无未锁定批次，无法拼邮发货')
            intent_type = 'none' if delivery_type == 'none' else cargo_scope
            iparams = {
                'cid': cid, 't': intent_type,
                'dt': delivery_type, 'cs': cargo_scope,
                'an': str(address.get('name') or '').strip(),
                'ap': str(address.get('phone') or '').strip(),
                'al': str(address.get('line') or '').strip(),
                'az': str(address.get('zip') or '').strip(),
                'rm': str(body.get('remark') or '').strip()[:200],
                'now': _now(), 'uid': user_id
            }
            if scope1_row:
                _exec(db, """
                    UPDATE shipment_intents
                    SET delivery_type = :dt, cargo_scope = :cs, intent_type = :t,
                        address_name = :an, address_phone = :ap,
                        address_line = :al, address_zip = :az, remark = :rm,
                        updated_at = :now, updated_by = :uid
                    WHERE id = :id
                """, dict(iparams, id=scope1_row['id']))
            else:
                _exec(db, """
                    INSERT INTO shipment_intents
                      (customer_id, batch_id, scope, package_id, delivery_type, cargo_scope,
                       intent_type, address_name, address_phone, address_line, address_zip,
                       remark, status, created_at, updated_at, updated_by)
                    VALUES (:cid, 0, 1, NULL, :dt, :cs, :t, :an, :ap, :al, :az,
                            :rm, 0, :now, :now, :uid)
                """, iparams)
            _audit_log('confirm_ship_intent', 'customer={} delivery={} scope={}'.format(
                cid, delivery_type, cargo_scope))

        if ids:
            params = {'cid': cid}
            holders = []
            for i, value in enumerate(ids):
                key = 'i%d' % i
                params[key] = value
                holders.append(':' + key)
            valid = {r['id'] for r in _rows(db, """
                SELECT id FROM incoming_packages
                WHERE customer_id = :cid AND status IN ('已入库', '待打包')
                  AND id IN ({ph})
            """.format(ph=', '.join(holders)), params)}
            if len(valid) != len(ids):
                # 有件已被仓库打包/状态变化，让客户刷新后重选，避免确认到不存在的对象
                return (None, '部分货件状态已变化，请刷新后重试')
            ids = sorted(valid)

        remark = str(body.get('remark') or '')[:200]
        _upsert_week_intent(db, cid, week, ids, remark)
        # 备注与系统客户备注共用同一字段（customers.remark）：
        # 小程序填的备注直接落到客户备注，相当于模拟人在系统「添加客户备注」，
        # 系统打包/发货页以普通客户备注展示，无特殊标记，与操作员手填完全一致。
        if remark.strip():
            _exec(db, "UPDATE customers SET remark = :rm WHERE id = :cid",
                  {'rm': remark.strip(), 'cid': cid})
        intent_row = _get_week_intent(db, cid, week)
        intent_id = intent_row['id'] if intent_row else None
        _try_log(db, 'CLIENT_CONFIRM_SHIP', 'week=%s items=%d' % (week, len(ids)))

        # —— 客户确认即同步到仓库系统（incoming_packages）——
        sync = {'applied': 0, 'delivery_type': None, 'batch': None, 'skipped': None}
        if ids:
            scope1 = _one(db, """
                SELECT delivery_type, cargo_scope FROM shipment_intents
                WHERE customer_id = :cid AND scope = 1
                ORDER BY updated_at DESC LIMIT 1
            """, {'cid': cid})
            d_type = (scope1 or {}).get('delivery_type') or 'pooled'
            c_scope = (scope1 or {}).get('cargo_scope') or 'all'
            if d_type == 'none':
                sync['skipped'] = '本周暂不发货，未同步到仓库'
            else:
                snapshot = _apply_sync_to_system(db, cid, ids, d_type)
                sync['applied'] = len(snapshot)
                sync['delivery_type'] = d_type
                if d_type == 'pooled':
                    zip_code = (_one(db, 'SELECT zip_code FROM customers WHERE id=:c',
                                    {'c': cid}) or {}).get('zip_code')
                    m = _matched_unlocked_batch(db, zip_code)
                    sync['batch'] = (m or {}).get('batch_no')
                if intent_id is not None:
                    _exec(db, "UPDATE shipment_intents SET sync_snapshot=:s WHERE id=:id",
                          {'s': json.dumps(snapshot, ensure_ascii=False), 'id': intent_id})

        message = ('本周已确认 %d 件，已同步仓库 %d 件' % (len(ids), sync['applied'])) \
            if ids else '本周全部留仓'
        return ({'week_label': week, 'item_count': len(ids), 'sync': sync,
                 'message': message}, None)

    @bp.route('/api/client/confirm-ship', methods=['POST'])
    @require_auth
    def client_confirm_ship():
        """提交本周要发的件。空数组 = 本周全部留仓（前端需二次确认）。"""
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        payload, err = _do_confirm_ship(cid, request.get_json(silent=True) or {}, g.user['id'])
        if err:
            return _err(err)
        return _ok(payload)

    @bp.route('/api/client/confirm-cancel', methods=['POST'])
    @require_auth
    def client_confirm_cancel():
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)

        week = _week_label()
        current = _get_week_intent(db, cid, week)
        if not current:
            return _ok({'message': '本周暂无确认记录'})
        if current.get('intent_status') == 'locked':
            return _err('仓库已接单，无法取消')

        # 回滚此前同步到仓库的改动（仅回滚仍停留在 待打包 的件）
        rolled = 0
        if current.get('sync_snapshot'):
            try:
                snap = json.loads(current['sync_snapshot'])
            except Exception:
                snap = []
            rolled = _rollback_sync_from_system(db, snap)
        _exec(db, """
            UPDATE shipment_intents
            SET intent_status = 'cancelled', sync_snapshot = NULL, updated_at = :t
            WHERE id = :id
        """, {'t': _now(), 'id': current['id']})
        _try_log(db, 'CLIENT_CANCEL_SHIP', 'week=%s rolled=%d' % (week, rolled))
        return _ok({'message': '已取消本周确认', 'rolled_back': rolled})

    @bp.route('/api/admin/unlock', methods=['POST'])
    @require_auth
    def admin_unlock():
        """第二重验证：独立管理密码，成功后签发短时效授权 token"""
        if not ADMIN_PIN:
            return _err('管理密码未配置，请联系管理员', 200, 40090)
        # 只校验微信身份，不套 require_admin（否则解锁接口自己也需要已解锁）
        if int(g.user.get('role') or 0) != 1 and g.openid not in ADMIN_OPENIDS:
            return _err('无管理端权限', 200, 40003)
        now = time.time()
        if admin_lock.get(g.openid, 0) > now:
            return _err('尝试次数过多，请 10 分钟后再试', 200, 40091)
        body = request.get_json(silent=True) or {}
        pin = str(body.get('pin') or '')
        if not hmac.compare_digest(pin, ADMIN_PIN):
            recent = [t for t in admin_fails[g.openid] if now - t < 600]
            recent.append(now)
            admin_fails[g.openid] = recent
            if len(recent) >= 5:
                admin_lock[g.openid] = now + 600
            return _err('管理密码不正确', 200, 40092)
        admin_fails.pop(g.openid, None)
        token = _sign_scoped_token(g.openid, 'admin', ADMIN_SESSION_TTL)
        _audit_log('admin_unlock', 'openid={}'.format(g.openid))
        return _ok({'admin_token': token, 'expires_in': ADMIN_SESSION_TTL}, '验证通过')

    @bp.route('/api/admin/batches', methods=['GET'])
    @require_admin
    def admin_batches():
        """批次列表 + 每个批次的具体情况（入库件/包裹/客户/意向确认进度）。

        vip=1：仅返回 VIP 单独发货批次（batches.vip_customer_id IS NOT NULL），
        并关联客户名/号；普通列表则排除 VIP 批次，保持原批次页纯净。
        """
        vip = str(request.args.get('vip') or '').strip() == '1'
        if vip:
            rows = _rows(db, """
                SELECT b.id, b.batch_no, b.zip_code, b.batch_type, b.is_locked,
                       b.shipment_status, b.vip_customer_id, b.vip_day,
                       c.name AS customer_name, c.customer_no AS customer_no
                FROM batches b
                LEFT JOIN customers c ON c.id = b.vip_customer_id
                WHERE b.vip_customer_id IS NOT NULL
                ORDER BY b.vip_day DESC, b.id DESC LIMIT 200
            """)
        else:
            rows = _rows(db, """
                SELECT b.id, b.batch_no, b.zip_code, b.batch_type, b.is_locked,
                       b.shipment_status, b.normal_channel, b.sensitive_channel
                FROM batches b
                WHERE b.vip_customer_id IS NULL
                ORDER BY b.id DESC LIMIT 100
            """)
        # 入库件状态分布（按批次）
        inc = {}
        for r in _rows(db, """
            SELECT batch_id, status, COUNT(*) AS n FROM incoming_packages
            WHERE batch_id IS NOT NULL GROUP BY batch_id, status
        """):
            inc.setdefault(int(r['batch_id']), {})[str(r['status'] or '')] = int(r['n'])
        # 包裹状态分布（按批次）
        pck = {}
        for r in _rows(db, """
            SELECT batch_id, status, COUNT(*) AS n FROM packed_packages
            WHERE batch_id IS NOT NULL GROUP BY batch_id, status
        """):
            pck.setdefault(int(r['batch_id']), {})[str(r['status'] or '')] = int(r['n'])
        # 涉及客户数（入库 + 包裹 去重合并）
        cust = {}
        for r in _rows(db, """
            SELECT batch_id, customer_id FROM incoming_packages WHERE batch_id IS NOT NULL
            UNION
            SELECT batch_id, customer_id FROM packed_packages WHERE batch_id IS NOT NULL
        """):
            bid = r['batch_id']
            if bid is None or r['customer_id'] is None:
                continue
            cust.setdefault(int(bid), set()).add(int(r['customer_id']))
        # 发货方式意向确认进度（scope=1）
        intent = {}
        try:
            for r in _rows(db, """
                SELECT batch_id, COUNT(*) AS n,
                       SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS c
                FROM shipment_intents WHERE scope = 1 AND batch_id IS NOT NULL
                GROUP BY batch_id
            """):
                intent[int(r['batch_id'])] = {'total': int(r['n'] or 0),
                                              'confirmed': int(r['c'] or 0)}
        except Exception:
            pass
        for r in rows:
            bid = int(r['id'])
            i = inc.get(bid, {})
            p = pck.get(bid, {})
            r['incoming_stored'] = i.get('已入库', 0)
            r['incoming_ready'] = i.get('待打包', 0)
            r['packed_packed'] = (p.get('已打包', 0) + p.get('待合箱', 0)
                                  + p.get('已合箱', 0))
            r['packed_shipped'] = p.get('已发货', 0) + p.get('已完成', 0)
            r['packed_total'] = sum(p.values())  # 打包件数（仅供参考）
            # VIP 卡片标题口径：包裹数 = 入库件总数（VIP 单独发货的包裹即入库件，
            # 很多是直接发货、无打包记录，故用入库件而非打包件，避免数字偏小/为 0）
            r['package_total'] = sum(i.values())
            r['customer_count'] = len(cust.get(bid, ()))
            it = intent.get(bid) or {'total': 0, 'confirmed': 0}
            r['intents_total'] = it['total']
            r['intents_confirmed'] = it['confirmed']
            r['is_vip'] = bool(r.get('vip_customer_id'))
        return _ok({'list': rows})

    @bp.route('/api/admin/batch', methods=['POST'])
    @require_admin
    def admin_create_batch():
        body = request.get_json(silent=True) or {}
        batch_no = str(body.get('batch_no') or '').strip()
        zip_code = str(body.get('zip_code') or '').strip()
        # 批次不区分普/敏：batch_type 仅作历史兼容列，不再由前端选择
        if not batch_no or not zip_code:
            return _err('批次号和邮编不能为空')
        dup = _one(db, """
            SELECT batch_no FROM batches WHERE zip_code = :zip AND is_locked = 0 LIMIT 1
        """, {'zip': zip_code})
        if dup:
            return _err('该邮编已有未锁定批次（' + str(dup['batch_no']) + '），请先锁定或发货')

        payload = {
            'no': batch_no, 'zip': zip_code,
            'nc': str(body.get('normal_channel') or ''),
            'sc': str(body.get('sensitive_channel') or '')
        }
        try:
            # 优先不写 batch_type，让列默认值生效
            _exec(db, """
                INSERT INTO batches
                  (batch_no, zip_code, is_locked, shipment_status,
                   normal_channel, sensitive_channel)
                VALUES (:no, :zip, 0, '待打包', :nc, :sc)
            """, payload)
        except Exception:
            # 兼容旧表结构：batch_type 为 NOT NULL 且无默认值时，回退写历史值（不展示、不参与分流）
            db.session.rollback()
            _exec(db, """
                INSERT INTO batches
                  (batch_no, zip_code, batch_type, is_locked, shipment_status,
                   normal_channel, sensitive_channel)
                VALUES (:no, :zip, '普', 0, '待打包', :nc, :sc)
            """, payload)
        _audit_log('create_batch', 'batch_no={} zip={}'.format(batch_no, zip_code))
        return _ok({'batch_no': batch_no}, '批次已建立')

    @bp.route('/api/admin/intents', methods=['GET'])
    @require_admin
    def admin_intents():
        batch_id = request.args.get('batch_id')
        if not batch_id:
            return _err('缺少 batch_id')
        rows = _rows(db, """
            SELECT s.customer_id, s.delivery_type, s.cargo_scope, s.intent_type,
                   s.address_name, s.address_phone, s.address_line, s.address_zip,
                   s.remark, s.status, s.updated_at,
                   c.name AS customer_name, c.customer_no
            FROM shipment_intents s
            LEFT JOIN customers c ON c.id = s.customer_id
            WHERE s.batch_id = :bid AND s.scope = 1
            ORDER BY s.updated_at DESC
        """, {'bid': batch_id})

        # 待决策包裹只统计件数：打包前普敏属性未知，不做拆分
        pools = _rows(db, """
            SELECT customer_id, COUNT(*) AS cnt
            FROM incoming_packages p
            WHERE p.status = '已入库'
            GROUP BY customer_id
        """)
        pool_map = {int(r['customer_id']): int(r['cnt']) for r in pools}

        summary = {'total': len(rows), 'all': 0, 'normal_only': 0,
                   'sensitive_only': 0, 'none': 0, 'confirmed': 0,
                   'pooled': 0, 'direct': 0, 'none_delivery': 0}
        for r in rows:
            summary[r['intent_type']] = summary.get(r['intent_type'], 0) + 1
            delivery = r.get('delivery_type') or ('none' if r['intent_type'] == 'none' else 'pooled')
            key = 'none_delivery' if delivery == 'none' else delivery
            summary[key] = summary.get(key, 0) + 1
            if int(r['status']) == 1:
                summary['confirmed'] += 1
            # 打包前无普敏拆分：nature_known=False，前端显示「打包时判定」
            r['normal_count'] = None
            r['sensitive_count'] = None
            r['nature_known'] = False
            r['package_count'] = pool_map.get(int(r['customer_id']), 0)
        return _ok({'summary': summary, 'list': rows})

    @bp.route('/api/admin/intent/confirm', methods=['POST'])
    @require_admin
    def admin_confirm_intent():
        body = request.get_json(silent=True) or {}
        batch_id = body.get('batch_id')
        customer_ids = body.get('customer_ids') or []
        if not batch_id or not isinstance(customer_ids, list) or not customer_ids:
            return _err('请选择要确认的客户意向')

        marks = []
        params = {'bid': batch_id}
        for i, cid in enumerate(customer_ids):
            key = 'c' + str(i)
            marks.append(':' + key)
            params[key] = int(cid)
        _exec(db, """
            UPDATE shipment_intents SET status = 1, updated_at = :t
            WHERE batch_id = :bid AND scope = 1 AND status = 0
              AND customer_id IN ({marks})
        """.format(marks=','.join(marks)), dict(params, t=_now()))

        # 生成「待打包清单」：只 SELECT，绝不写回 incoming_packages.status
        checklist = []
        for cid in customer_ids:
            intent = _one(db, """
                SELECT delivery_type, cargo_scope, intent_type,
                       address_name, address_phone, address_line, address_zip
                FROM shipment_intents
                WHERE batch_id = :bid AND customer_id = :cid AND scope = 1 LIMIT 1
            """, {'bid': batch_id, 'cid': int(cid)})
            if not intent:
                continue
            itype = intent['intent_type']
            delivery = intent.get('delivery_type') or ('none' if itype == 'none' else 'pooled')
            scope = intent.get('cargo_scope') or ('all' if itype == 'all' else itype)
            base = {
                'customer_id': int(cid),
                'delivery_type': delivery,
                'cargo_scope': scope,
                'intent_type': itype
            }
            if delivery == 'direct':
                base['address'] = {
                    'name': intent.get('address_name') or '',
                    'phone': intent.get('address_phone') or '',
                    'line': intent.get('address_line') or '',
                    'zip': intent.get('address_zip') or ''
                }
            if delivery == 'none':
                base.update({'package_ids': [], 'count': 0})
                checklist.append(base)
                continue
            # 打包前属性未知：清单不做普/敏预过滤，只列全部待发包，
            # 由仓库打包时按客户意愿（scope）与实际属性挑件
            pkgs = _rows(db, """
                SELECT p.id FROM incoming_packages p
                WHERE p.customer_id = :cid AND p.status = '已入库'
            """, {'cid': int(cid)})
            base['nature_pending'] = scope in ('normal_only', 'sensitive_only')
            base.update({'package_ids': [p['id'] for p in pkgs], 'count': len(pkgs)})
            checklist.append(base)

        _audit_log('confirm_intent', 'batch={} customers={}'.format(batch_id, len(customer_ids)))
        return _ok({'confirmed': len(customer_ids), 'checklist': checklist},
                   '已确认，待打包清单已生成（未改动包裹状态）')

    @bp.route('/api/admin/bindings', methods=['GET'])
    @require_admin
    def admin_bindings():
        addr_col = _address_column(db)
        addr_expr = ('c.' + addr_col) if addr_col else "''"
        rows = _rows(db, """
            SELECT u.id, u.openid, u.bind_status, u.created_at, u.updated_at,
                   c.name AS customer_name, c.customer_no, c.phone, c.zip_code,
                   {addr} AS area
            FROM client_users u
            LEFT JOIN customers c ON c.id = u.customer_id
            WHERE u.customer_id IS NOT NULL
            ORDER BY u.bind_status ASC, u.created_at DESC LIMIT 200
        """.format(addr=addr_expr))
        for r in rows:
            phone = str(r.pop('phone', '') or '')
            r['phone_mask'] = ('****' + phone[-4:]) if len(phone) >= 4 else ''
            r['identifier'] = r.get('customer_no') or ''
            # 前端统一按 area 展示，address 保留兼容
            r['address'] = r.get('area') or ''
        pending = sum(1 for r in rows if int(r['bind_status']) == 1)
        return _ok({'list': rows, 'pending_count': pending})

    @bp.route('/api/admin/bind/audit', methods=['POST'])
    @require_admin
    def admin_bind_audit():
        """审核绑定申请；2026-11-06 新增 unbind：解绑已通过的客户。
        解绑必须同时清 customer_id，否则 bind 接口的「一客户一 openid」占用检查不会释放。
        """
        body = request.get_json(silent=True) or {}
        user_id = body.get('user_id')
        action = body.get('action')
        if not user_id or action not in ('approve', 'reject', 'unbind'):
            return _err('审核参数无效')
        if action == 'unbind':
            row = _one(db, 'SELECT id, bind_status FROM client_users WHERE id = :id',
                       {'id': int(user_id)})
            if not row or int(row['bind_status'] or 0) != 2:
                return _err('该记录不是已绑定状态')
            _exec(db, """
                UPDATE client_users SET bind_status = 0, customer_id = NULL,
                                       updated_at = :t WHERE id = :id
            """, {'t': _now(), 'id': int(user_id)})
            _audit_log('bind_unbind', 'user_id={}'.format(user_id))
            return _ok({'bind_status': 0}, '已解绑，客户可重新申请绑定')
        status = 2 if action == 'approve' else 3
        _exec(db, """
            UPDATE client_users SET bind_status = :s, updated_at = :t WHERE id = :id
        """, {'s': status, 't': _now(), 'id': int(user_id)})
        _audit_log('bind_audit', 'user_id={} action={}'.format(user_id, action))
        return _ok({'bind_status': status}, '已通过' if status == 2 else '已驳回')


    @bp.route('/api/admin/shipment_intents', methods=['GET'])
    @require_admin
    def admin_shipment_intents():
        """操作端读客户确认（只读）。带客户当前待发货件数，便于显示「已确认 N / 共 M」。"""
        week = str(request.args.get('week_label') or _week_label())
        if 'week_label' not in _si_columns(db):
            return _ok({'week_label': week, 'list': [], 'ready': False})

        rows = _rows(db, """
            SELECT s.id, s.customer_id, s.week_label, s.item_ids, s.item_count,
                   s.intent_status, s.confirmed_at, s.remark, s.operator_locked_at,
                   c.name AS customer_name, c.customer_no, c.zip_code
            FROM shipment_intents s
            LEFT JOIN customers c ON c.id = s.customer_id
            WHERE s.scope = :sc AND s.week_label = :wk
            ORDER BY s.confirmed_at DESC
        """, {'sc': CONFIRM_SCOPE, 'wk': week})

        # 一次聚合，避免逐客户查
        counts = {r['customer_id']: r['n'] for r in _rows(db, """
            SELECT customer_id, COUNT(*) AS n FROM incoming_packages
            WHERE status IN ('已入库', '待打包') GROUP BY customer_id
        """)}

        for r in rows:
            r['pending_count'] = counts.get(r['customer_id'], 0)
            r['item_id_list'] = []
            if r.get('item_ids'):
                try:
                    r['item_id_list'] = [int(x) for x in json.loads(r['item_ids'])]
                except Exception:
                    r['item_id_list'] = []
            r['locked'] = (r.get('intent_status') == 'locked')
            r['is_all'] = (r['item_count'] or 0) >= r['pending_count'] > 0

        return _ok({'week_label': week, 'list': rows, 'ready': True})

    @bp.route('/api/admin/intent/lock', methods=['POST'])
    @require_admin
    def admin_intent_lock():
        """操作员点「确认待打包」时调用：锁定该客户本周确认，客户此后只读。"""
        body = request.get_json(silent=True) or {}
        cids = body.get('customer_ids') or []
        week = str(body.get('week_label') or _week_label())
        if not isinstance(cids, list) or not cids:
            return _err('请指定客户')

        if 'week_label' not in _si_columns(db):
            return _ok({'locked': 0, 'message': '确认表未升级，跳过'})

        stamp = _now()
        locked = 0
        for raw in cids:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            existing = _get_week_intent(db, cid, week)
            if not existing:
                continue
            _exec(db, """
                UPDATE shipment_intents
                SET intent_status = 'locked', operator_locked_at = :t, updated_at = :t
                WHERE id = :id
            """, {'t': stamp, 'id': existing['id']})
            locked += 1
        return _ok({'locked': locked})

    # ========== 管理端：客户总览 / 按客户查看包裹 / 代客操作 ==========
    BIND_LABELS = {2: '已绑定', 1: '待审核', 3: '已驳回'}

    @bp.route('/api/admin/customers', methods=['GET'])
    @require_admin
    def admin_customers():
        """客户总览：默认只显示「有记录」客户（系统里有过入库/打包记录）。
        pool=1 时改为「意向池」：从始至终没有任何入库或发货记录的客户。
        判定为动态 SQL，一旦有货入库自动离开意向池，不需要额外标记。
        """
        kw = str(request.args.get('keyword') or '').strip()
        page = max(1, int(request.args.get('page', 1)))
        size = min(200, max(1, int(request.args.get('size', 100))))
        pool = str(request.args.get('pool') or '') == '1'
        params = {}
        # 列表口径：有记录 / 意向池（无记录）
        where = NO_RECORD_SQL if pool else HAS_RECORD_SQL
        # VIP 单独发货记录：展示全部 VIP 客户（不限制是否有包裹记录，地址信息本身即记录）
        is_vip = str(request.args.get('is_vip') or '').strip()
        if is_vip == '1':
            where = '1=1'
        def _and(cond):
            return '(' + where + ') AND ' + cond
        if kw:
            where = _and('(c.name LIKE :kw OR c.customer_no LIKE :kw OR c.zip_code LIKE :kw)')
            params['kw'] = '%' + kw + '%'
        # 按批次筛选：仅返回在该批次有入库件/包裹的客户（实时 JOIN，不走缓存）
        batch_id = request.args.get('batch_id')
        try:
            batch_id = int(batch_id) if batch_id else 0
        except (TypeError, ValueError):
            batch_id = 0
        if batch_id:
            sub = ('(c.id IN (SELECT customer_id FROM incoming_packages '
                   'WHERE batch_id = :bid UNION SELECT customer_id FROM packed_packages '
                   'WHERE batch_id = :bid))')
            where = sub if where == '1=1' else '(' + where + ') AND ' + sub
            params['bid'] = batch_id
        # 按邮编筛选：仅返回该邮编下所有批次中有包裹的客户
        zip_code = str(request.args.get('zip_code') or '').strip()
        if zip_code:
            sub = ('(c.id IN (SELECT customer_id FROM incoming_packages ip '
                   'JOIN batches b ON b.id = ip.batch_id WHERE b.zip_code = :zp '
                   'UNION SELECT customer_id FROM packed_packages pp '
                   'JOIN batches b2 ON b2.id = pp.batch_id WHERE b2.zip_code = :zp))')
            where = sub if where == '1=1' else '(' + where + ') AND ' + sub
            params['zp'] = zip_code
        # 按 VIP 单独发货筛选：仅返回勾选「单独发货」的客户（is_vip 已在上方置 1=1 基础）
        if is_vip == '1':
            where = where + ' AND c.is_vip = 1'
        total = _scalar(db, 'SELECT COUNT(*) FROM customers c WHERE ' + where, params)
        rows = _rows(db, """
            SELECT c.id, c.name, c.customer_no, c.zip_code, c.phone,
                   c.is_vip, c.invoice_contact, c.invoice_phone,
                   c.invoice_address, c.invoice_zip,
                   (SELECT COUNT(*) FROM incoming_packages p
                     WHERE p.customer_id = c.id
                       AND p.status IN ('已入库', '待打包')) AS pending_count,
                   (SELECT COUNT(*) FROM packed_packages pp
                     WHERE pp.customer_id = c.id
                       AND pp.status IN ('已打包', '待合箱', '已合箱')) AS packed_count,
                   (SELECT COUNT(*) FROM packed_packages pp
                     WHERE pp.customer_id = c.id
                       AND pp.status IN ('已发货', '已完成')) AS shipped_count
            FROM customers c
            WHERE {w}
            ORDER BY c.id DESC
            LIMIT :size OFFSET :offset
        """.format(w=where), dict(params, size=size, offset=(page - 1) * size))
        # 绑定状态：每客户取最新一条记录
        bind = {}
        try:
            for u in _rows(db, """
                SELECT customer_id, bind_status FROM client_users
                WHERE customer_id IS NOT NULL
                ORDER BY updated_at DESC, id DESC
            """):
                cu = int(u['customer_id'])
                if cu not in bind:
                    bind[cu] = int(u['bind_status'] or 0)
        except Exception:
            pass
        freight = _freight_map(db)
        week = _week_label()
        intents = {}
        try:
            for s in _rows(db, """
                SELECT customer_id, intent_status FROM shipment_intents
                WHERE scope = :sc AND week_label = :wk
            """, {'sc': CONFIRM_SCOPE, 'wk': week}):
                intents[int(s['customer_id'])] = str(s['intent_status'] or '')
        except Exception:
            pass
        for r in rows:
            cid = int(r['id'])
            r['is_vip'] = int(r.get('is_vip') or 0)
            bs = bind.get(cid)
            r['bind_status'] = bs
            r['bind_label'] = BIND_LABELS.get(bs, '未绑定')
            r['bound'] = (bs == 2)
            r['pending_freight'] = freight.get(cid, 0.0)
            ist = intents.get(cid, '')
            r['week_intent'] = {'status': ist,
                                'confirmed': ist in ('active', 'locked'),
                                'locked': ist == 'locked'}
            phone = str(r.pop('phone', '') or '')
            r['phone_mask'] = ('****' + phone[-4:]) if len(phone) >= 4 else ''
        # 汇总口径：全部客户（不受分页影响）；pool_count 为全库意向池人数
        n_bound = sum(1 for v in bind.values() if v == 2)
        n_pending = sum(1 for v in bind.values() if v == 1)
        n_rejected = sum(1 for v in bind.values() if v == 3)
        n_linked = len(bind)
        active_total = _scalar(db, 'SELECT COUNT(*) FROM customers c WHERE ' + HAS_RECORD_SQL) or 0
        pool_total = _scalar(db, 'SELECT COUNT(*) FROM customers c WHERE ' + NO_RECORD_SQL) or 0
        summary = {
            'total': int(total or 0),
            'active_total': int(active_total),
            'pool_count': int(pool_total),
            'bound': n_bound,
            'pending_audit': n_pending,
            'rejected': n_rejected,
            'unbound': max(0, int(total or 0) - n_linked),
        }
        resp = {'week_label': week, 'summary': summary, 'list': rows,
                'page': page, 'size': size, 'total': int(total or 0),
                'pool': pool,
                'has_more': page * size < int(total or 0)}
        if batch_id:
            binfo = _one(db, 'SELECT batch_no, zip_code FROM batches WHERE id = :bid',
                         {'bid': batch_id})
            resp['batch'] = {'id': batch_id,
                             'batch_no': (binfo['batch_no'] if binfo else ''),
                             'zip_code': (binfo['zip_code'] if binfo else '')}
        if zip_code:
            resp['zip'] = zip_code
        return _ok(resp)

    @bp.route('/api/admin/customer/<int:cid>/packages', methods=['GET'])
    @require_admin
    def admin_customer_packages(cid):
        """管理端按客户查看包裹：与客户端完全同一实现（代客视角，三态 + 运费）。"""
        info = _one(db, """
            SELECT id, name, customer_no, zip_code FROM customers WHERE id = :cid
        """, {'cid': cid})
        if not info:
            return _err('客户不存在')
        payload = _shipments_payload(cid,
                                     request.args.get('tab'),
                                     request.args.get('keyword'),
                                     request.args.get('page', 1),
                                     request.args.get('size', 20),
                                     as_client=False)
        payload['customer'] = info
        return _ok(payload)

    @bp.route('/api/admin/customer/<int:cid>/confirm-ship', methods=['POST'])
    @require_admin
    def admin_customer_confirm_ship(cid):
        """管理员代客户提交本周发货确认（与客户端同一实现与全部约束）。"""
        info = _one(db, "SELECT id FROM customers WHERE id = :cid", {'cid': cid})
        if not info:
            return _err('客户不存在')
        payload, err = _do_confirm_ship(cid, request.get_json(silent=True) or {},
                                        g.user['id'])
        if err:
            return _err(err)
        _audit_log('admin_confirm_ship', 'customer={} admin={}'.format(cid, g.openid))
        return _ok(payload)

    @bp.route('/api/admin/customer/<int:cid>/confirm-cancel', methods=['POST'])
    @require_admin
    def admin_customer_confirm_cancel(cid):
        """管理员代客户取消本周确认（回滚同步，约束与客户端取消一致）。"""
        week = str((request.get_json(silent=True) or {}).get('week_label')
                   or _week_label())
        current = _get_week_intent(db, cid, week)
        if not current:
            return _ok({'message': '该客户本周暂无确认记录'})
        if current.get('intent_status') == 'locked':
            return _err('仓库已接单，无法取消')
        rolled = 0
        if current.get('sync_snapshot'):
            try:
                snap = json.loads(current['sync_snapshot'])
            except Exception:
                snap = []
            rolled = _rollback_sync_from_system(db, snap)
        _exec(db, """
            UPDATE shipment_intents
            SET intent_status = 'cancelled', sync_snapshot = NULL, updated_at = :t
            WHERE id = :id
        """, {'t': _now(), 'id': current['id']})
        _audit_log('admin_cancel_ship', 'customer={} rolled={}'.format(cid, rolled))
        return _ok({'message': '已取消该客户本周确认', 'rolled_back': rolled})

    # =======================================================================
    # 管理端 · 操作类接口（收款 / 锁定 / 异常 / 优惠券 / 运费明细）
    # 约束：仅新增标志位(is_locked / is_abnormal / freight_settled) 与 coupons 表，
    #       不改 incoming_packages.status、packed_packages.status 等核心流转字段。
    # =======================================================================

    @bp.route('/api/admin/batch/lock', methods=['POST'])
    @require_admin
    def admin_batch_lock():
        """2026-11-06：批次锁定/解锁只能在主系统操作端完成，小程序端一律拒绝。
        （保留路由是为了让旧版编译包得到明确提示，而不是 404。）
        """
        return _err('批次锁定/解锁只能在系统操作端完成', 200, 40003)

    @bp.route('/api/admin/customer/<int:cid>/settle-freight', methods=['POST'])
    @require_admin
    def admin_settle_freight(cid):
        """一键标记该客户全部待付运费已收：未结算包裹置 freight_settled=1（已付过的不动）。"""
        info = _one(db, 'SELECT id FROM customers WHERE id = :cid', {'cid': cid})
        if not info:
            return _err('客户不存在')
        before = _pending_freight(db, cid)
        if before <= 0:
            return _err('该客户当前没有待付运费')
        now = _now()
        _exec(db,
              "UPDATE packed_packages SET freight_settled = 1, "
              "freight_paid_by = 'admin', freight_paid_at = :t "
              "WHERE customer_id = :cid AND status IN (%s) "
              "AND (freight_settled IS NULL OR freight_settled = 0)" % _FREIGHT_STATUS_IN,
              {'cid': cid, 't': now})
        _exec(db, 'UPDATE customers SET pending_freight = 0 WHERE id = :cid', {'cid': cid})
        n = _scalar(db,
                    "SELECT COUNT(*) FROM packed_packages WHERE customer_id = :cid "
                    "AND freight_settled = 1 AND freight_paid_at = :t" % '',
                    {'cid': cid, 't': now}) or 0
        _audit_log('admin_settle_freight',
                   'customer={} amount={:.2f} packages={}'.format(cid, before, n))
        return _ok({'settled_amount': round(before, 2), 'settled_count': int(n),
                    'remaining': _pending_freight(db, cid)})

    def _batch_dim_status(db, bid):
        """批次尺寸推送/确认状态（管理端用）：pushed/pushed_at/all_confirmed/confirms[]。"""
        push = _one(db, 'SELECT pushed, pushed_at FROM batch_dim_push WHERE batch_id = :b', {'b': bid})
        pushed = bool(push and push['pushed'])
        pushed_at = (push['pushed_at'] if push else '') or ''
        custs = _rows(db, """
            SELECT DISTINCT pp.customer_id AS cid, c.name AS name
            FROM packed_packages pp
            LEFT JOIN customers c ON c.id = pp.customer_id
            WHERE pp.batch_id = :b AND pp.customer_id IS NOT NULL AND pp.status IN (%s)
        """ % _FREIGHT_STATUS_IN, {'b': bid})
        confirms = _rows(db,
            'SELECT customer_id, confirmed, confirmed_at FROM batch_dim_confirm WHERE batch_id = :b',
            {'b': bid})
        cmap = {int(x['customer_id']): x for x in confirms}
        out = []
        for c in custs:
            cid = int(c['cid'])
            cf = cmap.get(cid)
            out.append({
                'customer_id': cid,
                'name': str(c['name'] or ('客户' + str(cid))),
                'confirmed': bool(cf and cf['confirmed']),
                'confirmed_at': (cf['confirmed_at'] if cf else '') or ''
            })
        out.sort(key=lambda x: (not x['confirmed'], x['name']))
        all_confirmed = pushed and len(out) > 0 and all(x['confirmed'] for x in out)
        return {
            'pushed': pushed,
            'pushed_at': pushed_at,
            'all_confirmed': all_confirmed,
            'confirms': out,
            'total': len(out),
            'confirmed_count': sum(1 for x in out if x['confirmed'])
        }

    @bp.route('/api/admin/batch/<int:bid>/bill', methods=['GET'])
    @require_admin
    def admin_batch_bill(bid):
        """批次账单：该批次所含客户逐个罗列，普货/敏货分组，含待付运费。

        口径与 _pending_freight 一致：仅统计产生运费的 packed 包裹
        （_FREIGHT_PACKED_STATUSES）中「未结算」的部分。
        运费 = 计费重 ×（普 25 / 敏 35）；计费重默认取系统逐包裹计算值之和，
        但若管理端对该 客户×普/敏 组设置了「手动计费重覆盖」，则取覆盖值（可编辑）。
        体积重保留 3 位小数；返回批次是否已「派发」给客户端。
        """
        b = _one(db, 'SELECT id, batch_no FROM batches WHERE id = :b', {'b': bid})
        if not b:
            return _err('批次不存在')
        def _dim_num(v):
            """尺寸数字格式化：整数去小数点，其余去尾零（32.0→32, 11.5→11.5）"""
            try:
                f = float(v)
            except (TypeError, ValueError):
                return '?'
            return str(int(f)) if f == int(f) else ('%.2f' % f).rstrip('0').rstrip('.')

        rows = _rows(db, """
            SELECT pp.id, pp.customer_id, c.name, pp.nature, pp.weight, pp.volume_weight,
                   pp.freight_settled, pp.length, pp.width, pp.height
            FROM packed_packages pp
            LEFT JOIN customers c ON c.id = pp.customer_id
            WHERE pp.batch_id = :b AND pp.customer_id IS NOT NULL
              AND pp.status IN (%s)
        """ % _FREIGHT_STATUS_IN, {'b': bid})
        agg = {}
        for r in rows:
            cw = _chargeable_weight(r)
            if cw is None:
                continue
            grp = 'sensitive' if _is_sensitive(r.get('nature')) else 'normal'
            key = int(r['customer_id'])
            settled = int(r.get('freight_settled') or 0)
            it = agg.setdefault(key, {
                'customer_id': key,
                'name': str(r.get('name') or ('客户' + str(key))),
                'normal': {'count': 0, 'settled_count': 0, 'weight': 0.0, 'volume_weight': 0.0,
                           'computed': 0.0, 'settled_cw': 0.0, 'dims': []},
                'sensitive': {'count': 0, 'settled_count': 0, 'weight': 0.0, 'volume_weight': 0.0,
                              'computed': 0.0, 'settled_cw': 0.0, 'dims': []},
            })
            it[grp]['count'] += 1
            if settled:
                it[grp]['settled_count'] += 1
                it[grp]['settled_cw'] += cw
            it[grp]['computed'] += cw
            if r.get('length') is not None and r.get('width') is not None and r.get('height') is not None:
                it[grp]['dims'].append('%s*%s*%s' % (
                    _dim_num(r['length']), _dim_num(r['width']), _dim_num(r['height'])))
            w = _num_or_none(r.get('weight'))
            vw = _num_or_none(r.get('volume_weight'))
            if w is not None:
                it[grp]['weight'] += w
            if vw is not None:
                it[grp]['volume_weight'] += vw
        # 手动计费重覆盖（一次取本批次全部，避免逐客户查）
        ovs = _rows(db,
            "SELECT customer_id, nature, chargeable_weight FROM batch_freight_override "
            "WHERE batch_id = :b", {'b': bid})
        ovmap = {}
        for o in ovs:
            ovmap[(int(o['customer_id']), str(o['nature']))] = o['chargeable_weight']
        items = []
        for it in agg.values():
            for grp in ('normal', 'sensitive'):
                g = it[grp]
                rate = FREIGHT_RATE_SENSITIVE if grp == 'sensitive' else FREIGHT_RATE_NORMAL
                overridden = ovmap.get((it['customer_id'], grp)) is not None
                effective = float(ovmap[(it['customer_id'], grp)]) if overridden \
                    else round(g['computed'], 1)
                g['weight'] = round(g['weight'], 1)
                g['volume_weight'] = round(g['volume_weight'], 3)   # 体积重保留 3 位小数
                g['computed'] = round(g['computed'], 1)
                g['chargeable'] = round(effective, 3)               # 可编辑的计费重（组级总额）
                g['rate'] = rate
                freight_total = round(effective * rate, 2)         # 该组运费总额（已付+待付）
                paid_frac = (g['settled_cw'] / g['computed']) if g['computed'] > 0 else 0.0
                g['freight'] = freight_total
                g['paid_freight'] = round(freight_total * paid_frac, 2)
                g['pending_freight'] = round(freight_total - g['paid_freight'], 2)
                g['overridden'] = overridden
            it['pending_total'] = round(it['normal']['pending_freight'] + it['sensitive']['pending_freight'], 2)
            it['paid_total'] = round(it['normal']['paid_freight'] + it['sensitive']['paid_freight'], 2)
            it['all_settled'] = it['pending_total'] <= 0
            items.append(it)
        items.sort(key=lambda x: -x['pending_total'])
        tot = {
            'normal_pending': round(sum(i['normal']['pending_freight'] for i in items), 2),
            'sensitive_pending': round(sum(i['sensitive']['pending_freight'] for i in items), 2),
        }
        tot['paid_total'] = round(sum(i['paid_total'] for i in items), 2)
        tot['pending_total'] = round(tot['normal_pending'] + tot['sensitive_pending'], 2)
        disp = _one(db, 'SELECT dispatched, dispatched_at FROM batch_bill_dispatch WHERE batch_id = :b',
                    {'b': bid})
        dim = _batch_dim_status(db, bid)
        return _ok({'batch_id': bid, 'batch_no': str(b['batch_no'] or ''),
                    'items': items, 'totals': tot,
                    'dispatched': bool(disp and disp['dispatched']),
                    'dispatched_at': (disp['dispatched_at'] if disp else ''),
                    'dim': dim})

    @bp.route('/api/admin/batch/<int:bid>/freight-override', methods=['POST'])
    @require_admin
    def admin_set_freight_override(bid):
        """设置/清除某客户在某批次、指定普/敏组的「手动计费重覆盖」。

        请求体：{ customer_id, nature('normal'|'sensitive'), chargeable_weight }
          - chargeable_weight 为数字 → 覆盖系统计算值（运费随之变化）
          - chargeable_weight 为 null / 空字符串 / 省略 → 删除覆盖，恢复系统计算值
        """
        body = request.get_json(silent=True) or {}
        cid = int(body.get('customer_id') or 0)
        nature = str(body.get('nature') or '').strip().lower()
        if nature not in ('normal', 'sensitive'):
            return _err('nature 必须是 normal 或 sensitive')
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        if not _one(db, 'SELECT id FROM customers WHERE id = :c', {'c': cid}):
            return _err('客户不存在')
        raw = body.get('chargeable_weight', '__missing__')
        if raw is None or raw == '' or raw == '__missing__':
            _exec(db,
                  "DELETE FROM batch_freight_override WHERE batch_id = :b AND customer_id = :c AND nature = :n",
                  {'b': bid, 'c': cid, 'n': nature})
            effective = _group_computed_chargeable(db, bid, cid, nature)
            overridden = False
        else:
            try:
                cwf = float(raw)
            except (TypeError, ValueError):
                return _err('计费重必须是数字')
            if cwf < 0:
                return _err('计费重不能为负')
            cwf = round(cwf, 3)
            _exec(db,
                  """INSERT INTO batch_freight_override(batch_id, customer_id, nature, chargeable_weight, updated_at)
                     VALUES (:b, :c, :n, :w, :t)
                     ON CONFLICT(batch_id, customer_id, nature) DO UPDATE SET chargeable_weight = :w, updated_at = :t""",
                  {'b': bid, 'c': cid, 'n': nature, 'w': cwf, 't': _now()})
            effective = cwf
            overridden = True
        rate = FREIGHT_RATE_SENSITIVE if nature == 'sensitive' else FREIGHT_RATE_NORMAL
        return _ok({'customer_id': cid, 'nature': nature,
                    'chargeable': round(effective, 3), 'freight': round(effective * rate, 2),
                    'rate': rate, 'overridden': overridden})

    @bp.route('/api/admin/batch/<int:bid>/dispatch-bill', methods=['POST'])
    @require_admin
    def admin_dispatch_batch_bill(bid):
        """派发批次账单给客户端：只有已派发的批次，客户端才能在「我的账单」看到。"""
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        # 尺寸信息确认门控：已推送尺寸信息但尚有客户未确认时，禁止派发账单
        dim = _batch_dim_status(db, bid)
        if dim['pushed'] and not dim['all_confirmed']:
            return _err('尚有客户未确认尺寸信息，无法派发账单（请在「尺寸信息」中确认全部客户）')
        now = _now()
        _exec(db,
              """INSERT INTO batch_bill_dispatch(batch_id, dispatched, dispatched_at)
                 VALUES (:b, 1, :t)
                 ON CONFLICT(batch_id) DO UPDATE SET dispatched = 1, dispatched_at = :t""",
              {'b': bid, 't': now})
        _audit_log('admin_dispatch_batch_bill', 'batch={}'.format(bid))
        return _ok({'dispatched': True, 'dispatched_at': now})

    @bp.route('/api/admin/batch/<int:bid>/recall-bill', methods=['POST'])
    @require_admin
    def admin_recall_batch_bill(bid):
        """撤回已派发的批次账单：客户端立即看不到本批次账单与运费（可再次派发）。"""
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        _exec(db,
              """INSERT INTO batch_bill_dispatch(batch_id, dispatched, dispatched_at)
                 VALUES (:b, 0, NULL)
                 ON CONFLICT(batch_id) DO UPDATE SET dispatched = 0, dispatched_at = NULL""",
              {'b': bid})
        _audit_log('admin_recall_batch_bill', 'batch={}'.format(bid))
        return _ok({'dispatched': False})

    @bp.route('/api/admin/batch/<int:bid>/push-dims', methods=['POST'])
    @require_admin
    def admin_push_batch_dims(bid):
        """管理端推送批次尺寸信息给客户：客户在小程序逐一确认无误后，才允许派发账单。"""
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        now = _now()
        _exec(db,
              """INSERT INTO batch_dim_push(batch_id, pushed, pushed_at)
                 VALUES (:b, 1, :t)
                 ON CONFLICT(batch_id) DO UPDATE SET pushed = 1, pushed_at = :t""",
              {'b': bid, 't': now})
        # 为批次内每个有已打包包裹的客户建确认行（已存在则保留原确认状态）
        custs = _rows(db, """
            SELECT DISTINCT pp.customer_id AS cid
            FROM packed_packages pp
            WHERE pp.batch_id = :b AND pp.customer_id IS NOT NULL AND pp.status IN (%s)
        """ % _FREIGHT_STATUS_IN, {'b': bid})
        for c in custs:
            _exec(db,
                  """INSERT INTO batch_dim_confirm(batch_id, customer_id, confirmed, confirmed_at)
                     VALUES (:b, :c, 0, NULL)
                     ON CONFLICT(batch_id, customer_id) DO NOTHING""",
                  {'b': bid, 'c': int(c['cid'])})
        _audit_log('admin_push_batch_dims', 'batch={}'.format(bid))
        return _ok(_batch_dim_status(db, bid))

    @bp.route('/api/admin/batch/<int:bid>/recall-dims', methods=['POST'])
    @require_admin
    def admin_recall_batch_dims(bid):
        """撤回尺寸信息推送：客户端立即看不到；已确认状态保留，可再次推送。"""
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        _exec(db,
              """INSERT INTO batch_dim_push(batch_id, pushed, pushed_at)
                 VALUES (:b, 0, NULL)
                 ON CONFLICT(batch_id) DO UPDATE SET pushed = 0, pushed_at = NULL""",
              {'b': bid})
        _audit_log('admin_recall_batch_dims', 'batch={}'.format(bid))
        return _ok(_batch_dim_status(db, bid))

    @bp.route('/api/client/vip-batches', methods=['GET'])
    @require_auth
    def client_vip_batches():
        """客户端：VIP 单独发货批次列表（与管理端 VIP 记录口径一致）。
        - 普通 VIP 客户：仅返回本人(vip_customer_id=cid)的批次。
        - 管理员带 all=1：返回全部 VIP 批次（操作员在客户端预览用）。
        命名格式由前端统一为「客户名 + 短日期(YY-MM-DD) + 箱数」。
        """
        cid = require_bound_customer()
        if not cid:
            return _err('尚未完成客户绑定', 200, 40002)
        is_admin = g.openid in ADMIN_OPENIDS
        want_all = str(request.args.get('all') or '') == '1'
        if is_admin and want_all:
            where = 'b.vip_customer_id IS NOT NULL'
            params = {}
        else:
            where = 'b.vip_customer_id = :cid'
            params = {'cid': cid}
        rows = _rows(db, """
            SELECT b.id, b.batch_no, b.vip_day, b.is_locked, b.shipment_status,
                   c.name AS customer_name, c.customer_no AS customer_no
            FROM batches b
            LEFT JOIN customers c ON c.id = b.vip_customer_id
            WHERE {scope}
            ORDER BY b.vip_day DESC, b.id DESC
        """.replace('{scope}', where), params)
        # 入库件总数（包裹数口径，与管理端 VIP 批次标题一致）
        inc = {}
        for r in _rows(db, """
            SELECT batch_id, COUNT(*) AS n FROM incoming_packages
            WHERE batch_id IN (SELECT id FROM batches b WHERE {scope})
            GROUP BY batch_id
        """.replace('{scope}', where), params):
            inc[int(r['batch_id'])] = int(r['n'])
        # 打包件状态分布（已打包 / 已发货 / 已完成）
        pck = {}
        for r in _rows(db, """
            SELECT batch_id, status, COUNT(*) AS n FROM packed_packages
            WHERE batch_id IN (SELECT id FROM batches b WHERE {scope})
            GROUP BY batch_id, status
        """.replace('{scope}', where), params):
            pck.setdefault(int(r['batch_id']), {})[str(r['status'] or '')] = int(r['n'])
        for r in rows:
            bid = int(r['id'])
            p = pck.get(bid, {})
            r['package_total'] = inc.get(bid, 0)            # 包裹数（入库件）
            r['packed_total'] = sum(p.values())
            r['packed_packed'] = (p.get('已打包', 0) + p.get('待合箱', 0) + p.get('已合箱', 0))
            r['packed_shipped'] = p.get('已发货', 0) + p.get('已完成', 0)
            r['is_vip'] = True
        return _ok({'list': rows, 'is_admin_all': bool(is_admin and want_all)})

    @bp.route('/api/client/batch/<int:bid>/bill', methods=['GET'])
    @require_auth
    def client_batch_bill(bid):
        """客户端查看已派发的批次账单（仅本人、仅已派发可见）。

        与派发时管理端账单口径一致：计费重取手动覆盖值否则系统值，运费 = 计费重 × 单价。
        已结算的包裹计入已付金额；未结算计入待付。
        """
        cid = require_bound_customer()
        if cid is None:
            return _err('请先绑定客户', 401, 40012)
        disp = _one(db, 'SELECT dispatched, dispatched_at FROM batch_bill_dispatch WHERE batch_id = :b',
                    {'b': bid})
        if not disp or not disp['dispatched']:
            return _ok({'dispatched': False})
        b = _one(db, 'SELECT batch_no FROM batches WHERE id = :b', {'b': bid})
        bno = str(b['batch_no'] or '') if b else ''
        rows = _rows(db, """
            SELECT nature, weight, volume_weight, freight_settled FROM packed_packages
            WHERE batch_id = :b AND customer_id = :c AND status IN (%s)
        """ % _FREIGHT_STATUS_IN, {'b': bid, 'c': cid})
        groups = {
            'normal': {'count': 0, 'weight': 0.0, 'volume_weight': 0.0, 'computed': 0.0, 'paid': 0.0},
            'sensitive': {'count': 0, 'weight': 0.0, 'volume_weight': 0.0, 'computed': 0.0, 'paid': 0.0},
        }
        for r in rows:
            grp = 'sensitive' if _is_sensitive(r.get('nature')) else 'normal'
            g = groups[grp]
            g['count'] += 1
            w = _num_or_none(r.get('weight'))
            vw = _num_or_none(r.get('volume_weight'))
            if w is not None:
                g['weight'] += w
            if vw is not None:
                g['volume_weight'] += vw
            cw = _chargeable_weight(r)
            if cw is not None:
                g['computed'] += cw
                if r['freight_settled']:
                    g['paid'] += cw
        res = []
        for grp in ('normal', 'sensitive'):
            g = groups[grp]
            if g['count'] == 0:
                continue
            rate = FREIGHT_RATE_SENSITIVE if grp == 'sensitive' else FREIGHT_RATE_NORMAL
            effective, _ = _group_effective_chargeable(db, bid, cid, grp)
            freight = round(effective * rate, 2)
            paid_freight = round(g['paid'] * rate, 2)
            res.append({
                'nature': grp,
                'count': g['count'],
                'weight': round(g['weight'], 1),
                'volume_weight': round(g['volume_weight'], 3),
                'chargeable': round(effective, 3),
                'rate': rate,
                'freight': freight,
                'paid_freight': paid_freight,
                'pending_freight': round(freight - paid_freight, 2),
                'settled': bool(g['computed'] > 0 and g['paid'] >= g['computed'] - 1e-6),
            })
        return _ok({'dispatched': True, 'batch_no': bno,
                    'dispatched_at': (disp['dispatched_at'] if disp else ''),
                    'items': res,
                    'total_freight': round(sum(x['freight'] for x in res), 2),
                    'total_pending': round(sum(x['pending_freight'] for x in res), 2)})

    @bp.route('/api/client/bills', methods=['GET'])
    @require_auth
    def client_my_bills():
        """客户端「我的账单」：列出已派发、且包含本人包裹的批次账单。"""
        cid = require_bound_customer()
        if cid is None:
            return _err('请先绑定客户', 401, 40012)
        rows = _rows(db, """
            SELECT b.id AS bid, b.batch_no, d.dispatched_at
            FROM batch_bill_dispatch d
            JOIN batches b ON b.id = d.batch_id
            JOIN packed_packages pp ON pp.batch_id = d.batch_id AND pp.customer_id = :c
            WHERE d.dispatched = 1 AND pp.status IN (%s)
            GROUP BY b.id
            ORDER BY d.dispatched_at DESC
        """ % _FREIGHT_STATUS_IN, {'c': cid})
        out = []
        for r in rows:
            bid = int(r['bid'])
            total = 0.0
            for grp in ('normal', 'sensitive'):
                eff, _ = _group_effective_chargeable(db, bid, cid, grp)
                rate = FREIGHT_RATE_SENSITIVE if grp == 'sensitive' else FREIGHT_RATE_NORMAL
                total += eff * rate
            out.append({'batch_id': bid, 'batch_no': str(r['batch_no'] or ''),
                        'dispatched_at': r['dispatched_at'] or '',
                        'total_freight': round(total, 2)})
        return _ok({'items': out})

    @bp.route('/api/client/dim-confirms', methods=['GET'])
    @require_auth
    def client_dim_confirms():
        """客户端「尺寸确认」列表：已推送尺寸、且包含本人包裹的批次。"""
        cid = require_bound_customer()
        if cid is None:
            return _err('请先绑定客户', 401, 40012)
        rows = _rows(db, """
            SELECT b.id AS bid, b.batch_no, p.pushed_at
            FROM batch_dim_push p
            JOIN batches b ON b.id = p.batch_id
            JOIN packed_packages pp ON pp.batch_id = p.batch_id AND pp.customer_id = :c
            WHERE p.pushed = 1 AND pp.status IN (%s)
            GROUP BY b.id
            ORDER BY p.pushed_at DESC
        """ % _FREIGHT_STATUS_IN, {'c': cid})
        out = []
        for r in rows:
            bid = int(r['bid'])
            cf = _one(db, 'SELECT confirmed, confirmed_at FROM batch_dim_confirm WHERE batch_id = :b AND customer_id = :c',
                      {'b': bid, 'c': cid})
            cnt = _one(db, """
                SELECT COUNT(*) AS n FROM packed_packages
                WHERE batch_id = :b AND customer_id = :c AND status IN (%s)
            """ % _FREIGHT_STATUS_IN, {'b': bid, 'c': cid})
            out.append({
                'batch_id': bid,
                'batch_no': str(r['batch_no'] or ''),
                'pushed_at': r['pushed_at'] or '',
                'confirmed': bool(cf and cf['confirmed']),
                'confirmed_at': (cf['confirmed_at'] if cf else '') or '',
                'package_count': int((cnt or {}).get('n', 0))
            })
        return _ok({'items': out})

    @bp.route('/api/client/batch/<int:bid>/dims', methods=['GET'])
    @require_auth
    def client_batch_dims(bid):
        """客户端查看已推送的批次尺寸信息（仅本人包裹）。未推送返回 pushed=false。"""
        cid = require_bound_customer()
        if cid is None:
            return _err('请先绑定客户', 401, 40012)
        push = _one(db, 'SELECT pushed FROM batch_dim_push WHERE batch_id = :b', {'b': bid})
        if not push or not push['pushed']:
            return _ok({'pushed': False})
        b = _one(db, 'SELECT batch_no FROM batches WHERE id = :b', {'b': bid})
        bno = str(b['batch_no'] or '') if b else ''
        rows = _rows(db, """
            SELECT id, nature, length, width, height, weight, volume_weight
            FROM packed_packages
            WHERE batch_id = :b AND customer_id = :c AND status IN (%s)
            ORDER BY id
        """ % _FREIGHT_STATUS_IN, {'b': bid, 'c': cid})
        packages = []
        for r in rows:
            cw = _chargeable_weight(r)
            packages.append({
                'id': int(r['id']),
                'nature': 'sensitive' if _is_sensitive(r.get('nature')) else 'normal',
                'length': _num_or_none(r.get('length')),
                'width': _num_or_none(r.get('width')),
                'height': _num_or_none(r.get('height')),
                'weight': _num_or_none(r.get('weight')),
                'volume_weight': _num_or_none(r.get('volume_weight')),
                'chargeable': round(cw, 3) if cw is not None else None
            })
        cf = _one(db, 'SELECT confirmed, confirmed_at FROM batch_dim_confirm WHERE batch_id = :b AND customer_id = :c',
                  {'b': bid, 'c': cid})
        return _ok({
            'pushed': True,
            'batch_no': bno,
            'confirmed': bool(cf and cf['confirmed']),
            'confirmed_at': (cf['confirmed_at'] if cf else '') or '',
            'packages': packages
        })

    @bp.route('/api/client/batch/<int:bid>/confirm-dims', methods=['POST'])
    @require_auth
    def client_confirm_dims(bid):
        """客户端确认本批次尺寸信息无误（确认后管理端方可派发账单）。"""
        cid = require_bound_customer()
        if cid is None:
            return _err('请先绑定客户', 401, 40012)
        push = _one(db, 'SELECT pushed FROM batch_dim_push WHERE batch_id = :b', {'b': bid})
        if not push or not push['pushed']:
            return _err('该批次尚未推送尺寸信息')
        now = _now()
        _exec(db,
              """INSERT INTO batch_dim_confirm(batch_id, customer_id, confirmed, confirmed_at)
                 VALUES (:b, :c, 1, :t)
                 ON CONFLICT(batch_id, customer_id) DO UPDATE SET confirmed = 1, confirmed_at = :t""",
              {'b': bid, 'c': cid, 't': now})
        _audit_log('client_confirm_dims', 'batch={} customer={}'.format(bid, cid))
        return _ok({'confirmed': True, 'confirmed_at': now})

    @bp.route('/api/admin/batch/<int:bid>/customer/<int:cid>/settle-freight', methods=['POST'])
    @require_admin
    def admin_batch_customer_settle_freight(bid, cid):
        """按客户结清：仅标记该客户在该批次内未结算包裹已付款（整批不自动结清）。

        可选 nature 参数（query 或 JSON body 的 nature 字段）按普/敏拆开结清：
          - nature=normal    只结清该客户的普货
          - nature=sensitive 只结清该客户的敏货
          - 不传             结清该客户在本批次的全部（普+敏），保持旧行为兼容
        这样「花儿的幸福」这类客户可以把普货运费和敏货运费分开付款、分开标记。
        """
        b = _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid})
        if not b:
            return _err('批次不存在')
        cinfo = _one(db, 'SELECT id FROM customers WHERE id = :c', {'c': cid})
        if not cinfo:
            return _err('客户不存在')
        body = request.get_json(silent=True) or {}
        nature = str((request.args.get('nature') or body.get('nature') or '')).strip().lower()
        if nature and nature not in ('normal', 'sensitive'):
            return _err('nature 只能是 normal / sensitive')
        # 普/敏 SQL 筛分：与 _is_sensitive 完全对齐，避免两边口径漂移
        if nature == 'sensitive':
            nature_filter = " AND (nature LIKE '%敏%' OR LOWER(nature) IN ('sensitive','ming'))"
        elif nature == 'normal':
            nature_filter = " AND (nature IS NULL OR (nature NOT LIKE '%敏%' AND LOWER(nature) NOT IN ('sensitive','ming')))"
        else:
            nature_filter = ""
        # 运费金额：取「手动覆盖值（若有）否则系统计算值」× 单价。
        # 与账单页展示口径完全一致——管理端改了计费重，这里结算的就按改后的值。
        # （override 是 客户×普/敏 组级别；组内逐包裹标记 freight_settled=1）
        def _grp_amount(grp):
            eff, _ = _group_effective_chargeable(db, bid, cid, grp)
            rate = FREIGHT_RATE_SENSITIVE if grp == 'sensitive' else FREIGHT_RATE_NORMAL
            return round(eff * rate, 2)
        if nature:
            before = _grp_amount(nature)
        else:
            before = round(_grp_amount('normal') + _grp_amount('sensitive'), 2)
        if before <= 0:
            return _err('该客户在该批次' + ('的' + ('普货' if nature == 'normal' else '敏货') if nature else '') + '没有待付运费')
        now = _now()
        _exec(db,
              "UPDATE packed_packages SET freight_settled = 1, "
              "freight_paid_by = 'admin', freight_paid_at = :t "
              "WHERE batch_id = :b AND customer_id = :c AND status IN (" + _FREIGHT_STATUS_IN + ") "
              "AND (freight_settled IS NULL OR freight_settled = 0)" + nature_filter,
              {'b': bid, 'c': cid, 't': now})
        n = _scalar(db,
                    'SELECT COUNT(*) FROM packed_packages '
                    'WHERE batch_id = :b AND customer_id = :c '
                    'AND freight_paid_at = :t AND freight_paid_by = \'admin\'' + nature_filter,
                    {'b': bid, 'c': cid, 't': now}) or 0
        _audit_log('admin_batch_customer_settle_freight',
                   'batch={} customer={} nature={} amount={:.2f} packages={}'
                   .format(bid, cid, nature or 'all', before, n))
        return _ok({'customer_id': cid, 'nature': nature or 'all',
                    'settled_amount': round(before, 2), 'settled_count': int(n)})

    @bp.route('/api/admin/package/<int:pid>/toggle-paid', methods=['POST'])
    @require_admin
    def admin_toggle_paid(pid):
        """管理端标记/取消某包裹「已付款」（手动标记，无真实支付）。"""
        pk = _one(db, 'SELECT id FROM packed_packages WHERE id = :p', {'p': pid})
        if not pk:
            return _err('包裹不存在')
        cur = _scalar(db, 'SELECT freight_settled FROM packed_packages WHERE id = :p', {'p': pid}) or 0
        if int(cur):
            _exec(db,
                  'UPDATE packed_packages SET freight_settled = 0, '
                  'freight_paid_by = NULL, freight_paid_at = NULL WHERE id = :p',
                  {'p': pid})
            settled, by, at = 0, '', ''
        else:
            now = _now()
            _exec(db,
                  "UPDATE packed_packages SET freight_settled = 1, "
                  "freight_paid_by = 'admin', freight_paid_at = :t WHERE id = :p",
                  {'p': pid, 't': now})
            settled, by, at = 1, 'admin', now
        cid = _scalar(db, 'SELECT customer_id FROM packed_packages WHERE id = :p', {'p': pid})
        _audit_log('admin_toggle_paid', 'package={} settled={}'.format(pid, settled))
        return _ok({'id': pid, 'freight_settled': settled, 'freight_paid': bool(settled),
                    'freight_paid_by': by, 'freight_paid_at': at,
                    'pending_freight': _pending_freight(db, int(cid)) if cid else 0.0})

    @bp.route('/api/admin/package/<int:pid>/anomaly', methods=['POST'])
    @require_admin
    def admin_package_anomaly(pid):
        """标记/取消某包裹为问题件（新增 is_abnormal 标志位 + abnormal_remark）。"""
        body = request.get_json(silent=True) or {}
        abnormal = bool(body.get('abnormal', True))
        reason = str(body.get('reason') or '')
        scope = str(body.get('scope') or 'incoming')
        tbl = 'packed_packages' if scope == 'packed' else 'incoming_packages'
        info = _one(db, 'SELECT id FROM {} WHERE id = :p'.format(tbl), {'p': pid})
        if not info:
            return _err('包裹不存在')
        _exec(db,
              'UPDATE {} SET is_abnormal = :a, abnormal_remark = :r WHERE id = :p'.format(tbl),
              {'a': 1 if abnormal else 0, 'r': reason, 'p': pid})
        _audit_log('admin_package_anomaly',
                   'package={} scope={} abnormal={} reason={}'.format(pid, scope, abnormal, reason))
        return _ok({'id': pid, 'is_abnormal': 1 if abnormal else 0, 'abnormal_remark': reason})

    @bp.route('/api/admin/customer/<int:cid>/freight-detail', methods=['GET'])
    @require_admin
    def admin_freight_detail(cid):
        """客户运费逐包裹明细（计费重 × 单价），含已付款标记（全部包裹，不止未付）。"""
        rows = _rows(db,
            "SELECT id, serial_label, nature, weight, volume_weight, status, "
            "freight_settled, freight_paid_by, freight_paid_at FROM packed_packages "
            "WHERE customer_id = :cid AND status IN (%s) " % _FREIGHT_STATUS_IN,
            {'cid': cid})
        items = []
        unpaid = 0.0
        for r in rows:
            cw = _chargeable_weight(r)
            if cw is None:
                continue
            nature = str(r.get('nature') or '')
            rate = FREIGHT_RATE_SENSITIVE if ('敏' in nature or nature.lower() in ('sensitive', 'ming')) \
                else FREIGHT_RATE_NORMAL
            freight = round(cw * rate, 2)
            paid = bool(int(r.get('freight_settled') or 0))
            if not paid:
                unpaid += freight
            items.append({
                'id': int(r['id']),
                'label': str(r.get('serial_label') or ''),
                'nature': nature,
                'chargeable': cw,
                'rate': rate,
                'freight': freight,
                'status': str(r.get('status') or ''),
                'paid': paid,
                'paid_by': str(r.get('freight_paid_by') or ''),
                'paid_at': str(r.get('freight_paid_at') or ''),
            })
        return _ok({'items': items, 'total': round(unpaid, 2)})

    @bp.route('/api/admin/customer/<int:cid>/issue-coupon', methods=['POST'])
    @require_admin
    def admin_issue_coupon(cid):
        """给客户发放优惠券（新建 coupons 表记录）。"""
        info = _one(db, 'SELECT id FROM customers WHERE id = :cid', {'cid': cid})
        if not info:
            return _err('客户不存在')
        body = request.get_json(silent=True) or {}
        ctype = str(body.get('type') or 'amount')
        try:
            value = float(body.get('value') or 0)
        except (TypeError, ValueError):
            return _err('面额无效')
        note = str(body.get('note') or '')
        try:
            expire_days = int(body.get('expire_days') or 30)
        except (TypeError, ValueError):
            expire_days = 30
        if ctype not in ('amount', 'percent'):
            return _err('优惠券类型无效（amount/percent）')
        if value <= 0:
            return _err('面额必须大于 0')
        code = 'SJ' + datetime.now().strftime('%y%m%d') + \
               hashlib.md5((g.openid + str(time.time())).encode()).hexdigest()[:5].upper()
        expires = (datetime.now() + timedelta(days=expire_days)).strftime('%Y-%m-%d %H:%M:%S')
        _exec(db,
              "INSERT INTO coupons (customer_id, code, ctype, value, note, "
              "expires_at, issued_by, issued_at, status) "
              "VALUES (:cid, :code, :ctype, :value, :note, :exp, :op, :now, 'unused')",
              {'cid': cid, 'code': code, 'ctype': ctype, 'value': value,
               'note': note, 'exp': expires, 'op': g.openid, 'now': _now()})
        _audit_log('admin_issue_coupon',
                   'customer={} code={} ctype={} value={}'.format(cid, code, ctype, value))
        return _ok({'code': code, 'ctype': ctype, 'value': value, 'expires_at': expires})

    @bp.route('/api/admin/customer/<int:cid>/coupons', methods=['GET'])
    @require_admin
    def admin_coupon_list(cid):
        rows = _rows(db,
            "SELECT id, code, ctype, value, note, expires_at, status, "
            "used_at, issued_at FROM coupons WHERE customer_id = :cid ORDER BY id DESC",
            {'cid': cid})
        out = [dict(r) for r in rows]
        return _ok({'list': out})

    @bp.route('/api/admin/customer/<int:cid>/redeem-coupon', methods=['POST'])
    @require_admin
    def admin_redeem_coupon(cid):
        """用券抵扣该客户待付运费（核销 = 将其未结算包裹置 freight_settled=1）。"""
        body = request.get_json(silent=True) or {}
        code = str(body.get('code') or '')
        cp = _one(db, 'SELECT id, customer_id, status, value, ctype FROM coupons WHERE code = :c',
                  {'c': code})
        if not cp:
            return _err('优惠券不存在')
        if int(cp['customer_id']) != cid:
            return _err('该优惠券不属于此客户')
        if str(cp['status']) != 'unused':
            return _err('优惠券已使用或已失效')
        before = _pending_freight(db, cid)
        if before <= 0:
            return _err('该客户当前无待付运费，无法抵扣')
        _exec(db,
              "UPDATE packed_packages SET freight_settled = 1 "
              "WHERE customer_id = :cid AND status IN (%s) "
              "AND (freight_settled IS NULL OR freight_settled = 0)" % _FREIGHT_STATUS_IN,
              {'cid': cid})
        _exec(db,
              "UPDATE coupons SET status = 'used', used_at = :t, used_amount = :a WHERE id = :id",
              {'t': _now(), 'a': round(before, 2), 'id': int(cp['id'])})
        _audit_log('admin_redeem_coupon', 'customer={} code={}'.format(cid, code))
        return _ok({'redeemed_amount': round(before, 2),
                    'remaining': _pending_freight(db, cid)})

    @bp.route('/api/admin/dashboard', methods=['GET'])
    @require_admin
    def admin_dashboard():
        """管理端主页「运营驾驶舱」聚合指标：全局 KPI + 待办。
        2026-11-06 口径调整：
          customer_total 只数「有记录」客户；无记录客户单列 pool_count（意向池）。
          abnormal/orphan 改为系统端真实「无主件」（is_anonymous + customer_id IS NULL），
          不再是小程序自定义的 is_abnormal 标记。
        """
        customer_total = _scalar(db, 'SELECT COUNT(*) FROM customers c WHERE ' + HAS_RECORD_SQL) or 0
        pool_count = _scalar(db, 'SELECT COUNT(*) FROM customers c WHERE ' + NO_RECORD_SQL) or 0
        in_stock = _scalar(db,
                           "SELECT COUNT(*) FROM incoming_packages WHERE status = '已入库'") or 0
        freight = _freight_map(db)
        pending_freight = round(sum(freight.values()), 2) if freight else 0.0
        orphan = _scalar(db, 'SELECT COUNT(*) FROM incoming_packages WHERE ' + ORPHAN_WHERE) or 0
        pending_bind = _scalar(db,
                               'SELECT COUNT(*) FROM client_users WHERE bind_status = 1') or 0
        week = _week_label()
        pending_intent = _scalar(db, """
            SELECT COUNT(*) FROM shipment_intents
            WHERE scope = :sc AND week_label = :wk
              AND intent_status NOT IN ('active', 'locked')
        """, {'sc': CONFIRM_SCOPE, 'wk': week}) or 0
        locked_batches = _scalar(db, 'SELECT COUNT(*) FROM batches WHERE is_locked = 1') or 0
        return _ok({
            'customer_total': int(customer_total),
            'pool_count': int(pool_count),
            'in_stock': int(in_stock),
            'pending_freight': pending_freight,
            'abnormal': int(orphan),
            'orphan': int(orphan),
            'pending_bind': int(pending_bind),
            'pending_intent': int(pending_intent),
            'locked_batches': int(locked_batches),
            'week_label': week,
        })

    # =======================================================================
    # 2026-11-06 管理端增强（用户 8 项需求）
    #   1 新建/删除客户（写系统 customers 表，编号规则与 app.py 一致）
    #   2/8 无记录客户隐藏 + 意向池（见 HAS_RECORD_SQL / admin_customers pool=1）
    #   3 入库单号操作：删除 / 退货 / 恢复 / 转无主件（状态口径同 app.py）
    #   4 无主件：对接系统 is_anonymous + customer_id IS NULL，支持归户/删除
    #   5 解绑（见 admin_bind_audit unbind）  6 批次锁定禁用（见 admin_batch_lock）
    #   7 批次物流轨迹（batch_tracks 小程序侧独立表，客户端可同步查看）
    # =======================================================================

    def _new_customer_no():
        """客户号 C{YYYYMMDD}{4位序号}，与 app.py generate_customer_no 同规则。"""
        prefix = 'C' + datetime.now().strftime('%Y%m%d')
        last = _scalar(db,
                       'SELECT customer_no FROM customers WHERE customer_no LIKE :p '
                       'ORDER BY customer_no DESC LIMIT 1', {'p': prefix + '%'})
        seq = 1
        if last:
            try:
                seq = int(str(last)[len(prefix):]) + 1
            except (TypeError, ValueError):
                seq = 1
        # 极小概率并发撞号时向后顺延
        while _scalar(db, 'SELECT COUNT(*) FROM customers WHERE customer_no = :n',
                      {'n': '%s%04d' % (prefix, seq)}):
            seq += 1
        return '%s%04d' % (prefix, seq)

    def _new_barcode():
        """条码：1 大写字母 + 5 位数字，与 app.py generate_customer_barcode 同规则。"""
        for _ in range(30):
            raw = os.urandom(6)
            code = chr(ord('A') + raw[0] % 26) + ''.join(str(b % 10) for b in raw[1:])
            if not _scalar(db, 'SELECT COUNT(*) FROM customers WHERE barcode = :b', {'b': code}):
                return code
        return 'Z' + str(int(time.time()))[-5:]

    def _batch_locked_of(pid):
        """入库单所属批次是否被系统端锁定；锁定批次的写操作一律拒绝（同 app.py 全局守卫）。"""
        row = _one(db, """
            SELECT b.is_locked, b.batch_no FROM incoming_packages ip
            LEFT JOIN batches b ON b.id = ip.batch_id WHERE ip.id = :p
        """, {'p': pid})
        return bool(row and int(row.get('is_locked') or 0) == 1), \
            (row.get('batch_no') if row else '')

    @bp.route('/api/admin/customer/create', methods=['POST'])
    @require_admin
    def admin_customer_create():
        """新建客户（与系统 /api/customer/add 同字段、同编号规则，实时同步到系统）。"""
        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or '').strip()
        address = str(body.get('address') or '').strip()
        zip_code = str(body.get('zip_code') or '').strip()
        phone = str(body.get('phone') or '').strip()
        remark = str(body.get('remark') or '').strip()
        is_vip = 1 if body.get('is_vip') else 0
        invoice_contact = str(body.get('invoice_contact') or '').strip()
        if not name:
            return _err('请填写客户名')
        if len(name) > 50:
            return _err('客户名不能超过 50 字')
        if not address:
            return _err('请填写收货地址（系统端必填）')
        if len(address) > 200:
            return _err('地址不能超过 200 字')
        if is_vip and not invoice_contact:
            return _err('VIP 客户请填写发票联系人')
        if _scalar(db, 'SELECT COUNT(*) FROM customers WHERE name = :n', {'n': name}):
            return _err('客户名已存在：' + name)
        cno = _new_customer_no()
        barcode = _new_barcode()
        # 系统端 VIP 用 invoice_address 覆盖 address；管理端简化为同值
        _exec(db, """
            INSERT INTO customers
              (customer_no, name, barcode, address, zip_code, remark, is_vip, phone,
               invoice_contact, invoice_address, shipment_count, pending_freight,
               has_box, has_bulk, created_at)
            VALUES (:no, :n, :bc, :ad, :zp, :rk, :vip, :ph, :ic, :iad, 0, 0, 0, 0, :t)
        """, {'no': cno, 'n': name, 'bc': barcode, 'ad': address, 'zp': zip_code,
              'rk': remark, 'vip': is_vip, 'ph': phone, 'ic': invoice_contact,
              'iad': address if is_vip else '', 't': _now()})
        row = _one(db, 'SELECT id FROM customers WHERE customer_no = :no', {'no': cno})
        cid = int(row['id']) if row else 0
        _audit_log('ADD_CUSTOMER', 'admin create customer={} no={}'.format(cid, cno))
        return _ok({'id': cid, 'name': name, 'customer_no': cno, 'barcode': barcode,
                    'zip_code': zip_code}, '客户已创建，系统端立即可见')

    @bp.route('/api/admin/customer/<int:cid>/delete', methods=['POST'])
    @require_admin
    def admin_customer_delete(cid):
        """删除客户：沿用系统 /api/customer/delete 的三道前置校验，只有意向池客户可删。"""
        info = _one(db, 'SELECT id, name FROM customers WHERE id = :c', {'c': cid})
        if not info:
            return _err('客户不存在')
        if _scalar(db, 'SELECT COUNT(*) FROM incoming_packages WHERE customer_id = :c', {'c': cid}):
            return _err('该客户有入库记录，不能删除')
        if _scalar(db, 'SELECT COUNT(*) FROM packed_packages WHERE customer_id = :c', {'c': cid}):
            return _err('该客户有打包/发货记录，不能删除')
        if float(_scalar(db, 'SELECT pending_freight FROM customers WHERE id = :c', {'c': cid}) or 0) > 0:
            return _err('该客户有待付运费，不能删除')
        if _scalar(db,
                   'SELECT COUNT(*) FROM client_users WHERE customer_id = :c AND bind_status IN (1,2)',
                   {'c': cid}):
            return _err('该客户已被小程序绑定/待审核，请先解绑再删除')
        _exec(db, 'DELETE FROM customers WHERE id = :c', {'c': cid})
        _audit_log('DELETE_CUSTOMER', 'admin delete customer={} name={}'.format(cid, info['name']))
        return _ok({'id': cid}, '客户已删除，系统端同步生效')

    @bp.route('/api/admin/customer/lookup', methods=['GET'])
    @require_admin
    def admin_customer_lookup():
        """全量客户搜索（含意向池），供无主件归户时选客户。"""
        kw = str(request.args.get('keyword') or '').strip()
        if not kw:
            return _ok({'list': []})
        rows = _rows(db, """
            SELECT id, name, customer_no, zip_code, address FROM customers
            WHERE name LIKE :kw OR customer_no LIKE :kw OR zip_code LIKE :kw
                  OR barcode LIKE :kw
            ORDER BY name LIMIT 20
        """, {'kw': '%' + kw + '%'})
        return _ok({'list': rows})

    @bp.route('/api/admin/inbound/<int:pid>/delete', methods=['POST'])
    @require_admin
    def admin_inbound_delete(pid):
        """删除入库单（同 app.py api_delete_inbound：物理删除，仅 已入库/待打包）。"""
        pkg = _one(db, 'SELECT id, status, tracking_number FROM incoming_packages WHERE id = :p',
                   {'p': pid})
        if not pkg:
            return _err('单号不存在')
        if str(pkg['status']) not in ('已入库', '待打包'):
            return _err('仅「已入库 / 待打包」的单号可删除')
        locked, bno = _batch_locked_of(pid)
        if locked:
            return _err('批次 {} 已锁定，请在系统端处理'.format(bno))
        _exec(db, 'DELETE FROM incoming_packages WHERE id = :p', {'p': pid})
        _audit_log('DELETE_INBOUND',
                   'admin delete inbound={} tracking={}'.format(pid, pkg['tracking_number']))
        return _ok({'id': pid}, '单号已删除，货位随记录释放')

    @bp.route('/api/admin/inbound/<int:pid>/return', methods=['POST'])
    @require_admin
    def admin_inbound_return(pid):
        """退货（同 app.py api_inbound_mark_returned：置 status='已退货'，不物理删除）。"""
        pkg = _one(db, 'SELECT id, status, tracking_number FROM incoming_packages WHERE id = :p',
                   {'p': pid})
        if not pkg:
            return _err('单号不存在')
        if str(pkg['status']) == '已打包':
            return _err('已打包单号不能退货，请在系统端先撤包')
        locked, bno = _batch_locked_of(pid)
        if locked:
            return _err('批次 {} 已锁定，请在系统端处理'.format(bno))
        reason = str((request.get_json(silent=True) or {}).get('reason') or '').strip()
        _exec(db, "UPDATE incoming_packages SET status = '已退货', remark = :r WHERE id = :p",
              {'r': reason, 'p': pid})
        _audit_log('MARK_RETURNED',
                   'admin return inbound={} tracking={} reason={}'.format(
                       pid, pkg['tracking_number'], reason))
        return _ok({'id': pid, 'status': '已退货'}, '单号已标记退货')

    @bp.route('/api/admin/inbound/<int:pid>/restore', methods=['POST'])
    @require_admin
    def admin_inbound_restore(pid):
        """恢复退货单号（同 app.py restore_returned：已退货 → 已入库）。"""
        pkg = _one(db, "SELECT id, status FROM incoming_packages WHERE id = :p AND status = '已退货'",
                   {'p': pid})
        if not pkg:
            return _err('该单号不是已退货状态')
        _exec(db, "UPDATE incoming_packages SET status = '已入库' WHERE id = :p", {'p': pid})
        _audit_log('RESTORE_RETURNED', 'admin restore inbound={}'.format(pid))
        return _ok({'id': pid, 'status': '已入库'}, '已恢复为入库状态')

    @bp.route('/api/admin/inbound/<int:pid>/mark-orphan', methods=['POST'])
    @require_admin
    def admin_inbound_mark_orphan(pid):
        """把单号转为「无主件」（系统口径：customer_id=NULL + is_anonymous=1）。"""
        pkg = _one(db, 'SELECT id, status, tracking_number FROM incoming_packages WHERE id = :p',
                   {'p': pid})
        if not pkg:
            return _err('单号不存在')
        if str(pkg['status']) not in ('已入库', '待打包'):
            return _err('仅「已入库 / 待打包」的单号可转无主件')
        locked, bno = _batch_locked_of(pid)
        if locked:
            return _err('批次 {} 已锁定，请在系统端处理'.format(bno))
        _exec(db, """
            UPDATE incoming_packages SET customer_id = NULL, is_anonymous = 1,
                                        sequence_no = NULL WHERE id = :p
        """, {'p': pid})
        _audit_log('MARK_ORPHAN',
                   'admin orphan inbound={} tracking={}'.format(pid, pkg['tracking_number']))
        return _ok({'id': pid}, '已转入无主件，可在系统端归户')

    @bp.route('/api/admin/orphans', methods=['GET'])
    @require_admin
    def admin_orphans():
        """无主件列表（与系统 /anonymous 页面同口径）。"""
        rows = _rows(db, """
            SELECT ip.id, ip.tracking_number, ip.entry_time, ip.status,
                   ip.customer_remark, ip.abnormal_remark, ip.batch_id,
                   b.batch_no, b.zip_code
            FROM incoming_packages ip
            LEFT JOIN batches b ON b.id = ip.batch_id
            WHERE """ + ORPHAN_WHERE + " ORDER BY ip.entry_time DESC LIMIT 200")
        for r in rows:
            t = r.get('entry_time')
            r['entry_time_display'] = t.strftime('%Y-%m-%d %H:%M') if hasattr(t, 'strftime') else str(t or '')
            r.pop('entry_time', None)
        return _ok({'list': rows, 'total': len(rows)})

    @bp.route('/api/admin/orphan/<int:pid>/assign', methods=['POST'])
    @require_admin
    def admin_orphan_assign(pid):
        """无主件归户（同 app.py api_anonymous_assign：重排序号 + 回填收货地址）。"""
        body = request.get_json(silent=True) or {}
        try:
            cid = int(body.get('customer_id') or 0)
        except (TypeError, ValueError):
            return _err('客户 id 无效')
        pkg = _one(db, 'SELECT id, is_anonymous FROM incoming_packages WHERE id = :p', {'p': pid})
        if not pkg or not int(pkg['is_anonymous'] or 0):
            return _err('该单号不是无主件')
        cust = _one(db, 'SELECT id, name, address FROM customers WHERE id = :c', {'c': cid})
        if not cust:
            return _err('客户不存在')
        max_seq = _scalar(db,
                          'SELECT COALESCE(MAX(sequence_no), 0) FROM incoming_packages '
                          'WHERE customer_id = :c', {'c': cid}) or 0
        _exec(db, """
            UPDATE incoming_packages SET customer_id = :c, is_anonymous = 0,
                   sequence_no = :s, receiving_address = :ad WHERE id = :p
        """, {'c': cid, 's': int(max_seq) + 1, 'ad': cust.get('address') or '', 'p': pid})
        _audit_log('ASSIGN_ORPHAN',
                   'admin assign orphan={} -> customer={}({})'.format(pid, cid, cust['name']))
        return _ok({'id': pid, 'customer_id': cid},
                   '已归户到 ' + str(cust['name']) + '，系统端同步可见')

    @bp.route('/api/admin/orphan/<int:pid>/delete', methods=['POST'])
    @require_admin
    def admin_orphan_delete(pid):
        """删除无主件（同 app.py api_anonymous_delete：物理删除）。"""
        pkg = _one(db, 'SELECT id, is_anonymous FROM incoming_packages WHERE id = :p', {'p': pid})
        if not pkg or not int(pkg['is_anonymous'] or 0):
            return _err('该单号不是无主件，请在入库明细中操作')
        _exec(db, 'DELETE FROM incoming_packages WHERE id = :p', {'p': pid})
        _audit_log('DELETE_ORPHAN', 'admin delete orphan={}'.format(pid))
        return _ok({'id': pid}, '无主件已删除')

    # ---------------- 批次物流轨迹 ----------------
    def _md(value):
        s = str(value or '').strip()
        if not s:
            return '--月--日'
        try:
            parts = s.split('-')
            return '{}月{}日'.format(int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            return s

    def _track_display(r):
        if str(r.get('progress') or '').strip():
            return str(r['progress']).strip()
        return '预计{}开船，预计{}到港，预计{}提柜，预计{}签收'.format(
            _md(r.get('sail_date')), _md(r.get('arrive_date')),
            _md(r.get('pickup_date')), _md(r.get('sign_date')))

    def _track_decorate(r):
        out = dict(r)
        # node_data 在库里是 JSON 字符串，接口统一返回对象给前端
        raw = r.get('node_data') or ''
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else {}
            except (ValueError, TypeError):
                raw = {}
        out['node_data'] = raw if isinstance(raw, dict) else {}
        out['progress_display'] = _track_display(r)
        out['carton_display'] = '{}箱'.format(int(r.get('carton_count') or 0))
        return out

    @bp.route('/api/admin/batch/<int:bid>/tracks', methods=['GET'])
    @require_admin
    def admin_batch_tracks(bid):
        """读取批次轨迹：固定普货/敏货两组，缺的组返回空行便于编辑。"""
        batch = _one(db, 'SELECT id, batch_no, zip_code FROM batches WHERE id = :b', {'b': bid})
        if not batch:
            return _err('批次不存在')
        saved = {r['group_name']: r for r in _rows(db, """
            SELECT group_name, internal_no, transfer_no, carton_count,
                   sail_date, arrive_date, pickup_date, sign_date, progress,
                   current_node, node_data, updated_at
            FROM batch_tracks WHERE batch_id = :b
        """, {'b': bid})}
        rows = []
        for grp in TRACK_GROUPS:
            base = {'group_name': grp, 'internal_no': '', 'transfer_no': '', 'carton_count': 0,
                    'sail_date': '', 'arrive_date': '', 'pickup_date': '', 'sign_date': '',
                    'progress': '', 'current_node': '', 'node_data': {}, 'updated_at': ''}
            base.update(saved.get(grp) or {})
            rows.append(_track_decorate(base))
        return _ok({'batch': batch, 'rows': rows})

    @bp.route('/api/admin/batch/<int:bid>/tracks', methods=['POST'])
    @require_admin
    def admin_batch_tracks_save(bid):
        """保存批次轨迹（普货/敏货两组 upsert）；系统端不同步，客户端可实时读取。"""
        if not _one(db, 'SELECT id FROM batches WHERE id = :b', {'b': bid}):
            return _err('批次不存在')
        payload = (request.get_json(silent=True) or {}).get('rows') or []
        saved = 0
        for raw in payload:
            grp = str(raw.get('group_name') or '').strip()
            if grp not in TRACK_GROUPS:
                continue
            try:
                cnt = max(0, int(raw.get('carton_count') or 0))
            except (TypeError, ValueError):
                cnt = 0
            existing = _one(db, """
                SELECT sail_date, arrive_date, pickup_date, sign_date, progress,
                       carton_count, current_node, node_data
                FROM batch_tracks WHERE batch_id = :b AND group_name = :g
            """, {'b': bid, 'g': grp}) or {}

            # 旧版四个日期字段：新版管理端不再展示，缺键时保留库中原值，避免覆盖历史数据
            def legacy(key, limit=20):
                val = raw[key] if key in raw else (existing.get(key) or '')
                return str(val or '').strip()[:limit]

            # 按节点存明细：{node_key: {date, note}}，与客户端 12 节点对应
            nd_raw = raw.get('node_data')
            if not isinstance(nd_raw, dict):
                nd_raw = {}
                if 'node_data' not in raw and existing.get('node_data'):
                    try:
                        nd_raw = json.loads(existing['node_data']) or {}
                    except (ValueError, TypeError):
                        nd_raw = {}
            nd_clean = {}
            for k, v in nd_raw.items():
                if not isinstance(v, dict):
                    continue
                nd_clean[str(k)[:40]] = {
                    'date': str(v.get('date') or '').strip()[:20],
                    'note': str(v.get('note') or '').strip()[:200],
                }
            vals = {
                'b': bid, 'g': grp,
                'ino': str(raw.get('internal_no') or '').strip()[:100],
                'tno': str(raw.get('transfer_no') or '').strip()[:100],
                'cnt': cnt,
                'sail': legacy('sail_date'),
                'arr': legacy('arrive_date'),
                'pick': legacy('pickup_date'),
                'sign': legacy('sign_date'),
                'pg': legacy('progress', 300),
                'cur': str(raw.get('current_node')
                           if 'current_node' in raw
                           else (existing.get('current_node') or '')).strip()[:40],
                'nd': json.dumps(nd_clean, ensure_ascii=False),
                't': _now(), 'o': g.openid,
            }
            exists = _scalar(db, 'SELECT COUNT(*) FROM batch_tracks WHERE batch_id = :b AND group_name = :g',
                             {'b': bid, 'g': grp})
            if exists:
                _exec(db, """
                    UPDATE batch_tracks SET internal_no=:ino, transfer_no=:tno,
                        carton_count=:cnt, sail_date=:sail, arrive_date=:arr,
                        pickup_date=:pick, sign_date=:sign, progress=:pg,
                        current_node=:cur, node_data=:nd,
                        updated_at=:t, updated_by=:o
                    WHERE batch_id=:b AND group_name=:g
                """, vals)
            else:
                _exec(db, """
                    INSERT INTO batch_tracks (batch_id, group_name, internal_no, transfer_no,
                        carton_count, sail_date, arrive_date, pickup_date, sign_date,
                        progress, current_node, node_data, updated_at, updated_by)
                    VALUES (:b, :g, :ino, :tno, :cnt, :sail, :arr, :pick, :sign,
                            :pg, :cur, :nd, :t, :o)
                """, vals)
            saved += 1
        _audit_log('BATCH_TRACK', 'admin batch={} groups={}'.format(bid, saved))
        return _ok({'batch_id': bid, 'saved': saved}, '轨迹已更新，客户端可同步查看')

    @bp.route('/api/client/tracks', methods=['GET'])
    @require_auth
    def client_tracks():
        """客户端物流轨迹：只显示「该客户在该批次确有对应货物」的组（2026-11-06 口径）。

          - 客户在某批次没有任何入库/打包记录 → 整个批次不返回；
          - 只有普货 → 只返回普货组；只有敏货 → 只返回敏货组；两类都有 → 两组；
          - 未绑定客户的微信号 → 返回空列表。
        普敏判定：incoming_packages 用 is_danger（1=敏、0/NULL=普；nature 列有历史脏值），
                  packed_packages 用 nature（含「敏」为敏，其余为普）。
        """
        cid = require_bound_customer()
        if not cid:
            return _ok({'list': [], 'scope': 'unbound'})

        # 单批次过滤：从「单独地址」等批次卡点「轨迹」进来只看该批次（2026-11-07）。
        # 管理员可看任意批次（预览口径，不要求本人在该批次有货）；
        # 普通客户仍受「本人在该批次确有货物」约束，无货则返回空。
        only_bid = request.args.get('batch_id', type=int)
        is_admin = g.openid in ADMIN_OPENIDS
        if only_bid and is_admin:
            rows = _rows(db, """
                SELECT t.batch_id, b.batch_no, b.zip_code, t.group_name, t.internal_no,
                       t.transfer_no, t.carton_count, t.sail_date, t.arrive_date,
                       t.pickup_date, t.sign_date, t.progress, t.current_node,
                       t.node_data, t.updated_at
                FROM batch_tracks t
                JOIN batches b ON b.id = t.batch_id
                WHERE t.batch_id = :b
                ORDER BY CASE t.group_name WHEN '普货' THEN 0 ELSE 1 END
            """, {'b': only_bid})
            grp = {'batch_id': only_bid, 'batch_no': '', 'zip_code': '', 'rows': []}
            for r in rows:
                if not grp['batch_no']:
                    grp['batch_no'] = r.get('batch_no') or ''
                    grp['zip_code'] = r.get('zip_code') or ''
                grp['rows'].append(_track_decorate(r))
            return _ok({'list': [grp] if grp['rows'] else [],
                        'scope': 'admin_batch', 'batch_id': only_bid})

        links = _rows(db, """
            SELECT batch_id,
                   MAX(CASE WHEN is_danger = 1 THEN 1 ELSE 0 END) AS has_sensitive,
                   MAX(CASE WHEN is_danger IS NULL OR is_danger = 0 THEN 1 ELSE 0 END) AS has_normal
            FROM incoming_packages
            WHERE customer_id = :cid AND batch_id IS NOT NULL
            GROUP BY batch_id
        """, {'cid': cid})
        links += _rows(db, """
            SELECT batch_id,
                   MAX(CASE WHEN nature LIKE '%敏%' OR LOWER(nature) IN ('sensitive', 'ming')
                            THEN 1 ELSE 0 END) AS has_sensitive,
                   MAX(CASE WHEN nature IS NULL OR nature = ''
                             OR (nature NOT LIKE '%敏%'
                                 AND LOWER(nature) NOT IN ('sensitive', 'ming'))
                            THEN 1 ELSE 0 END) AS has_normal
            FROM packed_packages
            WHERE customer_id = :cid AND batch_id IS NOT NULL
            GROUP BY batch_id
        """, {'cid': cid})
        flags = {}
        for r in links:
            if not r.get('batch_id'):
                continue
            bid = int(r['batch_id'])
            f = flags.setdefault(bid, {'normal': False, 'sensitive': False})
            f['normal'] = f['normal'] or bool(r['has_normal'])
            f['sensitive'] = f['sensitive'] or bool(r['has_sensitive'])
        if not flags:
            return _ok({'list': [], 'scope': 'customer', 'customer_id': cid})

        # 普通客户带 batch_id 时：只保留该批次（且必须本人在该批次确有货物）
        if only_bid:
            f = flags.get(only_bid)
            if not f:
                return _ok({'list': [], 'scope': 'customer', 'customer_id': cid,
                            'batch_id': only_bid})
            flags = {only_bid: f}
            ids = [only_bid]
        else:
            ids = sorted(flags.keys(), reverse=True)[:40]
        params = {('p' + str(i)): b for i, b in enumerate(ids)}
        in_sql = ','.join(':' + k for k in params)
        rows = _rows(db, """
            SELECT t.batch_id, b.batch_no, b.zip_code, t.group_name, t.internal_no,
                   t.transfer_no, t.carton_count, t.sail_date, t.arrive_date,
                   t.pickup_date, t.sign_date, t.progress, t.current_node,
                   t.node_data, t.updated_at
            FROM batch_tracks t
            JOIN batches b ON b.id = t.batch_id
            WHERE t.batch_id IN ({in_sql})
            ORDER BY t.batch_id DESC, CASE t.group_name WHEN '普货' THEN 0 ELSE 1 END
        """.format(in_sql=in_sql), params)
        groups = {}
        for r in rows:
            bid = int(r['batch_id'])
            f = flags.get(bid)
            if not f:
                continue
            is_normal = str(r['group_name'] or '') != '敏货'
            if is_normal and not f['normal']:
                continue
            if not is_normal and not f['sensitive']:
                continue
            if bid not in groups:
                groups[bid] = {'batch_id': bid, 'batch_no': r.get('batch_no') or '',
                               'zip_code': r.get('zip_code') or '', 'rows': []}
            groups[bid]['rows'].append(_track_decorate(r))
        return _ok({'list': list(groups.values()),
                    'scope': 'customer', 'customer_id': cid})

    return bp
