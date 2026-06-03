"""
从下厨房抓取菜品图片。
- 连续5次"有食谱但无图"→判定节流，自动等3分钟后继续
- 全部跑完后如仍有无图菜品，最多重试3轮
- 真正"搜不到同名食谱"的直接跳过不计入节流
"""
import sys, os, time
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import requests
from io import BytesIO
from datetime import datetime
from PIL import Image
from bs4 import BeautifulSoup
from app import app, db, Dish

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.xiachufang.com/',
}
BASE        = 'https://www.xiachufang.com'
UPLOAD      = 'static/uploads'
NORMAL_DELAY    = 30.0  # 正常请求间隔（秒）— 每30秒打开一个页面
THROTTLE_THRESH = 5     # 连续无图达此数即视为软节流
THROTTLE_WAIT   = 300   # 软节流后等待秒数（5分钟）
HARD_BLOCK_WAIT = 300   # 429硬封锁等待秒数（5分钟）
MAX_ROUNDS      = 5     # 最多重跑轮数


def fetch(url, retries=5):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code == 429:
                print(f'  🚫 429 硬封锁，等待 {HARD_BLOCK_WAIT//60} 分钟...', flush=True)
                time.sleep(HARD_BLOCK_WAIT)
                continue  # 重试
            return r
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(8)
    return requests.get(url, headers=HEADERS, timeout=12)  # 最后一次


def search_recipe_url(name):
    r = fetch(f'{BASE}/search/?keyword={requests.utils.quote(name)}')
    if r.status_code != 200:
        return None, False          # (url, exact_match)
    soup = BeautifulSoup(r.text, 'html.parser')
    links = []
    exact = None
    for a in soup.find_all('a', href=True):
        parts = a['href'].strip('/').split('/')
        if len(parts) == 2 and parts[0] == 'recipe' and parts[1].isdigit():
            full = BASE + a['href']
            if full not in links:
                links.append(full)
            if a.get_text(strip=True) == name and exact is None:
                exact = full
    if not links:
        return None, False
    return (exact or links[0]), (exact is not None)


def get_image_url(recipe_url):
    r = fetch(recipe_url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, 'html.parser')

    # 1. og:image（最可靠）
    tag = soup.find('meta', property='og:image')
    if tag and tag.get('content') and not tag['content'].startswith('data:'):
        return tag['content']

    # 2. 特定容器里的 img
    for sel in ['.recipe-cover img', '.cover img', '.main-image img',
                'img.cover-image', 'img.main-photo']:
        img = soup.select_one(sel)
        if img:
            src = img.get('src') or img.get('data-src') or ''
            if src.startswith('http') and 'chuimg.com' in src:
                return src

    # 3. 全页搜 chuimg.com CDN 图
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or ''
        if 'chuimg.com' in src and src.startswith('http'):
            return src

    return None


def download_save(img_url):
    r = fetch(img_url)
    if r.status_code != 200:
        return None
    ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
    fpath = os.path.join(UPLOAD, f'{ts}.jpg')
    img = Image.open(BytesIO(r.content))
    img.thumbnail((300, 300), Image.LANCZOS)
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')
    img.save(fpath, 'JPEG', optimize=True, quality=85)
    return f'static/uploads/{ts}.jpg'


def run_round(dishes, round_num):
    total = len(dishes)
    print(f'\n=== 第 {round_num} 轮，剩余 {total} 道菜 ===\n', flush=True)
    ok = skip = fail = 0
    no_img_streak = 0   # 连续"有食谱但无图"计数

    for i, dish in enumerate(dishes, 1):
        # 节流保护
        if no_img_streak >= THROTTLE_THRESH:
            print(f'  ⏳ 节流检测，等待 {THROTTLE_WAIT}s ...', flush=True)
            time.sleep(THROTTLE_WAIT)
            no_img_streak = 0

        prefix = f'[{i}/{total}] {dish.name}'
        try:
            recipe_url, exact = search_recipe_url(dish.name)
            time.sleep(NORMAL_DELAY)   # 搜索页 → 食谱页 间隔30秒
            if not recipe_url:
                print(f'{prefix} → 无同名食谱，跳过', flush=True)
                skip += 1
                no_img_streak = 0   # 搜不到不算节流
                continue

            match_tag = '(精确)' if exact else '(近似)'
            img_url = get_image_url(recipe_url)
            time.sleep(NORMAL_DELAY)   # 食谱页 → 下载图片 间隔30秒
            if not img_url:
                print(f'{prefix} → 食谱{match_tag}无图', flush=True)
                no_img_streak += 1
                time.sleep(NORMAL_DELAY)
                continue

            path = download_save(img_url)
            if not path:
                print(f'{prefix} → 下载失败', flush=True)
                fail += 1
                no_img_streak = 0
                time.sleep(NORMAL_DELAY)
                continue

            dish.image_url = path
            db.session.commit()
            print(f'{prefix} → ✓ {match_tag}', flush=True)
            ok += 1
            no_img_streak = 0
            time.sleep(NORMAL_DELAY)

        except Exception as e:
            print(f'{prefix} → 异常: {e}', flush=True)
            db.session.rollback()
            fail += 1
            no_img_streak = 0
            time.sleep(4.0)

    print(f'\n本轮：成功 {ok}，跳过(无食谱) {skip}，失败 {fail}', flush=True)
    return ok


with app.app_context():
    total_ok = 0
    for round_num in range(1, MAX_ROUNDS + 1):
        dishes = (Dish.query
                  .filter((Dish.image_url == None) | (Dish.image_url == ''))
                  .order_by(Dish.id).all())
        if not dishes:
            print('所有菜品均已有图片，结束。', flush=True)
            break

        got = run_round(dishes, round_num)
        total_ok += got

        if got == 0:
            print(f'本轮未获得任何新图片，停止重试。', flush=True)
            break

        remaining = (Dish.query
                     .filter((Dish.image_url == None) | (Dish.image_url == ''))
                     .count())
        print(f'剩余无图菜品：{remaining}', flush=True)
        if remaining == 0:
            break
        if round_num < MAX_ROUNDS:
            print(f'等待 60s 后开始第 {round_num+1} 轮...', flush=True)
            time.sleep(60)

    print(f'\n全部完成，累计新增图片 {total_ok} 张。', flush=True)
