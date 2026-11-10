"""合并 罗兰岗H + 91755I --> 罗兰岗H(5层)，I货架改为备用"""
from app import app, db
from models import ShelfSlot, IncomingPackage

with app.app_context():
    # 1. 先确保 罗兰岗H 有5层（放91755），再移包
    fifth_exist = db.session.execute(
        db.select(db.func.count()).where(
            ShelfSlot.shelf_code == "罗兰岗H", ShelfSlot.floor == 5
        )
    ).scalar()
    
    if fifth_exist == 0:
        for pos in ["1", "2", "3", "4", "5"]:
            sl = ShelfSlot(
                shelf_code="罗兰岗H", floor=5, zone=pos,
                slot_label=f"罗兰岗H-5-{pos}",
                row="", col=0,
                address_label="91755 91755"
            )
            db.session.add(sl)
        db.session.commit()
        print("罗兰岗H 新增第5层(91755) 5个货位")

    # 2. 将 91755I 上的包裹移到 罗兰岗H-5-1
    import sqlalchemy as sa
    h5_slot = db.session.execute(
        db.select(ShelfSlot).where(
            ShelfSlot.shelf_code == "罗兰岗H",
            ShelfSlot.floor == 5,
            ShelfSlot.zone == "1"
        )
    ).scalar()

    if h5_slot:
        i_slot_ids = db.session.execute(
            db.select(ShelfSlot.id).where(ShelfSlot.shelf_code == "91755I")
        ).scalars().all()

        if i_slot_ids:
            updated = db.session.execute(
                sa.update(IncomingPackage)
                .where(IncomingPackage.shelf_slot_id.in_(i_slot_ids))
                .values(shelf_slot_id=h5_slot.id)
            )
            db.session.commit()
            print(f"移包: {updated.rowcount} 件 -> 罗兰岗H-5-1")

    # 3. 删除 91755I
    deleted = db.session.execute(
        sa.delete(ShelfSlot).where(ShelfSlot.shelf_code == "91755I")
    )
    db.session.commit()
    print(f"删除 91755I: {deleted.rowcount} 个货位")

    # 4. 创建备用I货架
    i_existing = db.session.execute(
        db.select(db.func.count()).where(ShelfSlot.shelf_code == "备用I")
    ).scalar()

    if i_existing == 0:
        for floor in range(1, 5):
            for pos in ["1", "2", "3", "4", "5"]:
                sl = ShelfSlot(
                    shelf_code="备用I", floor=floor, zone=pos,
                    slot_label=f"备用I-{floor}-{pos}",
                    row="", col=0,
                    address_label=""
                )
                db.session.add(sl)
        db.session.commit()
        print("创建 备用I (4层×5=20货位)")
    else:
        print(f"备用I 已存在: {i_existing} 货位")

    # 5. 验证
    print("\n=== 最终状态 ===")
    for code in ["罗兰岗H", "备用I"]:
        rows = db.session.execute(
            db.select(ShelfSlot.floor, db.func.count(), ShelfSlot.address_label)
            .where(ShelfSlot.shelf_code == code)
            .group_by(ShelfSlot.floor).order_by(ShelfSlot.floor)
        ).all()
        for floor, cnt, addr in rows:
            print(f"  {code} 第{floor}层: {cnt}位 [{addr}]")

    print("\n完成!")
