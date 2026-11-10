# -*- coding: utf-8 -*-
"""
发票生成引擎 - 从数据库导出装货清单+出货信息表 → 生成普货/敏货发票Excel
图片从本地文件夹读取，品名信息优先查 product_cache，未命中调 DeepSeek API
"""
import os
import re
import io
import json
import time
import random
import requests
import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, Border, Side, Alignment
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
import tempfile
from collections import defaultdict, Counter
from datetime import datetime
from fuzzywuzzy import fuzz

# ==================== 业务常量 ====================
PUHUO_START_ROW = 31
MINHUO_START_ROW = 18

PUHUO_FIXED_VALUES = {"A": "是", "B": "普船海派", "D": "纸箱"}
PUHUO_DB_TO_TEMPLATE = {
    "product_name": "E", "product_en_name": "F",
    "material": "G", "material_en": "H",
    "use_age": "I", "use_age_en": "J",
    "hs_code": "K", "brand": "L",
    "model": "M", "total_pieces": "N",
    "unit_price": "O",
}
PUHUO_LLM_FIELDS = ["material", "material_en", "use_age", "use_age_en", "unit_price"]
PUHUO_IMAGE_COL = "Y"

TEMPLATE_DIR = '/opt/invoice_templates'
IMAGE_DIR = '/opt/images'

MINHUO_FIXED_VALUES = {"B": 1}
MINHUO_DB_TO_TEMPLATE = {
    "product_name": "H", "product_en_name": "G",
    "unit_price": "I", "material": "K",
    "use_age": "M", "hs_code": "L",
    "brand": "N", "model": "O",
}
MINHUO_LLM_FIELDS = ["unit_price", "material", "use_age"]
MINHUO_IMAGE_COL = "P"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


def col_letter_to_num(letter):
    """A→1, Z→26, AA→27"""
    val = 0
    for c in letter.upper():
        val = val * 26 + (ord(c) - 64)
    return val


def set_cell_style(cell, size=12):
    """设置单元格字体、对齐、边框 — 统一黑体12号居中"""
    cell.font = Font(name='微软雅黑', size=size)
    cell.alignment = Alignment(horizontal='center', vertical='center')
    thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                  top=Side(style='thin'), bottom=Side(style='thin'))
    cell.border = thin


def call_deepseek(prompt, api_key, max_tokens=200, temperature=0):
    """调用 DeepSeek API"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
               "temperature": temperature, "max_tokens": max_tokens}
    for attempt in range(3):
        try:
            resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    return ""


def batch_fill_fields(product_names, api_key):
    """批量调用 DeepSeek 补全品名信息，返回 {品名: {字段: 值}}"""
    if not product_names:
        return {}

    prompt = f"""以下是一批跨境电商商品的中文品名。请为每个品名补全以下信息（用英文逗号分隔）：
材质|材质英文|用途|用途英文|单价(1-10整数)

每个品名一行，格式：
品名: 材质|材质英文|用途|用途英文|单价

示例：
塑料收纳盒: 塑料|plastic|收纳|storage|3
陶瓷杯子: 陶瓷|ceramic|饮用|drinking|5

以下是品名列表：
""" + "\n".join(product_names) + "\n\n请按格式返回："

    result = call_deepseek(prompt, api_key, max_tokens=2000)
    if not result:
        return {}

    filled = {}
    for line in result.strip().split('\n'):
        if ':' not in line or '|' not in line:
            continue
        name_part, info_part = line.split(':', 1)
        name = name_part.strip()
        parts = info_part.strip().split('|')
        if len(parts) >= 5:
            filled[name] = {
                "material": parts[0].strip()[:20],
                "material_en": parts[1].strip()[:30],
                "use_age": parts[2].strip()[:20],
                "use_age_en": parts[3].strip()[:30],
                "unit_price": max(1, min(10, int(re.findall(r'\d+', parts[4])[0]))) if re.findall(r'\d+', parts[4]) else 1,
            }
    return filled


def batch_translate(names, api_key):
    """批量翻译品名到英文"""
    if not names:
        return {}
    prompt = "将以下中文翻译成英文，一行一个，只输出英文：\n" + "\n".join(names)
    result = call_deepseek(prompt, api_key, max_tokens=1500)
    if not result:
        return {n: "Item" for n in names}
    lines = result.strip().split('\n')
    return {names[i]: lines[i].strip() for i in range(min(len(names), len(lines)))}


def batch_hs_code(names, api_key):
    """批量查询HS编码"""
    if not names:
        return {}
    prompt = "查询以下商品的中国海关HS编码（10位），每个一行，格式「品名: 编码」：\n" + "\n".join(names)
    result = call_deepseek(prompt, api_key, max_tokens=1500)
    codes = {}
    if result:
        for line in result.strip().split('\n'):
            if ':' in line:
                name, code = line.split(':', 1)
                # 去掉品名前后的 markdown 加粗、列表符号、空格
                name = re.sub(r'^[\s\-*•●\d\.\)]+', '', name).strip().strip('**').strip()
                code = code.strip().strip('**').strip()
                match = re.search(r'\d{10}', code)
                if match:
                    codes[name] = match.group(0)
    return codes


def find_image_sql(product_name, image_folder, seen_images=None):
    """通过 SQL 查图片索引 + fuzzywuzzy 匹配（高分候选中随机选择，不重复）"""
    if not image_folder or not os.path.exists(image_folder):
        return None

    from app import db
    from models import ProductImage

    product_clean = re.sub(r'[\s\u3000（）()]+', '', product_name).lower()
    if len(product_clean) < 1:
        return None

    # SQL 模糊查候选
    like_parts = product_clean.split()
    query = db.select(ProductImage)
    for part in like_parts:
        if len(part) >= 1:
            query = query.filter(ProductImage.filename.like(f'%{part}%'))
    candidates = db.session.scalars(query.limit(50)).all()

    if not candidates:
        return None

    # fuzzywuzzy 打分，收集所有 >= 80 分的候选
    # 中文用 partial_ratio（子串匹配），比 token_set_ratio 更准
    import random
    scored = []
    for img in candidates:
        file_clean = re.sub(r'[\s\u3000]+', '', img.filename).lower()
        score = max(
            fuzz.partial_ratio(product_clean, file_clean),
            fuzz.token_set_ratio(product_clean, file_clean),
        )
        if score >= 80:
            full_path = img.file_path
            if os.path.exists(full_path):
                scored.append((score, full_path))

    if not scored:
        return None

    # 排除本次已用过的图片
    if seen_images:
        available = [(s, p) for s, p in scored if p not in seen_images]
    else:
        available = scored

    # 如果全部都用过了，重置（再次随机）
    if not available:
        available = scored

    # 随机选一个（高分优先，低分偶有惊喜）
    # 按分数倒序，取前80%候选，随机挑一个
    available.sort(key=lambda x: x[0], reverse=True)
    pool_size = max(1, len(available) * 3 // 4)  # 取前75%高分
    pool = available[:pool_size]
    best_path = random.choice(pool)[1]
    return best_path


def download_product_image(product_name, image_folder, api_key):
    """当本地匹配不到图片时，从网络搜索并下载产品图片"""
    import hashlib

    if not image_folder or not os.path.isdir(image_folder):
        return None
    if not api_key:
        return None

    # 文件名：MD5(品名) + .jpg
    safe_name = hashlib.md5(product_name.encode('utf-8')).hexdigest()[:12]
    save_path = os.path.join(image_folder, f"{safe_name}.jpg")
    if os.path.exists(save_path):
        return save_path

    # 本地匹配没找到就跳过，不自动下载
    if not api_key:
        return None
    # 无本地图也无 API key，跳过
    return None


def insert_image_to_cell(ws, cell_coord, image_path, temp_files, size=180):
    """插入正方形图片到单元格，非正方形图片居中裁剪或padding"""
    try:
        pil_img = PILImage.open(image_path)
        pil_img = pil_img.convert('RGB') if pil_img.mode in ('RGBA', 'P', 'LA') else pil_img
        orig_w, orig_h = pil_img.size

        # 缩放到正方形：取长边缩放后居中裁剪
        if orig_w > orig_h:
            new_h = int(orig_h * size / orig_w)
            pil_img = pil_img.resize((size, new_h), PILImage.LANCZOS)
            # 上下padding
            square = PILImage.new('RGB', (size, size), (255, 255, 255))
            square.paste(pil_img, (0, (size - new_h) // 2))
        elif orig_h > orig_w:
            new_w = int(orig_w * size / orig_h)
            pil_img = pil_img.resize((new_w, size), PILImage.LANCZOS)
            # 左右padding
            square = PILImage.new('RGB', (size, size), (255, 255, 255))
            square.paste(pil_img, ((size - new_w) // 2, 0))
        else:
            square = pil_img.resize((size, size), PILImage.LANCZOS)

        fd, tmp_path = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        square.save(tmp_path, 'PNG')
        temp_files.append(tmp_path)

        img = Image(tmp_path)
        img.width = size
        img.height = size

        # 图片嵌入单元格（TwoCellAnchor + editAs="twoCell" 随单元格缩放）
        col_letter = re.match(r'([A-Z]+)', cell_coord).group(1)
        row_num = int(re.search(r'(\d+)', cell_coord).group(1))
        col_idx = col_letter_to_num(col_letter) - 1  # 0-based
        row_idx = row_num - 1                          # 0-based

        from_m = AnchorMarker(col=col_idx, colOff=0, row=row_idx, rowOff=0)
        to_m = AnchorMarker(col=col_idx + 1, colOff=0, row=row_idx + 1, rowOff=0)
        img.anchor = TwoCellAnchor(editAs="twoCell", _from=from_m, to=to_m)

        ws.add_image(img)
        ws.column_dimensions[col_letter].width = size // 7
    except Exception as e:
        print(f"插入图片失败 {image_path}: {e}")


def generate_invoice(boxes, cargo_dict, template_file, output_file, invoice_type,
                     image_folder, api_key, progress_callback=None):
    """
    生成发票 Excel
    boxes: 出货信息 [{box_no, type, weight, length, width, height, owners}]
    cargo_dict: {客户名: {普货: [品名], 敏货: [品名]}}
    """
    from app import db
    from models import ProductCache

    if invoice_type == 'puhuo':
        start_row = PUHUO_START_ROW
        fixed = PUHUO_FIXED_VALUES
        db_map = PUHUO_DB_TO_TEMPLATE
        img_col = PUHUO_IMAGE_COL
    else:
        start_row = MINHUO_START_ROW
        fixed = MINHUO_FIXED_VALUES
        db_map = MINHUO_DB_TO_TEMPLATE
        img_col = MINHUO_IMAGE_COL

    wb = load_workbook(template_file)
    ws = wb.active
    current_row = start_row
    merge_ranges = []
    temp_files = []

    # 收集所有品名
    all_products = set()
    for box in boxes:
        if (invoice_type == 'puhuo' and box.get("type") != "普货") or \
           (invoice_type == 'minhuo' and box.get("type") != "敏货"):
            continue
        for owner in box.get("owners", []):
            if owner in cargo_dict:
                all_products.update(cargo_dict[owner].get("普货" if invoice_type == 'puhuo' else "敏货", []))

    # 批量查缓存
    cache_map = {}
    if all_products:
        rows = db.session.scalars(
            db.select(ProductCache).filter(ProductCache.product_name.in_(list(all_products)))
        ).all()
        cache_map = {r.product_name: r.to_dict() for r in rows}

    # 找出未缓存的品名
    uncached = [p for p in all_products if p not in cache_map]
    if progress_callback:
        progress_callback(f"  品名: 共 {len(all_products)} 个, 缓存命中 {len(all_products) - len(uncached)}, 需AI补全 {len(uncached)}")

    new_cache = {}
    if uncached and api_key:
        if progress_callback:
            progress_callback(f"  🤖 调用 DeepSeek API 补全品名信息...")
        # 分批次（每次最多30个）
        batch_size = 30
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i:i + batch_size]
            batch_no = i // batch_size + 1
            total_batches = (len(uncached) - 1) // batch_size + 1
            if progress_callback:
                progress_callback(f"    API 批次 {batch_no}/{total_batches} ({len(batch)} 个品名)...")
            filled = batch_fill_fields(batch, api_key)
            translated = batch_translate(batch, api_key)
            hs_codes = batch_hs_code(batch, api_key)

            for p in batch:
                info = filled.get(p, {})
                entry = {
                    "product_name": p,
                    "product_en_name": translated.get(p, "Item"),
                    "material": info.get("material", "塑料"),
                    "material_en": info.get("material_en", "plastic"),
                    "use_age": info.get("use_age", "使用"),
                    "use_age_en": info.get("use_age_en", "use"),
                    "hs_code": hs_codes.get(p, ""),
                    "unit_price": info.get("unit_price", 1),
                    "brand": "无",
                    "model": "无",
                    "img_path": "",
                }
                new_cache[p] = entry
                cache_map[p] = entry
            time.sleep(0.3)

        # 写入缓存表
        for p, entry in new_cache.items():
            pc = ProductCache(**entry)
            db.session.add(pc)
        db.session.commit()

    # 补全已缓存但缺少HS编码的品名
    missing_hs = [p for p, info in cache_map.items() if not info.get("hs_code")]
    if missing_hs and api_key:
        if progress_callback:
            progress_callback(f"  🔍 补全 {len(missing_hs)} 个品名的海关编码...")
        hs_codes = batch_hs_code(missing_hs, api_key)
        updated = 0
        for p, code in hs_codes.items():
            if code:
                cache_map[p]["hs_code"] = code
                # 更新缓存表
                pc = db.session.scalar(db.select(ProductCache).filter_by(product_name=p))
                if pc:
                    pc.hs_code = code
                    updated += 1
        if updated:
            db.session.commit()
            if progress_callback:
                progress_callback(f"    已补全 {updated} 个编码")

    # 遍历箱子填数据
    if progress_callback:
        progress_callback(f"  填充 Excel 数据 ({len(boxes)} 箱), 匹配图片中...")
    total_boxes = 0
    seen_images = set()  # 本次发票已使用的图片路径，避免高频品名重复
    for box_idx, box in enumerate(boxes):
        if (invoice_type == 'puhuo' and box.get("type") != "普货") or \
           (invoice_type == 'minhuo' and box.get("type") != "敏货"):
            continue

        # 优先使用 box 自带的 products（VIP发票），否则从 cargo_dict 查
        goods_list = box.get("products", [])
        if not goods_list:
            for owner in box.get("owners", []):
                if owner in cargo_dict:
                    goods_list.extend(cargo_dict[owner].get("普货" if invoice_type == 'puhuo' else "敏货", []))

        unique = list(dict.fromkeys(goods_list))  # 去重保持顺序
        if not unique:
            continue

        total_boxes += 1
        if progress_callback and total_boxes % 5 == 0:
            progress_callback(f"    已处理 {total_boxes} 箱...")
        row_count = len(unique)
        start = current_row
        end = start + row_count - 1

        for idx, product_name in enumerate(unique):
            row = start + idx
            info = cache_map.get(product_name, {})

            # 固定值
            for col_letter, val in fixed.items():
                ws.cell(row, col_letter_to_num(col_letter), value=val)

            # 品名信息
            product_en = info.get("product_en_name", "Item")
            ws.cell(row, col_letter_to_num(db_map.get("product_name", "E")), value=product_name)
            ws.cell(row, col_letter_to_num(db_map.get("product_en_name", "F")), value=product_en)
            ws.cell(row, col_letter_to_num(db_map.get("material", "G")), value=info.get("material", "塑料"))
            if invoice_type == 'puhuo':
                ws.cell(row, col_letter_to_num(db_map.get("material_en", "H")), value=info.get("material_en", "plastic"))
            ws.cell(row, col_letter_to_num(db_map.get("use_age", "I")), value=info.get("use_age", "使用"))
            if invoice_type == 'puhuo':
                ws.cell(row, col_letter_to_num(db_map.get("use_age_en", "J")), value=info.get("use_age_en", "use"))
            ws.cell(row, col_letter_to_num(db_map.get("hs_code", "K")), value=info.get("hs_code", ""))
            ws.cell(row, col_letter_to_num(db_map.get("brand", "L")), value=info.get("brand", "无"))
            ws.cell(row, col_letter_to_num(db_map.get("model", "M")), value=info.get("model", "无"))
            ws.cell(row, col_letter_to_num(db_map.get("unit_price", "O")), value=info.get("unit_price", 1))
            if invoice_type == 'puhuo':
                ws.cell(row, col_letter_to_num(db_map.get("total_pieces", "N")), value=1)
            else:
                ws.cell(row, col_letter_to_num("J"), value=1)  # 敏货申报数量

            # 箱子的重量尺寸
            if invoice_type == 'puhuo':
                ws.cell(row, col_letter_to_num("R"), value=box.get("weight", 0))
                ws.cell(row, col_letter_to_num("S"), value=box.get("length", 0))
                ws.cell(row, col_letter_to_num("T"), value=box.get("width", 0))
                ws.cell(row, col_letter_to_num("U"), value=box.get("height", 0))
            else:
                ws.cell(row, col_letter_to_num("C"), value=box.get("weight", 0))
                ws.cell(row, col_letter_to_num("D"), value=box.get("length", 0))
                ws.cell(row, col_letter_to_num("E"), value=box.get("width", 0))
                ws.cell(row, col_letter_to_num("F"), value=box.get("height", 0))
                ws.cell(row, col_letter_to_num("A"), value=f"E-{total_boxes}")

            # 图片匹配（本地随机选择 → 自动下载）
            if image_folder:
                img_path = info.get("img_path", "")
                # 一律从本地候选池随机选（跳过缓存避免重复）
                new_path = find_image_sql(product_name, image_folder, seen_images)
                if new_path:
                    seen_images.add(new_path)
                    if new_path != img_path:
                        pc = db.session.scalar(db.select(ProductCache).filter_by(product_name=product_name))
                        if pc:
                            pc.img_path = new_path
                            db.session.commit()
                    if progress_callback:
                        label = os.path.basename(new_path)
                        progress_callback(f"    🖼️ 匹配: {product_name} → {label}")
                    img_path = new_path
                    print(f"[图片匹配] {product_name} -> {label}")
                elif img_path and os.path.exists(img_path):
                    seen_images.add(img_path)
                else:
                    print(f"[图片匹配] {product_name}: 本地未命中，seen={len(seen_images)}, folder={image_folder}")
                # 本地没找到，跳过（图片需手动上传到 /opt/images）
                if not img_path and progress_callback:
                    pass  # 静默，不占位、不下载

                if img_path and os.path.exists(img_path):
                    insert_image_to_cell(ws, f"{img_col}{row}", img_path, temp_files, size=180)
                elif progress_callback and not api_key:
                    pass  # 无API key，无法下载，静默

            # 行样式（箱号列）
            if invoice_type == 'puhuo':
                ws.cell(row, col_letter_to_num("C"), value=1)  # 箱号列

            if progress_callback and idx % 20 == 0:
                progress_callback(f"    填写行 {idx + 1}/{row_count} (第 {total_boxes} 箱)")

        # 合并单元格（敏货不合并货箱编号，每行独立显示）
        if invoice_type == 'puhuo' and row_count > 1:
            for col in ["C", "R", "S", "T", "U"]:
                merge_ranges.append((start, end, col))

        current_row = end + 1

    # 执行合并
    for s, e, col in merge_ranges:
        ws.merge_cells(f"{col}{s}:{col}{e}")
        set_cell_style(ws.cell(s, col_letter_to_num(col)))

    # 设置行高列宽
    end_col = 25 if invoice_type == 'puhuo' else 16
    for col in range(1, end_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    for row in range(start_row, current_row):
        ws.row_dimensions[row].height = 45

    # 统一数据行字体为黑体12号居中（所有填入内容的单元格）
    for row in range(start_row, current_row):
        for col_idx in range(1, end_col + 1):
            cell = ws.cell(row, col_idx)
            if cell.value is not None:
                cell.font = Font(name='微软雅黑', size=12)
                cell.alignment = Alignment(horizontal='center', vertical='center')

    # 普货：填写 B22(总体积) B23(总重量) B24(高频品名)
    if invoice_type == 'puhuo' and total_boxes > 0:
        from models import ProductCache
        # 总体积 + 总重量
        total_vol = total_wt = 0
        for box in boxes:
            if box.get("type") == "普货":
                total_wt += float(box.get("weight", 0))
                total_vol += float(box.get("length", 0)) * float(box.get("width", 0)) * float(box.get("height", 0)) / 1_000_000
        for r, val in [(22, round(total_vol, 3)), (23, round(total_wt, 1)), (24, "")]:
            cell = ws.cell(r, 2)
            if val:
                cell.value = val
            cell.font = Font(name='微软雅黑', size=12)
            cell.alignment = Alignment(horizontal='center', vertical='center')
            thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                          top=Side(style='thin'), bottom=Side(style='thin'))
            cell.border = thin

    # 全边框
    for row in ws.iter_rows():
        for cell in row:
            thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                          top=Side(style='thin'), bottom=Side(style='thin'))
            cell.border = thin

    wb.save(output_file)
    if progress_callback:
        progress_callback(f"  保存完成: {os.path.basename(output_file)} ({total_boxes} 箱)")
    for tmp in temp_files:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return output_file


def _find_template_by_zip(zip_code, invoice_type, template_dir):
    """按邮编自动匹配发票模板，匹配不上回退到 91755"""
    if not template_dir or not os.path.isdir(template_dir):
        return None
    prefix = "敏货" if invoice_type == 'minhuo' else "普货"
    # 如果指定了邮编，优先精确匹配
    if zip_code:
        exact = os.path.join(template_dir, f"{zip_code}{prefix}发票模板.xlsx")
        if os.path.isfile(exact):
            return exact
        for fn in os.listdir(template_dir):
            if fn.startswith(str(zip_code)) and prefix in fn and fn.endswith('.xlsx'):
                return os.path.join(template_dir, fn)
    # 回退到 91755
    fallback = os.path.join(template_dir, f"91755{prefix}发票模板.xlsx")
    if os.path.isfile(fallback):
        return fallback
    return None


def generate_all_invoices(batch_id, image_folder, api_key, output_dir,
                          progress_callback=None, invoice_type='all'):
    """从数据库生成出货信息表 + 装货清单 → 生成发票，模板按邮编自动匹配
    invoice_type: all | puhuo | minhuo"""
    from app import app, db
    from models import Batch, Carton, CartonItem, PackedPackage, Customer, AddressBook
    from sqlalchemy.orm import joinedload

    with app.app_context():
        batch = db.session.get(Batch, batch_id)
        if not batch:
            raise ValueError(f"批次 {batch_id} 不存在")

        if progress_callback:
            progress_callback(f"📦 开始处理批次 {batch.batch_no}")

        zip_code = batch.zip_code or ''
        puhuo_template = _find_template_by_zip(zip_code, 'puhuo', TEMPLATE_DIR)
        minhuo_template = _find_template_by_zip(zip_code, 'minhuo', TEMPLATE_DIR)

        if not puhuo_template and not minhuo_template:
            raise ValueError(f"未找到发票模板，请在 {TEMPLATE_DIR} 下放置 {zip_code or '91755'}普货/敏货发票模板.xlsx")

        if progress_callback:
            if puhuo_template:
                progress_callback(f"  模板匹配: {os.path.basename(puhuo_template)}（邮编 {zip_code or '91755'}）")
            if minhuo_template:
                progress_callback(f"  模板匹配: {os.path.basename(minhuo_template)}（邮编 {zip_code or '91755'}）")

        # 1. 读取箱子信息
        if progress_callback:
            progress_callback(f"读取箱子信息...")
        cartons = db.session.scalars(
            db.select(Carton).options(
                joinedload(Carton.batch)
            ).filter_by(batch_id=batch_id).order_by(Carton.box_seq)
        ).all()

        if progress_callback:
            progress_callback(f"  共 {len(cartons)} 个箱子")

        # 收集箱子信息
        boxes = []
        for idx, c in enumerate(cartons):
            if progress_callback and (idx % 10 == 0 or idx == len(cartons) - 1):
                progress_callback(f"  解析箱子 {idx + 1}/{len(cartons)}...")
            items = db.session.execute(
                db.select(CartonItem.packed_package_id)
                .filter_by(carton_id=c.id)
            ).all()
            pkg_ids = [r[0] for r in items]
            owners = set()
            if pkg_ids:
                owner_rows = db.session.execute(
                    db.select(Customer.name)
                    .join(PackedPackage, PackedPackage.customer_id == Customer.id)
                    .filter(PackedPackage.id.in_(pkg_ids))
                ).all()
                owners = [r[0] for r in owner_rows]

            boxes.append({
                "box_no": c.name,
                "type": "敏货" if c.carton_type == '敏' else "普货",
                "weight": c.total_weight or 0,
                "length": c.length or 0,
                "width": c.width or 0,
                "height": c.height or 0,
                "owners": list(owners),
            })

        # 2. 收集装货清单（客户→品名）
        if progress_callback:
            progress_callback(f"收集装货清单...")
        cargo_dict = defaultdict(lambda: {"普货": [], "敏货": []})
        for idx, c in enumerate(cartons):
            items = db.session.execute(
                db.select(CartonItem.packed_package_id).filter_by(carton_id=c.id)
            ).all()
            pkg_ids = [r[0] for r in items]
            if not pkg_ids:
                continue

            # 查打包记录→品名
            rows = db.session.execute(
                db.select(Customer.name, PackedPackage.contents_description, PackedPackage.nature)
                .join(Customer, Customer.id == PackedPackage.customer_id)
                .filter(PackedPackage.id.in_(pkg_ids))
            ).all()

            for cust_name, contents, nature in rows:
                if not contents:
                    continue
                items_list = re.split(r'[\s\u3000]+', str(contents).strip())
                nature_key = "敏货" if nature == '敏' else "普货"
                for item in items_list:
                    if item and item not in cargo_dict[cust_name][nature_key]:
                        # 扩展衣服→上衣/裤子/裙子
                        if item in ["衣服", "服装"]:
                            for sub in ["上衣", "裤子", "裙子"]:
                                if sub not in cargo_dict[cust_name][nature_key]:
                                    cargo_dict[cust_name][nature_key].append(sub)
                        else:
                            cargo_dict[cust_name][nature_key].append(item)

        # 转成普通 dict
        cargo_dict_simple = {k: dict(v) for k, v in cargo_dict.items()}

        if progress_callback:
            progress_callback(f"  已收集 {len(cargo_dict)} 个货主信息")

        puhuo_boxes = [b for b in boxes if b["type"] == "普货"]
        minhuo_boxes = [b for b in boxes if b["type"] == "敏货"]

        if progress_callback:
            progress_callback(f"📊 汇总: 普货 {len(puhuo_boxes)} 箱, 敏货 {len(minhuo_boxes)} 箱")

        today = datetime.now().strftime("%m月%d日")
        results = []

        # 查找地址库（按邮编匹配，一条记录同时含普/敏货信息）
        ab_entry = db.session.scalar(
            db.select(AddressBook).filter_by(zip_code=zip_code)
        )
        if progress_callback and ab_entry:
            progress_callback(f"📋 地址库匹配: {zip_code} → 普:{ab_entry.puhuo_consignee} 敏:{ab_entry.minhuo_consignee}")

        if invoice_type in ('all', 'puhuo') and puhuo_boxes and puhuo_template and os.path.exists(puhuo_template):
            filename = f"{batch.batch_no}-普货-{len(puhuo_boxes)}箱-{today}.xlsx"
            output = os.path.join(output_dir, filename)
            if progress_callback:
                progress_callback(f"📝 生成普货发票 ({len(puhuo_boxes)} 箱)...")
            generate_invoice(
                puhuo_boxes, cargo_dict_simple, puhuo_template, output, "puhuo",
                image_folder, api_key, progress_callback
            )
            _fill_invoice_header(output, ab_entry, "puhuo", box_count=len(puhuo_boxes))
            results.append(output)
            if progress_callback:
                progress_callback(f"  ✅ 普货发票已完成: {filename}")

        if invoice_type in ('all', 'minhuo') and minhuo_boxes and minhuo_template and os.path.exists(minhuo_template):
            filename = f"{batch.batch_no}-敏货-{len(minhuo_boxes)}箱-{today}.xlsx"
            output = os.path.join(output_dir, filename)
            if progress_callback:
                progress_callback(f"📝 生成敏货发票 ({len(minhuo_boxes)} 箱)...")
            generate_invoice(
                minhuo_boxes, cargo_dict_simple, minhuo_template, output, "minhuo",
                image_folder, api_key, progress_callback
            )
            _fill_invoice_header(output, ab_entry, "minhuo")
            results.append(output)
            if progress_callback:
                progress_callback(f"  ✅ 敏货发票已完成: {filename}")

        if progress_callback:
            progress_callback(f"🎉 全部完成！共生成 {len(results)} 个发票文件")
        return results


def _fill_invoice_header(output_file, ab_entry, invoice_type, box_count=0):
    """根据地址库填充发票抬头信息（普货用 puhuo_*, 敏货用 minhuo_*，缺则互相回退）"""
    if not ab_entry or not os.path.exists(output_file):
        return
    wb = load_workbook(output_file)
    ws = wb.active

    if invoice_type == 'puhuo':
        consignee = ab_entry.puhuo_consignee or ab_entry.minhuo_consignee
        phone = ab_entry.puhuo_phone or ab_entry.minhuo_phone
        address = ab_entry.puhuo_address or ab_entry.minhuo_address
        if consignee:
            ws.cell(11, 2).value = consignee
        if phone:
            ws.cell(12, 2).value = phone
        if ab_entry.zip_code:
            ws.cell(15, 2).value = ab_entry.zip_code
        if address:
            ws.cell(16, 2).value = address
        if ab_entry.country:
            ws.cell(7, 2).value = ab_entry.country
        if box_count > 0:
            ws.cell(21, 2).value = box_count
    elif invoice_type == 'minhuo':
        consignee = ab_entry.minhuo_consignee or ab_entry.puhuo_consignee
        phone = ab_entry.minhuo_phone or ab_entry.puhuo_phone
        address = ab_entry.minhuo_address or ab_entry.puhuo_address
        if ab_entry.zip_code:
            ws.cell(3, 2).value = ab_entry.zip_code
        if consignee:
            ws.cell(4, 2).value = consignee
        if address:
            ws.cell(6, 2).value = address
        if ab_entry.city:
            ws.cell(9, 2).value = ab_entry.city
        if ab_entry.state:
            ws.cell(10, 2).value = ab_entry.state
        if ab_entry.zip_code:
            ws.cell(11, 2).value = ab_entry.zip_code
        if ab_entry.country:
            ws.cell(12, 2).value = ab_entry.country
        if phone:
            ws.cell(13, 2).value = phone

    wb.save(output_file)
