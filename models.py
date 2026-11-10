# -*- coding: utf-8 -*-
"""
集运管理系统数据模型
按照《集运管理系统_核心功能设计.docx》策划书设计
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import pytz

db = SQLAlchemy()


def get_local_time():
    """获取本地时间（北京时间）"""
    return datetime.now(pytz.timezone('Asia/Shanghai'))


class Customer(db.Model):
    """客户管理表 - 存储所有客户的基本信息"""
    __tablename__ = 'customers'

    id = db.Column(db.Integer, primary_key=True)
    # 客户编号 - 系统自动生成
    customer_no = db.Column(db.String(20), unique=True, nullable=False, index=True, comment='客户编号')
    # 客户姓名
    name = db.Column(db.String(50), nullable=False, unique=True, comment='客户姓名')
    # 条形码 - 与客户严格一一对应
    barcode = db.Column(db.String(100), nullable=False, unique=True, index=True, comment='条形码')
    # 收货地址
    address = db.Column(db.String(200), nullable=False, comment='收货地址')
    # 邮编 - 美国邮政编码，通常为5位数字
    zip_code = db.Column(db.String(10), index=True, comment='美国邮政编码')
    # 发货次数 - 累计发货次数
    shipment_count = db.Column(db.Integer, default=0, comment='发货次数')
    # 待付运费 - 待结算的运费金额
    pending_freight = db.Column(db.Float, default=0.0, comment='待付运费')
    # 创建时间
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')
    # 备注
    remark = db.Column(db.Text, comment='备注信息')
    # 货物标记 - 箱子/大货
    has_box = db.Column(db.Boolean, default=False, comment='是否有箱装货物')
    has_bulk = db.Column(db.Boolean, default=False, comment='是否有大件货物')
    # 扫描挂起状态 - 用于二次扫描进度保存
    scan_hung_at = db.Column(db.DateTime, nullable=True, comment='扫描挂起时间')
    scan_hung_verified = db.Column(db.Integer, default=0, comment='挂起时已扫描数量')
    scan_hung_total = db.Column(db.Integer, default=0, comment='挂起时总数量')
    scan_hung_ids = db.Column(db.Text, nullable=True, comment='挂起时已扫描包裹ID(JSON数组)')
    scan_hung_danger_ids = db.Column(db.Text, nullable=True, comment='挂起时已标记敏货包裹ID(JSON数组)')

    # === VIP客户字段（单独发货） ===
    is_vip = db.Column(db.Boolean, default=False, comment='是否VIP客户（可单独发货）')
    phone = db.Column(db.String(30), default='', comment='客户电话')
    invoice_contact = db.Column(db.String(50), default='', comment='发票联系人（覆盖模板B11）')
    invoice_phone = db.Column(db.String(30), default='', comment='发票联系电话（覆盖模板B12）')
    invoice_address = db.Column(db.String(200), default='', comment='发票地址（覆盖模板B16）')
    invoice_zip = db.Column(db.String(20), default='', comment='发票邮编（覆盖模板B15）')
    company_name = db.Column(db.String(100), default='', comment='公司名')
    country = db.Column(db.String(50), default='', comment='国家')

    # 关系
    incoming_packages = db.relationship('IncomingPackage', backref='customer', lazy='dynamic')
    packed_packages = db.relationship('PackedPackage', backref='customer', lazy='dynamic')

    def __repr__(self):
        return f'<Customer {self.customer_no} - {self.name}>'


class AddressBook(db.Model):
    """地址库 - 按邮编存储发票填写信息，普/敏货各一套"""
    __tablename__ = 'address_book'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    zip_code = db.Column(db.String(20), default='', comment='邮编（索引）')
    # 普货
    puhuo_consignee = db.Column(db.String(50), default='', comment='普货收货人')
    puhuo_phone = db.Column(db.String(30), default='', comment='普货电话')
    puhuo_address = db.Column(db.String(200), default='', comment='普货地址')
    # 敏货
    minhuo_consignee = db.Column(db.String(50), default='', comment='敏货收货人')
    minhuo_phone = db.Column(db.String(30), default='', comment='敏货电话')
    minhuo_address = db.Column(db.String(200), default='', comment='敏货地址')
    # 共用
    city = db.Column(db.String(50), default='', comment='城市')
    state = db.Column(db.String(50), default='', comment='省份/州')
    country = db.Column(db.String(50), default='', comment='国家')
    remark = db.Column(db.Text, default='', comment='备注')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')

    def get_puhuo_consignee(self):
        return self.puhuo_consignee or self.minhuo_consignee
    def get_minhuo_consignee(self):
        return self.minhuo_consignee or self.puhuo_consignee

    def __repr__(self):
        return f'<AddressBook {self.zip_code}>'


class ShelfSlot(db.Model):
    """货位表 - 货架按地址+字母命名（如蒙市A、蒙市B），每架4层，每层5个位置(1~5)
    支持按客户分配，也支持按收货地址分配整架货架（address_label字段）
    """
    __tablename__ = 'shelf_slots'

    id = db.Column(db.Integer, primary_key=True)
    # 货架编号，地址+字母如 蒙市A、蒙市B、工业市D...（由assign_shelves_by_address动态创建）
    shelf_code = db.Column(db.String(50), nullable=False, index=True, comment='货架编号，如蒙市A')
    # 层数 1~4
    floor = db.Column(db.Integer, nullable=False, comment='层数1~4')
    # 位置 1~5
    zone = db.Column(db.String(5), nullable=False, default='1', comment='位置1~5')
    # 货位完整编号，如 蒙市A-1-1
    slot_label = db.Column(db.String(50), nullable=False, unique=True, index=True, comment='货位标签，如蒙市A-1-1')
    # 货架行（保留兼容，地址命名下为空字符串）
    row = db.Column(db.String(5), nullable=False, comment='货架行（保留兼容）')
    # 货架列（保留兼容，地址命名下为0）
    col = db.Column(db.Integer, nullable=False, comment='货架列（保留兼容）')
    # 分配给哪个客户（NULL=未分配）
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True, comment='分配给客户ID')
    # 收货地址标签（按地址分配整架货架时使用，如"蒙市"、"工业市"）
    address_label = db.Column(db.String(50), comment='收货地址标签')
    # 备注/标注
    remark = db.Column(db.String(200), comment='备注')

    # 关联的入库包裹
    packages = db.relationship('IncomingPackage', backref='shelf_slot', lazy='dynamic')
    customer = db.relationship('Customer', backref='shelf_slots', lazy='select')

    def __repr__(self):
        return f'<ShelfSlot {self.slot_label}>'

    @property
    def package_count(self):
        return self.packages.filter(
            IncomingPackage.status.in_(['已入库', '待打包'])
        ).count()


class IncomingPackage(db.Model):
    """入库明细记录表 - 记录所有入库包裹的详细信息"""
    __tablename__ = 'incoming_packages'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True, comment='客户ID（无主包裹为NULL）')
    entry_time = db.Column(db.DateTime, default=get_local_time, index=True, comment='入库时间')
    # 序号 - 该客户所有单号的排序，从1开始计数
    sequence_no = db.Column(db.Integer, default=1, comment='序号')
    # 扫描单号 - 首次扫描录入的快递单号，唯一不可重复
    tracking_number = db.Column(db.String(50), nullable=False, unique=True, index=True, comment='扫描单号')
    # 收货地 - 默认引用客户管理界面中的收货地址，可手动修改
    receiving_address = db.Column(db.String(200), comment='收货地')
    # 二次扫描单号 - 敏货二次扫描确认后的快递单号
    second_scan_number = db.Column(db.String(50), comment='二次扫描单号')
    # 是否敏货 - 根据二次扫描结果自动标记
    is_danger = db.Column(db.Boolean, default=False, comment='是否敏货')
    # 货物性质 - 普/敏，打包时从打包记录同步
    nature = db.Column(db.String(10), default='普', comment='货物性质：普/敏')
    # 货物状态 - 已入库/待打包/已打包
    status = db.Column(db.String(20), default='已入库', index=True, comment='货物状态：已入库→待打包→已打包')
    # 批次ID - 提前与批次关联，未关联时为 NULL
    batch_id = db.Column(db.Integer, db.ForeignKey('batches.id'), nullable=True, index=True, comment='批次ID')
    # 客户备注 - 客户的要求/特殊说明
    customer_remark = db.Column(db.Text, comment='客户备注')
    # 异常备注 - 入库操作中的异常情况，如"没找全"、"包裹破损"等
    abnormal_remark = db.Column(db.Text, comment='异常备注')
    # 备注（旧字段保留兼容）
    remark = db.Column(db.Text, comment='备注信息')
    # 无主包裹标识
    is_anonymous = db.Column(db.Boolean, default=False, comment='是否无主包裹')
    # 渠道发货方式 - 空运/快递/小包（快速标记渠道发货时记录）
    ship_channel = db.Column(db.String(50), comment='渠道发货方式：空运/快递/小包')
    # 发货时间 - 渠道发货/正常发货时记录
    shipped_at = db.Column(db.DateTime, comment='发货时间')
    # 内部单号 - 渠道发货时填写的内部/渠道单号（选填）
    internal_tracking_no = db.Column(db.String(100), comment='内部单号（渠道发货选填）')
    # 货位ID - 指定存放的货架位置
    shelf_slot_id = db.Column(db.Integer, db.ForeignKey('shelf_slots.id'), nullable=True, index=True, comment='货位ID')
    # 丢失单号字段
    lost_at = db.Column(db.DateTime, comment='丢失单号标记时间')
    lost_reason = db.Column(db.String(200), comment='丢失原因（如：快递丢失/客户拿走/破损/其他）')

    # 关系
    batch = db.relationship('Batch', backref='incoming_packages')

    def __repr__(self):
        return f'<IncomingPackage {self.tracking_number}>'


class Batch(db.Model):
    """批次表 - 管理发货批次，支持普货和敏货分别设置渠道和单号"""
    __tablename__ = 'batches'

    id = db.Column(db.Integer, primary_key=True)
    # 批次编号 - 格式如 P118（普货118批）
    batch_no = db.Column(db.String(20), unique=True, nullable=False, index=True, comment='批次编号')
    # 批次类型 - 普/敏
    batch_type = db.Column(db.String(10), nullable=False, comment='批次类型')
    # 邮编 - 批次的目的地邮编
    zip_code = db.Column(db.String(10), default='', comment='批次邮编')
    # === 普货渠道信息 ===
    # 发货渠道类型（普货）
    channel_type = db.Column(db.String(10), default='', comment='发货渠道类型（普/敏）- 旧字段保留兼容')
    # 普货发货渠道
    normal_channel = db.Column(db.String(100), default='', comment='普货发货渠道')
    # 普货派送渠道 - 如 DHL/UPS/FedEx 等
    normal_delivery = db.Column(db.String(100), default='', comment='普货派送渠道')
    # 普货单号
    normal_tracking = db.Column(db.String(100), default='', comment='普货单号')
    # === 敏货渠道信息 ===
    # 敏货发货渠道
    sensitive_channel = db.Column(db.String(100), default='', comment='敏货发货渠道')
    # 敏货派送渠道 - 如 DHL/UPS/FedEx 等
    sensitive_delivery = db.Column(db.String(100), default='', comment='敏货派送渠道')
    # 敏货单号
    sensitive_tracking = db.Column(db.String(100), default='', comment='敏货单号')
    # === 旧版兼容字段 ===
    # 派送渠道 - 旧字段保留兼容
    delivery_channel = db.Column(db.String(100), default='', comment='派送渠道（旧版）')
    # 单号 - 旧字段保留兼容
    tracking_number = db.Column(db.String(100), default='', comment='单号（旧版）')
    # 创建时间
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')
    # 是否已发货
    is_shipped = db.Column(db.Boolean, default=False, comment='已发货')
    # 是否已锁定
    is_locked = db.Column(db.Boolean, default=False, comment='已锁定（全部完成后封存）')
    lock_time = db.Column(db.DateTime, nullable=True, comment='批次锁定时间（即发货时间）')
    # 备注
    remark = db.Column(db.Text, comment='备注信息')
    # 物流状态 - 锁定后手动更新，记录货物发出后的轨迹（如已装柜/已上船/到港等）
    shipment_status = db.Column(db.Text, default='', comment='物流状态（已锁定后手动更新）')

    # 关系
    packed_packages = db.relationship('PackedPackage', backref='batch', lazy='dynamic')

    def get_channel_info(self, nature):
        """根据货物性质(普/敏)获取渠道信息"""
        if nature == '敏':
            return {
                'channel': self.sensitive_channel or '',
                'delivery': self.sensitive_delivery or '',
                'tracking': self.sensitive_tracking or ''
            }
        else:
            return {
                'channel': self.normal_channel or '',
                'delivery': self.normal_delivery or '',
                'tracking': self.normal_tracking or ''
            }

    def __repr__(self):
        return f'<Batch {self.batch_no}>'


class PackedPackage(db.Model):
    """打包记录表 - 记录打包操作"""
    __tablename__ = 'packed_packages'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True, comment='客户ID')
    batch_id = db.Column(db.Integer, db.ForeignKey('batches.id'), index=True, comment='批次ID')
    created_at = db.Column(db.DateTime, default=get_local_time, index=True, comment='创建时间')

    # 序号标签 - 如 2-1 表示共2个包裹的第1个
    serial_label = db.Column(db.String(20), nullable=False, comment='序号标签')
    total_packages = db.Column(db.Integer, default=1, comment='总包裹数')

    # 货物性质 - 普/敏
    nature = db.Column(db.String(10), nullable=False, comment='普货/敏货')

    # 标签是否已打印
    label_printed = db.Column(db.Boolean, default=False, comment='标签已打印')

    # 是否已合箱
    is_cartoned = db.Column(db.Boolean, default=False, comment='已合箱')

    # 货物状态 - 待合箱/已发货/已完成
    status = db.Column(db.String(20), default='待合箱', index=True, comment='货物状态：待合箱→已发货→已完成')

    # VIP 单独发货时间
    shipped_at = db.Column(db.DateTime, comment='VIP单独发货时间')

    # 备注（从入库包裹继承）
    customer_remark = db.Column(db.Text, comment='客户备注')
    abnormal_remark = db.Column(db.Text, comment='异常备注')

    # 发货明细
    contents_description = db.Column(db.Text, comment='包裹内容物')
    weight = db.Column(db.Float, default=0.0, comment='重量(kg)')
    length = db.Column(db.Float, default=0.0, comment='长度(cm)')
    width = db.Column(db.Float, default=0.0, comment='宽度(cm)')
    height = db.Column(db.Float, default=0.0, comment='高度(cm)')
    volume_weight = db.Column(db.Float, default=0.0, comment='体积重')

    # 拼单次客户名称（多个用、分隔）
    secondary_customer_names = db.Column(db.String(200), comment='次客户名称（多个用、分隔）')
    # VIP单独发货标记（batch_id=NULL + is_vip_solo=True 表示VIP单独包裹）
    is_vip_solo = db.Column(db.Boolean, default=False, index=True, comment='VIP单独发货')
    # 整箱标记（打包时勾选，表示该包裹为整箱，合箱时单独装箱不与其他客户混装）
    is_full_box = db.Column(db.Boolean, default=False, index=True, comment='整箱')

    # 关系 - 改为 lazy='select' 以支持 joinedload 预加载
    items = db.relationship('PackedPackageItem', backref='packed_package', lazy='select')

    def __repr__(self):
        return f'<PackedPackage {self.serial_label}>'


class PackedPackageItem(db.Model):
    """打包包裹明细表 - 关联入库包裹"""
    __tablename__ = 'packed_package_items'

    id = db.Column(db.Integer, primary_key=True)
    packed_package_id = db.Column(db.Integer, db.ForeignKey('packed_packages.id'), nullable=False, index=True, comment='打包ID')
    incoming_package_id = db.Column(db.Integer, db.ForeignKey('incoming_packages.id'), nullable=False, index=True, comment='入库记录ID')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')

    # 关系
    incoming_package = db.relationship('IncomingPackage', backref='packed_items')


class CartonGroup(db.Model):
    """合箱分组表 - 同一批次内按邮编等维度对箱子分组"""
    __tablename__ = 'carton_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, comment='组名（默认=邮编）')
    batch_id = db.Column(db.Integer, db.ForeignKey('batches.id'), nullable=False, index=True, comment='批次ID')
    zip_code = db.Column(db.String(10), nullable=True, comment='该组邮编')
    sort_order = db.Column(db.Integer, default=0, comment='排序')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')

    # 物流信息
    internal_tracking = db.Column(db.String(100), default='', comment='内部单号')
    transfer_tracking = db.Column(db.String(100), default='', comment='转单号')
    logistics_status = db.Column(db.Text, default='', comment='物流进度')

    # 关系
    cartons = db.relationship('Carton', backref='group', lazy='dynamic')
    batch = db.relationship('Batch', backref='carton_groups')

    def __repr__(self):
        return f'<CartonGroup {self.name}>'


class Carton(db.Model):
    """箱子信息表 - 合箱操作"""
    __tablename__ = 'cartons'

    id = db.Column(db.Integer, primary_key=True)
    # 箱子名称 - 自动生成，如 P120-1、M120-2
    name = db.Column(db.String(50), nullable=False, comment='箱子名称')
    # 箱子类型 - P(普)/M(敏)
    carton_type = db.Column(db.String(10), nullable=False, comment='箱型 P/M')
    # 所属批次
    batch_id = db.Column(db.Integer, db.ForeignKey('batches.id'), nullable=True, index=True, comment='批次ID')
    # 所属分组
    group_id = db.Column(db.Integer, db.ForeignKey('carton_groups.id'), nullable=True, index=True, comment='分组ID')
    # 批次内序号
    box_seq = db.Column(db.Integer, default=0, comment='批次内同类型序号')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')

    # 合箱后数据
    total_weight = db.Column(db.Float, default=0.0, comment='总重量(kg)')
    length = db.Column(db.Float, default=0.0, comment='长度(cm)')
    width = db.Column(db.Float, default=0.0, comment='宽度(cm)')
    height = db.Column(db.Float, default=0.0, comment='高度(cm)')
    volume_weight = db.Column(db.Float, default=0.0, comment='体积重')

    # 是否已发货
    is_shipped = db.Column(db.Boolean, default=False, comment='已发货')

    # 自定义邮编 - 手动修改后按新邮编编号，不修改则为NULL使用批次邮编
    custom_zip_code = db.Column(db.String(10), nullable=True, comment='自定义邮编')

    # 关系
    items = db.relationship('CartonItem', backref='carton', lazy='dynamic')
    batch = db.relationship('Batch', backref='cartons')

    def __repr__(self):
        return f'<Carton {self.name}>'


class CartonItem(db.Model):
    """箱子包裹明细表 - 关联打包包裹"""
    __tablename__ = 'carton_items'

    id = db.Column(db.Integer, primary_key=True)
    carton_id = db.Column(db.Integer, db.ForeignKey('cartons.id'), nullable=False, index=True, comment='箱子ID')
    packed_package_id = db.Column(db.Integer, db.ForeignKey('packed_packages.id'), nullable=False, index=True, comment='打包包裹ID')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')

    packed_package = db.relationship('PackedPackage', backref='carton_items')


class FreightRecord(db.Model):
    """运费记录表 - 记录客户运费明细"""
    __tablename__ = 'freight_records'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True, comment='客户ID')
    carton_id = db.Column(db.Integer, db.ForeignKey('cartons.id'), comment='箱子ID')
    # 运费金额
    amount = db.Column(db.Float, default=0.0, comment='运费金额')
    # 状态 - 待结算/已结算
    status = db.Column(db.String(20), default='待结算', index=True, comment='状态')
    # 创建时间
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')
    # 结算时间
    settled_at = db.Column(db.DateTime, comment='结算时间')

    # 关系
    customer = db.relationship('Customer', backref='freight_records')

    def __repr__(self):
        return f'<FreightRecord {self.id} - {self.amount}>'


class PriceConfig(db.Model):
    """单价配置表 - 存储普货和敏货的单价"""
    __tablename__ = 'price_configs'

    id = db.Column(db.Integer, primary_key=True)
    # 配置名称 - '普货单价' / '敏货单价'
    name = db.Column(db.String(20), unique=True, nullable=False, comment='配置名称')
    # 每公斤单价
    price_per_kg = db.Column(db.Float, default=0.0, comment='每公斤单价')
    # 更新时间
    updated_at = db.Column(db.DateTime, default=get_local_time, onupdate=get_local_time, comment='更新时间')


class OperationLog(db.Model):
    """操作日志表 - 记录关键操作审计"""
    __tablename__ = 'operation_logs'

    id = db.Column(db.Integer, primary_key=True)
    # 操作类型
    action = db.Column(db.String(50), nullable=False, index=True, comment='操作类型')
    # 操作内容
    content = db.Column(db.Text, comment='操作内容')
    # 操作人
    operator = db.Column(db.String(50), comment='操作人')
    # 关联目标类型和ID
    target_type = db.Column(db.String(50), comment='目标类型')
    target_id = db.Column(db.Integer, comment='目标ID')
    # 附加数据(JSON)
    extra_data = db.Column(db.Text, comment='附加数据')
    # 创建时间
    created_at = db.Column(db.DateTime, default=get_local_time, index=True, comment='创建时间')

    def __repr__(self):
        return f'<OperationLog {self.action} - {self.created_at}>'


def create_operation_log(action, content, operator=None, target_type=None, target_id=None, extra_data=None):
    """创建操作日志的辅助函数"""
    log = OperationLog(
        action=action,
        content=content,
        operator=operator,
        target_type=target_type,
        target_id=target_id,
        extra_data=extra_data
    )
    db.session.add(log)
    return log


class ProductCache(db.Model):
    """品名缓存表 - AI 补全的产品信息缓存，避免重复调用 API"""
    __tablename__ = 'product_cache'

    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(200), unique=True, nullable=False, index=True, comment='中文品名（唯一键）')
    product_en_name = db.Column(db.String(200), default='', comment='英文品名')
    material = db.Column(db.String(100), default='', comment='材质')
    material_en = db.Column(db.String(100), default='', comment='材质英文')
    use_age = db.Column(db.String(100), default='', comment='用途')
    use_age_en = db.Column(db.String(100), default='', comment='用途英文')
    hs_code = db.Column(db.String(20), default='', comment='海关编码')
    unit_price = db.Column(db.Integer, default=1, comment='申报单价')
    brand = db.Column(db.String(100), default='无', comment='品牌')
    model = db.Column(db.String(100), default='无', comment='型号')
    img_path = db.Column(db.String(500), default='', comment='匹配到的图片路径')
    created_at = db.Column(db.DateTime, default=get_local_time, comment='创建时间')
    updated_at = db.Column(db.DateTime, default=get_local_time, onupdate=get_local_time, comment='更新时间')

    def to_dict(self):
        return {
            'product_name': self.product_name,
            'product_en_name': self.product_en_name,
            'material': self.material,
            'material_en': self.material_en,
            'use_age': self.use_age,
            'use_age_en': self.use_age_en,
            'hs_code': self.hs_code,
            'unit_price': self.unit_price,
            'brand': self.brand,
            'model': self.model,
            'img_path': self.img_path,
        }


class ProductImage(db.Model):
    """产品图片索引表 - 从本地图片文件夹一次性扫描建索引"""
    __tablename__ = 'product_images'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False, index=True, comment='文件名（不含扩展名）')
    file_path = db.Column(db.String(500), nullable=False, comment='完整路径')
    file_ext = db.Column(db.String(10), default='', comment='扩展名')
    file_size = db.Column(db.Integer, default=0, comment='文件大小(bytes)')
    indexed_at = db.Column(db.DateTime, default=get_local_time, comment='索引时间')

    def __repr__(self):
        return f'<ProductImage {self.filename}>'


class InvoiceConfig(db.Model):
    """发票配置表 - 存储发票模板路径、图片文件夹等全局配置"""
    __tablename__ = 'invoice_config'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False, index=True, comment='配置键')
    value = db.Column(db.Text, default='', comment='配置值')
    updated_at = db.Column(db.DateTime, default=get_local_time, onupdate=get_local_time, comment='更新时间')

    @staticmethod
    def get(key, default=''):
        cfg = db.session.scalar(db.select(InvoiceConfig).filter_by(key=key))
        return cfg.value if cfg else default

    @staticmethod
    def set(key, value):
        cfg = db.session.scalar(db.select(InvoiceConfig).filter_by(key=key))
        if cfg:
            cfg.value = value
        else:
            cfg = InvoiceConfig(key=key, value=value)
            db.session.add(cfg)
        db.session.commit()
