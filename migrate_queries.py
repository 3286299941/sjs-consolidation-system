"""
Migrate all Model.query.* legacy API calls to SQLAlchemy 2.0 syntax.
Reads app.py, applies transformations, writes back.
"""

import re

def migrate(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    replacements = 0

    # ============================================================
    # Pattern 0: get_or_404(id) → db.session.get(Model, id) + abort(404)
    # Must run first to avoid being matched by .get()
    # ============================================================
    def replace_get_or_404(m):
        nonlocal replacements
        model = m.group(1)
        args = m.group(2)
        var_match = re.search(r'(\w+)\s*=\s*$', m.string[:m.start()].rsplit('\n', 1)[-1] if '\n' in m.string[:m.start()] else '')
        var_name = var_match.group(1) if var_match else 'result'
        indent_match = re.match(r'^(\s*)', m.string[m.start():])
        indent = indent_match.group(1) if indent_match else '    '
        replacements += 1
        return f"db.session.get({model}, {args})\n{indent}if {var_name} is None:\n{indent}    abort(404)"

    content = re.sub(
        r'(\w+)\.query\.get_or_404\(([^)]+)\)',
        replace_get_or_404,
        content
    )

    # ============================================================
    # Pattern 1: Model.query.get(id) → db.session.get(Model, id)
    # ============================================================
    def replace_get(m):
        nonlocal replacements
        replacements += 1
        return f"db.session.get({m.group(1)}, {m.group(2)})"

    content = re.sub(
        r'(\w+)\.query\.get\(([^)]+)\)',
        replace_get,
        content
    )

    # ============================================================
    # Pattern 2: Model.query.filter_by(k=v).all() → 
    #    db.session.scalars(db.select(Model).filter_by(k=v)).all()
    # ============================================================
    def replace_filter_by_all(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filters = m.group(4).strip()
        # Handle trailing .all() which might be on same line or next
        replacements += 1
        return f"{indent}{rv}db.session.scalars(db.select({model}).filter_by({filters})).all()"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter_by\((.*?)\)\.all\(\)',
        replace_filter_by_all,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 3: Model.query.filter_by(k=v).first() →
    #    db.session.scalar(db.select(Model).filter_by(k=v))
    # ============================================================
    def replace_filter_by_first(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filters = m.group(4).strip()
        replacements += 1
        return f"{indent}{rv}db.session.scalar(db.select({model}).filter_by({filters}))"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter_by\((.*?)\)\.first\(\)',
        replace_filter_by_first,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 4: Model.query.filter_by(k=v).count() →
    #    _scalar_count(Model, k=v)
    # ============================================================
    def replace_filter_by_count(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filters_str = m.group(4).strip()
        if filters_str:
            # Convert k=v to kwargs style for _scalar_count
            replacements += 1
            return f"{indent}{rv}_scalar_count({model}, {filters_str})"
        else:
            replacements += 1
            return f"{indent}{rv}_scalar_count({model})"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter_by\((.*?)\)\.count\(\)',
        replace_filter_by_count,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 5: Model.query.filter_by(k=v).update(...) →
    #    db.session.execute(db.update(Model).filter_by(k=v).values(...))
    # ============================================================
    def replace_filter_by_update(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filters = m.group(4).strip()
        update_kw = m.group(5).strip()
        replacements += 1
        return f"{indent}{rv}db.session.execute(db.update({model}).filter_by({filters}).values({update_kw}))"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter_by\((.*?)\)\.update\((.*?)\)',
        replace_filter_by_update,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 6: Model.query.filter_by(k=v).delete() →
    #    db.session.execute(db.delete(Model).filter_by(k=v))
    # ============================================================
    def replace_filter_by_delete(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filters = m.group(4).strip()
        replacements += 1
        return f"{indent}{rv}db.session.execute(db.delete({model}).filter_by({filters}))"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter_by\((.*?)\)\.delete\(\)',
        replace_filter_by_delete,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 7: Model.query.filter(...).all() →
    #    db.session.scalars(db.select(Model).filter(...)).all()
    # Multi-line support
    # ============================================================
    def replace_filter_all(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        # Capture everything between filter( and matching ).all()
        filter_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.scalars(db.select({model}).filter({filter_body})).all()"

    # Match Model.query.filter(  ...content...  ).all()
    # Use DOTALL for multi-line, but non-greedy
    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.all\(\)',
        replace_filter_all,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 8: Model.query.filter(...).first() →
    #    db.session.scalar(db.select(Model).filter(...))
    # ============================================================
    def replace_filter_first(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filter_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.scalar(db.select({model}).filter({filter_body}))"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.first\(\)',
        replace_filter_first,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 9: Model.query.filter(...).count() →
    #    db.session.scalar(db.select(func.count()).select_from(Model).filter(...))
    # ============================================================
    def replace_filter_count(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filter_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.scalar(db.select(func.count()).select_from({model}).filter({filter_body}))"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.count\(\)',
        replace_filter_count,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 10: Model.query.filter(...).update(...) →
    #    db.session.execute(db.update(Model).filter(...).values(...))
    # ============================================================
    def replace_filter_update(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filter_body = m.group(4)
        update_kw = m.group(5).strip()
        replacements += 1
        return f"{indent}{rv}db.session.execute(db.update({model}).filter({filter_body}).values({update_kw}))"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.update\((.*?)\)',
        replace_filter_update,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 11: Model.query.filter(...).delete() →
    #    db.session.execute(db.delete(Model).filter(...))
    # ============================================================
    def replace_filter_delete(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filter_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.execute(db.delete({model}).filter({filter_body}))"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.delete\(\)',
        replace_filter_delete,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 12: Model.query.join(...).filter(...).all() →
    #    db.session.scalars(db.select(Model).join(...).filter(...)).all()
    # ============================================================
    def replace_join_filter_all(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        join_body = m.group(4)
        filter_body = m.group(5)
        replacements += 1
        return f"{indent}{rv}db.session.scalars(db.select({model}).join({join_body}).filter({filter_body})).all()"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.join\((.*?)\)\.filter\((.*?)\)\.all\(\)',
        replace_join_filter_all,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 13: Model.query.join(...).filter(...).first() →
    #    db.session.scalar(db.select(Model).join(...).filter(...))
    # ============================================================
    def replace_join_filter_first(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        join_body = m.group(4)
        filter_body = m.group(5)
        replacements += 1
        return f"{indent}{rv}db.session.scalar(db.select({model}).join({join_body}).filter({filter_body}))"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.join\((.*?)\)\.filter\((.*?)\)\.first\(\)',
        replace_join_filter_first,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 14: Model.query.options(...).filter(...).all() →
    #    db.session.scalars(db.select(Model).options(...).filter(...)).all()
    # ============================================================
    def replace_options_filter_all(m):
        nonlocal replacements
        model = m.group(1)
        options_body = m.group(2)
        filter_body = m.group(3)
        replacements += 1
        return f"db.session.scalars(db.select({model}).options({options_body}).filter({filter_body})).all()"

    content = re.sub(
        r'(\w+)\.query\.options\((.*?)\)\.filter\((.*?)\)\.all\(\)',
        replace_options_filter_all,
        content,
        flags=re.DOTALL
    )

    # ============================================================
    # Pattern 15: Model.query.options(...) → as base (if no filter)
    # ============================================================
    def replace_options_standalone(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        options_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.select({model}).options({options_body})"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.options\((.*?)\)\s*$',
        replace_options_standalone,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 16: Model.query.order_by(...).all() →
    #    db.session.scalars(db.select(Model).order_by(...)).all()
    # ============================================================
    def replace_order_by_all(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        order = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.scalars(db.select({model}).order_by({order})).all()"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.order_by\((.*?)\)\.all\(\)',
        replace_order_by_all,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 17: Model.query.count() → _scalar_count(Model)
    # ============================================================
    def replace_query_count(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        replacements += 1
        return f"{indent}{rv}_scalar_count({model})"

    content = re.sub(
        r'^(\s*)((\w+)\s*=\s*)?(\w+)\.query\.count\(\)',
        replace_query_count,
        content,
        flags=re.MULTILINE
    )

    # ============================================================
    # Pattern 18: Model.query.filter(Model.attr.op(...)).all()
    #    db.session.scalars(db.select(Model).where(Model.attr.op(...))).all()
    # ============================================================
    def replace_filter_op_all(m):
        nonlocal replacements
        indent = m.group(1)
        rv = m.group(2) or ''
        model = m.group(3)
        filter_body = m.group(4)
        replacements += 1
        return f"{indent}{rv}db.session.scalars(db.select({model}).where({filter_body})).all()"

    content = re.sub(
        r'(\s*)((\w+)\s*=\s*)?(\w+)\.query\.filter\((.*?)\)\.all\(\)',
        replace_filter_op_all,
        content,
        flags=re.MULTILINE | re.DOTALL
    )

    # ============================================================
    # Pattern 19: Remaining .query.filter(...).count() (double-check)
    # Already handled in Pattern 9
    # ============================================================

    # ============================================================
    # Pattern 20: Model.query.order_by(...) without .all() → need scalars
    #    But these are usually assigned and used later with .all()
    #    Let's handle the pattern where it's assigned to a variable
    # ============================================================

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"Total replacements: {replacements}")
    print(f"Remaining .query. calls: {content.count('.query.')}")

if __name__ == '__main__':
    migrate(r'D:\pycharm\pythonProject\集运管理系统\app.py')
