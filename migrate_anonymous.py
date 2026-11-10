"""临时迁移脚本：让 customer_id 可为 NULL，添加 is_anonymous 字段"""
import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), 'consolidation.db')
if not os.path.exists(db_path):
    print('数据库不存在:', db_path)
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 检查 is_anonymous 字段
cursor.execute('PRAGMA table_info(incoming_packages)')
cols = [row[1] for row in cursor.fetchall()]
print('当前字段:', cols)

if 'is_anonymous' not in cols:
    cursor.execute('ALTER TABLE incoming_packages ADD COLUMN is_anonymous BOOLEAN DEFAULT 0')
    print('已添加 is_anonymous 字段')

# 检查 customer_id 是否 NOT NULL
cursor.execute("PRAGMA table_info(incoming_packages)")
for row in cursor.fetchall():
    if row[1] == 'customer_id':
        print(f'customer_id: nullable={not row[3]}')

# 重建表让 customer_id 可为 NULL
cursor.execute('''CREATE TABLE incoming_packages_new (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    entry_time DATETIME,
    sequence_no INTEGER DEFAULT 1,
    tracking_number VARCHAR(50) NOT NULL UNIQUE,
    receiving_address VARCHAR(200),
    second_scan_number VARCHAR(50),
    is_danger BOOLEAN DEFAULT 0,
    nature VARCHAR(10) DEFAULT '普',
    status VARCHAR(20) DEFAULT '已入库',
    batch_id INTEGER,
    customer_remark TEXT,
    abnormal_remark TEXT,
    remark TEXT,
    is_anonymous BOOLEAN DEFAULT 0,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(batch_id) REFERENCES batches(id)
)''')

cursor.execute('INSERT INTO incoming_packages_new SELECT * FROM incoming_packages')
cursor.execute('DROP TABLE incoming_packages')
cursor.execute('ALTER TABLE incoming_packages_new RENAME TO incoming_packages')

# 重建索引
cursor.execute('CREATE INDEX IF NOT EXISTS ix_incoming_packages_customer_id ON incoming_packages(customer_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS ix_incoming_packages_entry_time ON incoming_packages(entry_time)')
cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_incoming_packages_tracking_number ON incoming_packages(tracking_number)')
cursor.execute('CREATE INDEX IF NOT EXISTS ix_incoming_packages_status ON incoming_packages(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS ix_incoming_packages_batch_id ON incoming_packages(batch_id)')

conn.commit()
print('Done: customer_id 已改为 nullable')
conn.close()
