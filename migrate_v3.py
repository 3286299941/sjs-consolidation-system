"""
V3: Ordered by specificity, uses balanced-paren matching for nested args.
"""
import re

def balanced_content(text, start_pos):
    """Find content within balanced parentheses starting from start_pos (after '(')."""
    depth = 1
    i = start_pos
    while i < len(text) and depth > 0:
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
        i += 1
    if depth != 0:
        return None
    return text[start_pos:i-1]

def replace_pattern(content, model, method_chain, transformer, flags=0):
    """Find Model.query.method_chain and replace using transformer.
    Handles balanced parentheses."""
    prefix = f'{model}.query.{method_chain}'
    result = []
    i = 0
    count = 0
    while i < len(content):
        pos = content.find(prefix, i)
        if pos == -1:
            result.append(content[i:])
            break
        result.append(content[i:pos])
        i = pos + len(prefix)
        # Now we're at the opening paren
        if i < len(content) and content[i] == '(':
            args_str = balanced_content(content, i + 1)
            if args_str is not None:
                end_pos = i + len(args_str) + 2  # past the closing )
                replacement = transformer(args_str, content, pos, end_pos)
                result.append(replacement)
                i = end_pos
                count += 1
            else:
                result.append(prefix)
        else:
            result.append(prefix)
    return ''.join(result), count

def replace_simple(content, old, new):
    """Simple string replacement."""
    count = content.count(old)
    return content.replace(old, new), count


def migrate(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    total_before = content.count('.query.')
    all_stats = []

    # ==============================================================
    # Order matters! More specific first.
    # ==============================================================

    def do(name, fn):
        nonlocal content
        old_count = content.count('.query.')
        content, cnt = fn(content)
        new_count = content.count('.query.')
        all_stats.append((name, cnt, old_count - new_count))
        print(f'  [{name}] {cnt} replaced, {new_count} remaining')

    # --- fix get_or_404: must be BEFORE get ---
    def fix_get_or_404(content_in):
        models = ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']
        config = [
            # (target, replacement_prefix, need_abort)
            (f'1.query.get_or_404(', 'db.session.get(1, ', True)
        ]
        result = content_in
        total = 0
        for m in models:
            old = f'{m}.query.get_or_404('
            new_prefix = f'db.session.get({m}, '
            # Simple replacement - get_or_404 has simple args
            cnt = result.count(old)
            if cnt == 0:
                continue
            result = result.replace(old, new_prefix)
            total += cnt
        return result, total
    do('get_or_404', fix_get_or_404)

    # --- fix .get() ---
    def fix_get(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.get('
            new = f'db.session.get({m}, '
            cnt = content_in.count(old)
            if cnt:
                content_in = content_in.replace(old, new)
                total += cnt
        return content_in, total
    do('get', fix_get)

    # --- fix filter_by(...).first() ---
    def fix_filter_by_first(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter_by('
            # Find occurrences
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                # Find args
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                # Check if followed by .first()
                after_close = pos + len(old) + len(args) + 1  # past ')'
                if content_in[after_close:after_close+8] == '.first()':
                    replacement = f'db.session.scalar(db.select({m}).filter_by({args}))'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter_by().first()', fix_filter_by_first)

    # --- fix filter_by(...).all() ---
    def fix_filter_by_all(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter_by('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+6] == '.all()':
                    replacement = f'db.session.scalars(db.select({m}).filter_by({args})).all()'
                    content_in = content_in[:pos] + replacement + content_in[after_close+6:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter_by().all()', fix_filter_by_all)

    # --- fix filter_by(...).count() ---
    def fix_filter_by_count(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter_by('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.count()':
                    if args.strip():
                        replacement = f'_scalar_count({m}, {args})'
                    else:
                        replacement = f'_scalar_count({m})'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter_by().count()', fix_filter_by_count)

    # --- fix filter_by(...).update(...) ---
    def fix_filter_by_update(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter_by('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.update(':
                    update_pos = after_close + 7  # after '.update('
                    update_args = balanced_content(content_in, update_pos + 1)
                    if update_args is not None:
                        update_end = update_pos + len(update_args) + 2
                        replacement = f'db.session.execute(db.update({m}).filter_by({args}).values({update_args}))'
                        content_in = content_in[:pos] + replacement + content_in[update_end:]
                        total += 1
                    else:
                        pos += 1
                else:
                    pos += 1
        return content_in, total
    do('filter_by().update()', fix_filter_by_update)

    # --- fix filter_by(...).delete() ---
    def fix_filter_by_delete(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter_by('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.delete()':
                    replacement = f'db.session.execute(db.delete({m}).filter_by({args}))'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter_by().delete()', fix_filter_by_delete)

    # --- fix options(...).filter(...).all() ---
    def fix_options_filter_all(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.options('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                opt_args = balanced_content(content_in, pos + len(old))
                if opt_args is None:
                    pos += 1
                    continue
                after_opt = pos + len(old) + len(opt_args) + 1
                # Check for .filter(
                if content_in[after_opt:after_opt+8] == '.filter(':
                    filter_pos = after_opt + 7  # after '.filter('
                    filter_args = balanced_content(content_in, filter_pos + 1)
                    if filter_args is not None:
                        filter_end = filter_pos + len(filter_args) + 2
                        # Check for .all()
                        if content_in[filter_end:filter_end+6] == '.all()':
                            replacement = f'db.session.scalars(db.select({m}).options({opt_args}).filter({filter_args})).all()'
                            content_in = content_in[:pos] + replacement + content_in[filter_end+6:]
                            total += 1
                            continue
                pos += 1
        return content_in, total
    do('options().filter().all()', fix_options_filter_all)

    # --- fix filter(...).first() ---
    def fix_filter_first(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.first()':
                    replacement = f'db.session.scalar(db.select({m}).filter({args}))'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter().first()', fix_filter_first)

    # --- fix filter(...).all() ---
    def fix_filter_all(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+6] == '.all()':
                    replacement = f'db.session.scalars(db.select({m}).filter({args})).all()'
                    content_in = content_in[:pos] + replacement + content_in[after_close+6:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter().all()', fix_filter_all)

    # --- fix filter(...).count() ---
    def fix_filter_count(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.count()':
                    replacement = f'db.session.scalar(db.select(func.count()).select_from({m}).filter({args}))'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter().count()', fix_filter_count)

    # --- fix filter(...).update(...) ---
    def fix_filter_update(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.update(':
                    update_pos = after_close + 7
                    update_args = balanced_content(content_in, update_pos + 1)
                    if update_args is not None:
                        update_end = update_pos + len(update_args) + 2
                        replacement = f'db.session.execute(db.update({m}).filter({args}).values({update_args}))'
                        content_in = content_in[:pos] + replacement + content_in[update_end:]
                        total += 1
                    else:
                        pos += 1
                else:
                    pos += 1
        return content_in, total
    do('filter().update()', fix_filter_update)

    # --- fix filter(...).delete() ---
    def fix_filter_delete(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.filter('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+8] == '.delete()':
                    replacement = f'db.session.execute(db.delete({m}).filter({args}))'
                    content_in = content_in[:pos] + replacement + content_in[after_close+8:]
                    total += 1
                pos += 1
        return content_in, total
    do('filter().delete()', fix_filter_delete)

    # --- fix join(...).filter(...).all() ---
    def fix_join_filter_all(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.join('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                join_args = balanced_content(content_in, pos + len(old))
                if join_args is None:
                    pos += 1
                    continue
                after_join = pos + len(old) + len(join_args) + 1
                if content_in[after_join:after_join+8] == '.filter(':
                    filt_pos = after_join + 7
                    filt_args = balanced_content(content_in, filt_pos + 1)
                    if filt_args is not None:
                        filt_end = filt_pos + len(filt_args) + 2
                        if content_in[filt_end:filt_end+6] == '.all()':
                            replacement = f'db.session.scalars(db.select({m}).join({join_args}).filter({filt_args})).all()'
                            content_in = content_in[:pos] + replacement + content_in[filt_end+6:]
                            total += 1
                            continue
                pos += 1
        return content_in, total
    do('join().filter().all()', fix_join_filter_all)

    # --- fix join(...).filter(...).first() ---
    def fix_join_filter_first(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.join('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                join_args = balanced_content(content_in, pos + len(old))
                if join_args is None:
                    pos += 1
                    continue
                after_join = pos + len(old) + len(join_args) + 1
                if content_in[after_join:after_join+8] == '.filter(':
                    filt_pos = after_join + 7
                    filt_args = balanced_content(content_in, filt_pos + 1)
                    if filt_args is not None:
                        filt_end = filt_pos + len(filt_args) + 2
                        if content_in[filt_end:filt_end+9] == '.first()':
                            replacement = f'db.session.scalar(db.select({m}).join({join_args}).filter({filt_args}))'
                            content_in = content_in[:pos] + replacement + content_in[filt_end+9:]
                            total += 1
                            continue
                pos += 1
        return content_in, total
    do('join().filter().first()', fix_join_filter_first)

    # --- fix order_by(...).all() ---
    def fix_order_by_all(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.order_by('
            pos = 0
            while True:
                pos = content_in.find(old, pos)
                if pos == -1:
                    break
                args = balanced_content(content_in, pos + len(old))
                if args is None:
                    pos += 1
                    continue
                after_close = pos + len(old) + len(args) + 1
                if content_in[after_close:after_close+6] == '.all()':
                    replacement = f'db.session.scalars(db.select({m}).order_by({args})).all()'
                    content_in = content_in[:pos] + replacement + content_in[after_close+6:]
                    total += 1
                pos += 1
        return content_in, total
    do('order_by().all()', fix_order_by_all)

    # --- fix .query.count() ---
    def fix_count(content_in):
        total = 0
        for m in ['Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
                   'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord']:
            old = f'{m}.query.count()'
            cnt = content_in.count(old)
            if cnt:
                content_in = content_in.replace(old, f'_scalar_count({m})')
                total += cnt
        return content_in, total
    do('.query.count()', fix_count)

    # ==============================================================
    # Check for remaining .query calls
    # ==============================================================
    remaining = content.count('.query.')
    print(f'\n========================================')
    print(f'Total: {total_before} → {remaining} ({total_before - remaining} migrated)')

    if remaining > 0:
        print(f'\nRemaining .query calls:')
        for line_no, line in enumerate(content.split('\n'), 1):
            if '.query.' in line:
                print(f'  Line {line_no}: {line.strip()[:100]}')

    # Save
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'\nSaved to {filepath}')

if __name__ == '__main__':
    migrate(r'D:\pycharm\pythonProject\集运管理系统\app.py')
