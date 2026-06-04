"""数据库初始化脚本。

默认行为：仅在表为空时创建表并填充示例数据；已有数据时不动。
传 --reset 参数：drop_all + create_all + 重新填充示例数据（销毁所有订单与菜品）。

警告：--reset 是破坏性操作，会清空所有真实订单，仅用于开发/重置测试环境。
"""
import sys
import json
from app import app, db, Category, Dish, Order

# 确保 print 输出中文时不会因终端编码问题乱码
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def seed_examples():
    """填充示例分类树与示例菜品。
    分类结构：顶级分类（猪/牛/羊/鸡/海鲜/热素菜/凉菜/主食/汤）→ 子分类（如排骨菜/肉片菜...）。
    parent_id=0 表示顶级，与 app.py 中的约定保持一致。
    """
    # 第一步：创建顶级分类，(名称, parent_id=0, sort_order)
    categories = [
        ('猪', 0, 1), ('牛', 0, 2), ('羊', 0, 3), ('鸡', 0, 4),
        ('海鲜', 0, 5), ('热素菜', 0, 6), ('凉菜', 0, 7), ('主食', 0, 8), ('汤', 0, 9),
    ]
    for name, parent, order in categories:
        db.session.add(Category(name=name, parent_id=parent, sort_order=order))
    db.session.commit()

    # 第二步：查出顶级分类的 id，再挂子分类（需要先 commit 才能查到 id）
    pig_cat = Category.query.filter_by(name='猪', parent_id=0).first()
    beef_cat = Category.query.filter_by(name='牛', parent_id=0).first()
    chicken_cat = Category.query.filter_by(name='鸡', parent_id=0).first()

    pork_sub = ['排骨菜', '肉片菜', '里脊菜', '肉丝菜', '五花肉菜', '丸子菜', '肉末菜', '肘子菜', '内脏菜', '午餐肉菜']
    for i, sub in enumerate(pork_sub, 1):
        db.session.add(Category(name=sub, parent_id=pig_cat.id, sort_order=i))

    beef_sub = ['牛腩菜', '牛里脊', '牛蹄筋', '牛腱子', '牛杂菜']
    for i, sub in enumerate(beef_sub, 1):
        db.session.add(Category(name=sub, parent_id=beef_cat.id, sort_order=i))

    chicken_sub = ['鸡胸菜', '鸡翅', '鸡腿', '鸡杂']
    for i, sub in enumerate(chicken_sub, 1):
        db.session.add(Category(name=sub, parent_id=chicken_cat.id, sort_order=i))

    db.session.commit()

    # 第三步：查出子分类 id，再添加示例菜品
    paigu_cat = Category.query.filter_by(name='排骨菜', parent_id=pig_cat.id).first()
    reqie_cat = Category.query.filter_by(name='热素菜', parent_id=0).first()
    zhushi_cat = Category.query.filter_by(name='主食', parent_id=0).first()

    # 示例菜品：糖醋排骨带一个单选规格（口味）
    db.session.add(Dish(
        name='糖醋排骨', category_id=paigu_cat.id,
        ingredients='猪小排, 糖, 醋, 酱油',
        bento_compatible=True, preprocess_days=4, cook_minutes=40,
        extra_options=json.dumps([{"name": "口味", "type": "radio", "options": ["糖醋", "话梅"]}], ensure_ascii=False),
    ))
    db.session.add(Dish(
        name='清炒油菜', category_id=reqie_cat.id,
        ingredients='油菜, 蒜', bento_compatible=True,
        preprocess_days=0, cook_minutes=10, extra_options='[]',
    ))
    db.session.add(Dish(
        name='米饭', category_id=zhushi_cat.id,
        ingredients='大米', bento_compatible=True,
        preprocess_days=0, cook_minutes=20, extra_options='[]',
    ))
    db.session.commit()


def init(reset=False):
    """执行数据库初始化。
    reset=True：先 drop_all 再重建，适用于开发环境重置。
    reset=False（默认）：仅在表为空时填充示例数据，已有数据则跳过，避免误删真实数据。
    """
    with app.app_context():
        if reset:
            db.drop_all()
            db.create_all()
            seed_examples()
            print('数据库已重置并填充示例数据。')
            return

        db.create_all()
        # 检查是否已有数据，有则跳过，保护真实订单不被覆盖
        has_category = db.session.query(Category.id).first() is not None
        has_dish = db.session.query(Dish.id).first() is not None
        if has_category or has_dish:
            print('检测到已有数据，跳过示例填充。如需重置请使用 --reset。')
            return

        seed_examples()
        print('数据库初始化完成（已填充示例数据）。')


if __name__ == '__main__':
    reset_flag = '--reset' in sys.argv
    init(reset=reset_flag)
