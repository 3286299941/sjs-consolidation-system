"""
Precise migration: each pattern handled individually.
"""
import re

PATTERNS = []

def pattern(name, search, replace, count_only=False):
    PATTERNS.append((name, search, replace, count_only))

# ================================================================
# Step 1: Simple replacements - Model.query.method( → new method(
# These change the prefix but keep args unchanged
# ================================================================

# get_or_404: Model.query.get_or_404(id) → db.session.get(Model, id)
# But need abort(404) if None. We'll handle get_or_404 specially.
# For now, replace to db.session.get and add abort later manually.

simple_models = [
    'Customer', 'Batch', 'PackedPackage', 'Carton', 'IncomingPackage',
    'PackedPackageItem', 'CartonItem', 'PriceConfig', 'FreightRecord'
]

# Pattern 1: Model.query.get( → db.session.get(Model,
for m in simple_models:
    pattern(
        f'{m}.query.get(',
        rf'\b{m}\.query\.get\(',
        f'db.session.get({m}, '
    )

# Pattern 2: Model.query.get_or_404( → db.session.get(Model,
for m in simple_models:
    pattern(
        f'{m}.query.get_or_404(',
        rf'\b{m}\.query\.get_or_404\(',
        f'db.session.get({m}, '
    )

# ================================================================
# Step 2: filter_by(...).first() → db.session.scalar(db.select(M).filter_by(...))
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter_by(ARGS).first()',
        rf'\b{m}\.query\.filter_by\((.*?)\)\.first\(\)',
        lambda mt: f'db.session.scalar(db.select({m}).filter_by(' + mt.group(1) + '))'
    )

# ================================================================
# Step 3: filter_by(...).all() → db.session.scalars(db.select(M).filter_by(...)).all()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter_by(ARGS).all()',
        rf'\b{m}\.query\.filter_by\((.*?)\)\.all\(\)',
        lambda mt: f'db.session.scalars(db.select({m}).filter_by(' + mt.group(1) + ')).all()'
    )

# ================================================================
# Step 4: filter(...).first() → db.session.scalar(db.select(M).filter(...))
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter(ARGS).first()',
        rf'\b{m}\.query\.filter\((.*?)\)\.first\(\)',
        lambda mt: f'db.session.scalar(db.select({m}).filter(' + mt.group(1) + '))'
    )

# ================================================================
# Step 5: filter(...).all() → db.session.scalars(db.select(M).filter(...)).all()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter(ARGS).all()',
        rf'\b{m}\.query\.filter\((.*?)\)\.all\(\)',
        lambda mt: f'db.session.scalars(db.select({m}).filter(' + mt.group(1) + ')).all()'
    )

# ================================================================
# Step 6: join(...).filter(...).all() 
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.join(J).filter(F).all()',
        rf'\b{m}\.query\.join\((.*?)\)\.filter\((.*?)\)\.all\(\)',
        lambda mt: f'db.session.scalars(db.select({m}).join(' + mt.group(1) + ').filter(' + mt.group(2) + ')).all()'
    )

# ================================================================
# Step 7: join(...).filter(...).first()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.join(J).filter(F).first()',
        rf'\b{m}\.query\.join\((.*?)\)\.filter\((.*?)\)\.first\(\)',
        lambda mt: f'db.session.scalar(db.select({m}).join(' + mt.group(1) + ').filter(' + mt.group(2) + '))'
    )

# ================================================================
# Step 8: options(...).filter(...).all()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.options(O).filter(F).all()',
        rf'\b{m}\.query\.options\((.*?)\)\.filter\((.*?)\)\.all\(\)',
        lambda mt: f'db.session.scalars(db.select({m}).options(' + mt.group(1) + ').filter(' + mt.group(2) + ')).all()'
    )

# ================================================================
# Step 9: options(...) as a standalone base (no .filter after)
# These are usually assigned to a variable and used with .all() later
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.options(O)',
        rf'\b{m}\.query\.options\(([^)]*)\)',
        lambda mt: f'db.select({m}).options(' + mt.group(1) + ')'
    )

# ================================================================
# Step 10: order_by(...).all()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.order_by(O).all()',
        rf'\b{m}\.query\.order_by\((.*?)\)\.all\(\)',
        lambda mt: f'db.session.scalars(db.select({m}).order_by(' + mt.group(1) + ')).all()'
    )

# ================================================================
# Step 11: filter_by(...).update(...)
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter_by(F).update(U)',
        rf'\b{m}\.query\.filter_by\((.*?)\)\.update\((.*?)\)',
        lambda mt: f'db.session.execute(db.update({m}).filter_by(' + mt.group(1) + ').values(' + mt.group(2) + '))'
    )

# ================================================================
# Step 12: filter_by(...).delete()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter_by(F).delete()',
        rf'\b{m}\.query\.filter_by\((.*?)\)\.delete\(\)',
        lambda mt: f'db.session.execute(db.delete({m}).filter_by(' + mt.group(1) + '))'
    )

# ================================================================
# Step 13: filter(...).update(...)
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter(F).update(U)',
        rf'\b{m}\.query\.filter\((.*?)\)\.update\((.*?)\)',
        lambda mt: f'db.session.execute(db.update({m}).where(' + mt.group(1) + ').values(' + mt.group(2) + '))'
    )

# ================================================================
# Step 14: filter(...).delete()  
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.filter(F).delete()',
        rf'\b{m}\.query\.filter\((.*?)\)\.delete\(\)',
        lambda mt: f'db.session.execute(db.delete({m}).where(' + mt.group(1) + '))'
    )

# ================================================================
# Step 15: count()
# ================================================================
for m in simple_models:
    pattern(
        f'{m}.query.count()',
        rf'\b{m}\.query\.count\(\)',
        lambda mt: f'_scalar_count({m})'
    )

print(f"Total patterns defined: {len(PATTERNS)}")


def migrate(content):
    """Apply all patterns in order."""
    stats = {}
    for i, (name, search_pat, replace, count_only) in enumerate(PATTERNS):
        before_count = len(re.findall(search_pat, content, flags=re.DOTALL))
        if before_count == 0:
            continue
        if count_only:
            stats[name] = before_count
        elif callable(replace):
            content = re.sub(search_pat, replace, content, flags=re.DOTALL)
            after_count = len(re.findall(search_pat, content, flags=re.DOTALL))
            stats[name] = before_count - after_count
        else:
            content = re.sub(search_pat, replace, content)
            after_count = len(re.findall(search_pat, content, flags=re.DOTALL))
            stats[name] = before_count - after_count
    
    return content, stats


if __name__ == '__main__':
    filepath = r'D:\pycharm\pythonProject\集运管理系统\app.py'
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    before_total = content.count('.query.')
    content, stats = migrate(content)
    after_total = content.count('.query.')
    
    # Show stats
    for name, count in stats.items():
        if count > 0:
            print(f'  ✓ {name}: {count} replaced')
    print(f'\nTotal: {before_total} → {after_total}')
    
    # Save
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print('Saved.')
