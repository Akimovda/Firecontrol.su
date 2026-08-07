#!/usr/bin/env python3
"""
wp_linkmap.py — карта внутренних ссылок между постами и страницами WordPress.

Выгружает все опубликованные посты (post) и страницы (page) через REST API,
парсит внутренние ссылки в контенте (<a href="...">, ведущие на этот же домен)
и строит граф: у каждой страницы/статьи считается число входящих внутренних
ссылок. Страницы с нулём входящих ссылок из контента — кандидаты в "сироты"
(orphan pages).

Скрипт отдаёт только сырые данные (граф + короткая выжимка контента каждой
страницы) в JSON — семантические рекомендации "откуда сослаться по смыслу"
осмысленнее делать по этим данным вручную/через LLM, а не эвристикой по
пересечению слов.

Настройка — .env (WP_URL, WP_USER, WP_APP_PASSWORD), как у wp_publish.py / wp_audit.py.

Пример:
  python3 wp_linkmap.py --json reports/linkmap.json
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 30
PER_PAGE = 100


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[ошибка] не задана переменная окружения {name} (проверьте .env)")
    return val


def fetch_all(session, base, post_type, status="publish"):
    items = []
    page = 1
    while True:
        r = session.get(
            f"{base}/wp-json/wp/v2/{post_type}",
            params={"status": status, "per_page": PER_PAGE, "page": page},
            timeout=TIMEOUT,
        )
        if r.status_code == 400 and page > 1:
            break
        if r.status_code >= 400:
            sys.exit(f"[ошибка] получение {post_type}: {r.status_code}: {r.text[:400]}")
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return items


def norm_url(url, domain):
    """Приводим ссылку к каноническому виду path без домена/протокола/якоря/трейлинг-слэша."""
    if url.startswith("#"):
        return None
    if url.startswith("/"):
        path = url
    else:
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc.replace("www.", "") != domain.replace("www.", ""):
            return None  # внешняя ссылка
        path = parsed.path
    path = path.split("#")[0].split("?")[0]
    return path.rstrip("/") or "/"


def extract_links(html, domain):
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    out = []
    for h in hrefs:
        p = norm_url(h, domain)
        if p:
            out.append(p)
    return out


def strip_tags(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main():
    p = argparse.ArgumentParser(description="Карта внутренних ссылок WordPress.")
    p.add_argument("--json", help="сохранить сырые данные графа в JSON")
    args = p.parse_args()

    base = env("WP_URL").rstrip("/")
    user = env("WP_USER")
    app_pw = env("WP_APP_PASSWORD")
    domain = urlparse(base).netloc

    session = requests.Session()
    session.auth = (user, app_pw)
    session.headers.update({"User-Agent": "wp_linkmap.py"})

    posts = fetch_all(session, base, "posts")
    pages = fetch_all(session, base, "pages")

    items = []
    for p_ in posts:
        items.append({"type": "post", "id": p_["id"], "link": p_["link"], "title": p_["title"]["rendered"], "content": p_["content"]["rendered"]})
    for p_ in pages:
        items.append({"type": "page", "id": p_["id"], "link": p_["link"], "title": p_["title"]["rendered"], "content": p_["content"]["rendered"]})

    path_to_item = {}
    for it in items:
        path = norm_url(it["link"], domain)
        it["path"] = path
        path_to_item[path] = it

    incoming = {it["path"]: [] for it in items}
    outgoing = {it["path"]: [] for it in items}

    for it in items:
        for target_path in set(extract_links(it["content"], domain)):
            if target_path == it["path"]:
                continue
            outgoing[it["path"]].append(target_path)
            if target_path in incoming:
                incoming[target_path].append(it["path"])
            # если ссылка ведёт на путь, которого нет среди опубликованных items
            # (например, страница закрыта/удалена) — просто не попадёт в incoming

    result = []
    for it in items:
        title = re.sub(r"<[^>]+>", "", it["title"]).strip()
        excerpt = strip_tags(it["content"])[:400]
        result.append({
            "type": it["type"],
            "id": it["id"],
            "path": it["path"],
            "link": it["link"],
            "title": title,
            "excerpt": excerpt,
            "incoming_count": len(incoming[it["path"]]),
            "incoming_from": incoming[it["path"]],
            "outgoing_count": len(set(outgoing[it["path"]])),
            "outgoing_to": sorted(set(outgoing[it["path"]])),
        })

    result.sort(key=lambda r: r["incoming_count"])

    orphans = [r for r in result if r["incoming_count"] == 0 and r["path"] != "/"]

    print(f"[инфо] постов: {len(posts)}, страниц: {len(pages)}, всего элементов: {len(items)}")
    print(f"[инфо] страниц-сирот (0 входящих ссылок из контента): {len(orphans)}\n")

    print(f"{'Тип':6} | {'ID':5} | {'Вход':5} | {'Исход':6} | Заголовок")
    print("-" * 100)
    for r in result:
        print(f"{r['type']:6} | {r['id']:5} | {r['incoming_count']:5} | {r['outgoing_count']:6} | {r['title'][:70]}")

    if args.json:
        os.makedirs(os.path.dirname(args.json), exist_ok=True) if os.path.dirname(args.json) else None
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[готово] JSON сохранён: {args.json}")


if __name__ == "__main__":
    main()
