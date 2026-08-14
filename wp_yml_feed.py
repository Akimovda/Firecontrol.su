#!/usr/bin/env python3
"""
wp_yml_feed.py — генерирует YML-фид (Yandex Market Language) по услугам сайта
для загрузки в каталоги, принимающие YML-импорт (Flagma — flagma.ru/user/yml,
раздел "Товары и услуги" в Яндекс.Бизнесе и т.п.).

Список услуг берётся из WordPress по тегу "Услуги" (id 52 на firecontrol.su —
тот же тег, которым пользуется sidebar-service.php для блока "Другие услуги").
Цена берётся из словаря PRICES ниже — у большинства услуг сайта нет фиксированной
цены в контенте (услуги считаются по объекту), поэтому цифры "от X ₽" названы
пользователем вручную 2026-08-14 и не выводятся из контента страниц. При изменении
цен на услуги — обновить PRICES здесь.

Настройка — те же переменные окружения, что у wp_publish.py/wp_audit.py (.env
подхватывается автоматически): WP_URL, WP_USER, WP_APP_PASSWORD.

Пример:
  python3 wp_yml_feed.py --out feeds/services.yml
"""

import argparse
import os
import sys
from datetime import datetime
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 30
SERVICES_TAG_ID = 52
SHOP_NAME = "FireControl"
SHOP_COMPANY = "ООО «Феникс»"
CATEGORY_ID = "1"
CATEGORY_NAME = "Пожарная безопасность"

# slug -> (цена в рублях, "от X ..." как показывать в описании, если отличается от цены)
PRICES = {
    "ispytaniya-ograzhdenij-krovli": (200, "от 200 ₽ за погонный метр, без сопутствующих расходов"),
    "ispytaniya-pozharnyh-lestnicz": (900, "от 900 ₽ за погонный метр, без сопутствующих расходов"),
    "fire-audit": (3000, "от 3000 ₽"),
    "evacuation-plans": (2000, "от 2000 ₽"),
    "special-technical-conditions": (2_800_000, "от 2 800 000 ₽"),
    "fire-risk-calculation": (45_000, "от 45 000 ₽"),
    "fire-extinguishing-system": (450_000, "от 450 000 ₽"),
    "fire-safety-document": (15_000, "от 15 000 ₽"),
    "categorization": (3000, "от 3000 ₽"),
    "videonablyudenie": (50_000, "от 50 000 ₽"),
    "fire-safety-signs": (150, "от 150 ₽"),
    "certificate-ptm": (20_000, "от 20 000 ₽"),
    "measurement-of-insulation-resistance": (50_000, "от 50 000 ₽"),
    "fire-protection": (250, "от 250 ₽ за м², без сопутствующих расходов"),
    "fire-water-test": (2000, "от 2000 ₽"),
    "fire-alarm-systems": (100_000, "от 100 000 ₽"),
    "installation-of-fire-doors": (5000, "от 5000 ₽"),
    "fire-safety-declaration": (20_000, "от 20 000 ₽"),
}

SEO_DESC_KEYS = ["_yoast_wpseo_metadesc", "rank_math_description"]


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[ошибка] не задана переменная окружения {name} (проверьте .env)")
    return val


def fetch_service_pages(session, base):
    r = session.get(
        f"{base}/wp-json/wp/v2/pages",
        params={"tags": SERVICES_TAG_ID, "per_page": 100, "_fields": "id,slug,title,link,content,meta,featured_media"},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        sys.exit(f"[ошибка] получение страниц услуг: {r.status_code}: {r.text[:400]}")
    return r.json()


def fetch_media_url(session, base, media_id):
    if not media_id:
        return None
    r = session.get(f"{base}/wp-json/wp/v2/media/{media_id}", params={"_fields": "source_url"}, timeout=TIMEOUT)
    if r.status_code >= 400:
        return None
    return r.json().get("source_url")


def pick_description(page):
    meta = page.get("meta") or {}
    for key in SEO_DESC_KEYS:
        val = meta.get(key)
        if val:
            return val
    return page["title"]["rendered"]


def build_yml(offers):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(f'<yml_catalog date="{now}">')
    lines.append("  <shop>")
    lines.append(f"    <name>{escape(SHOP_NAME)}</name>")
    lines.append(f"    <company>{escape(SHOP_COMPANY)}</company>")
    lines.append("    <url>https://firecontrol.su</url>")
    lines.append('    <currencies><currency id="RUR" rate="1"/></currencies>')
    lines.append(f'    <categories><category id="{CATEGORY_ID}">{escape(CATEGORY_NAME)}</category></categories>')
    lines.append("    <offers>")
    for o in offers:
        lines.append(f'      <offer id="{o["id"]}" available="true">')
        lines.append(f'        <name>{escape(o["name"])}</name>')
        lines.append(f'        <url>{escape(o["url"])}</url>')
        lines.append(f'        <price>{o["price"]}</price>')
        lines.append("        <currencyId>RUR</currencyId>")
        lines.append(f"        <categoryId>{CATEGORY_ID}</categoryId>")
        if o["picture"]:
            lines.append(f'        <picture>{escape(o["picture"])}</picture>')
        lines.append(f'        <description>{escape(o["description"])}</description>')
        lines.append("      </offer>")
    lines.append("    </offers>")
    lines.append("  </shop>")
    lines.append("</yml_catalog>")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="feeds/services.yml", help="куда сохранить YML-файл")
    args = parser.parse_args()

    base = env("WP_URL").rstrip("/")
    user = env("WP_USER")
    app_pw = env("WP_APP_PASSWORD")

    session = requests.Session()
    session.auth = (user, app_pw)

    pages = fetch_service_pages(session, base)

    offers = []
    missing_price = []
    for p in pages:
        slug = p["slug"]
        if slug not in PRICES:
            missing_price.append(slug)
            continue
        price, price_note = PRICES[slug]
        picture = fetch_media_url(session, base, p.get("featured_media"))
        desc = pick_description(p)
        if price_note and price_note not in desc:
            desc = f"{desc} Стоимость: {price_note}."
        offers.append(
            {
                "id": p["id"],
                "name": p["title"]["rendered"],
                "url": p["link"],
                "price": price,
                "picture": picture,
                "description": desc,
            }
        )

    if missing_price:
        print(f"[внимание] нет цены в PRICES для: {', '.join(missing_price)} — пропущены", file=sys.stderr)

    yml = build_yml(offers)
    os.makedirs(os.path.dirname(args.out), exist_ok=True) if os.path.dirname(args.out) else None
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(yml)
    print(f"Готово: {len(offers)} услуг записано в {args.out}")


if __name__ == "__main__":
    main()
