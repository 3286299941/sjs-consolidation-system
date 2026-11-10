# -*- coding: utf-8 -*-
"""
集运管理系统 - 本地备份脚本
备份范围：数据库 + 上传文件 + 导出文件
用法：
    python backup.py              # 执行备份
    python backup.py --list       # 查看现有备份
    python backup.py --clean 7    # 清理7天前的旧备份
"""
import os
import sys
import shutil
import argparse
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

# 需要备份的内容
BACKUP_ITEMS = {
    'consolidation.db': 'db',
    'uploads':            'uploads',
}

# 可选：也备份导出文件（如果有 exports/ 目录）
EXPORT_DIR = os.path.join(BASE_DIR, 'exports')
if os.path.isdir(EXPORT_DIR):
    BACKUP_ITEMS['exports'] = 'exports'


def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def do_backup():
    """执行备份"""
    ensure_backup_dir()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f'backup_{timestamp}'
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    os.makedirs(backup_path, exist_ok=True)

    copied = []
    failed = []

    for item_name, subdir in BACKUP_ITEMS.items():
        src = os.path.join(BASE_DIR, item_name)
        if not os.path.exists(src):
            print(f'  [跳过] {item_name} (不存在)')
            continue

        try:
            dst = os.path.join(backup_path, item_name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            copied.append(item_name)
            # 显示大小
            size = _get_size(dst)
            print(f'  [已备份] {item_name} ({_format_size(size)})')
        except Exception as e:
            failed.append((item_name, str(e)))
            print(f'  [失败] {item_name}: {e}')

    # 在备份目录中记录信息
    info_path = os.path.join(backup_path, 'backup_info.txt')
    with open(info_path, 'w', encoding='utf-8') as f:
        f.write(f'备份时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'备份内容: {", ".join(copied)}\n')
        if failed:
            f.write(f'失败项: {", ".join(f[0] for f in failed)}\n')

    print(f'\n备份完成: {backup_name}')
    print(f'存放路径: {backup_path}')

    return backup_name, len(copied), len(failed)


def list_backups():
    """列出所有备份"""
    ensure_backup_dir()
    items = sorted(os.listdir(BACKUP_DIR), reverse=True)
    if not items:
        print('暂无备份')
        return

    print(f'现有备份 ({len(items)} 个):\n')
    for name in items:
        full = os.path.join(BACKUP_DIR, name)
        if not os.path.isdir(full):
            continue
        size = _get_size(full)
        # 解析时间
        try:
            ts = datetime.strptime(name.replace('backup_', ''), '%Y%m%d_%H%M%S')
            time_str = ts.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            time_str = name
        print(f'  {time_str}  {_format_size(size)}')


def clean_old_backups(days=7):
    """清理 N 天前的旧备份"""
    ensure_backup_dir()
    cutoff = datetime.now().timestamp() - days * 86400
    cleaned = 0

    for name in os.listdir(BACKUP_DIR):
        full = os.path.join(BACKUP_DIR, name)
        if not os.path.isdir(full):
            continue
        try:
            ts = datetime.strptime(name.replace('backup_', ''), '%Y%m%d_%H%M%S')
            if ts.timestamp() < cutoff:
                shutil.rmtree(full)
                print(f'  [已清理] {name}')
                cleaned += 1
        except ValueError:
            pass

    print(f'\n清理完成: {cleaned} 个旧备份已删除')


def _get_size(path):
    """计算文件/目录大小"""
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def _format_size(bytes_val):
    if bytes_val < 1024:
        return f'{bytes_val} B'
    elif bytes_val < 1024 * 1024:
        return f'{bytes_val / 1024:.1f} KB'
    else:
        return f'{bytes_val / (1024 * 1024):.1f} MB'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='集运管理系统 - 本地备份工具')
    parser.add_argument('--list', action='store_true', help='查看现有备份')
    parser.add_argument('--clean', type=int, metavar='DAYS', help='清理N天前的旧备份')
    args = parser.parse_args()

    if args.list:
        list_backups()
    elif args.clean:
        clean_old_backups(args.clean)
    else:
        print('=' * 50)
        print(f'  集运管理系统 - 本地备份')
        print(f'  时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        print('=' * 50)
        do_backup()
