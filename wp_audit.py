#!/usr/bin/env python3
"""
wp_audit.py — SEO-аудит опубликованных постов И страниц WordPress через REST API.

Проверяет по каждому посту/странице:
  - длину контента (тонкий контент — меньше --min-words слов, по умолчанию 800)
  - заполнены ли SEO title / description (Yoast или Rank Math — оба мета-поля читаются
    напрямую из post.meta, туда их пишет mu-плагин wp-seo-rest-meta.php)
  - НЕ содержит ли SEO title необработанные Yoast-шаблонные теги вида %%title%% —
    реальный баг, найденный на 5 страницах сайта: Yoast подставляет их как есть,
    если исходный шаблон в настройках не был заполнен для этого типа записи
  - установлена ли обложка (featured image) и есть ли у неё alt
  - есть ли alt у всех картинок в теле контента
  - структуру заголовков: заголовок поста/страницы — это единственный H1, поэтому
    лишний <h1> внутри контента считается дублем H1; отдельно проверяется наличие H2

Каждой проблеме назначен вес, по сумме весов считается приоритет — что чинить первым.

Настройка — те же переменные окружения, что у wp_publish.py (.env подхватывается
автоматически): WP_URL, WP_USER, WP_APP_PASSWORD.

Примеры:
  python3 wp_audit.py
  python3 wp_audit.py --types posts,pages
  python3 wp_audit.py --min-words 1000 --top 15
  python3 wp_audit.py --json reports/audit.json --csv reports/audit.csv
"""

import argparse
import csv
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 30
PER_PAGE = 100

SEO_META_KEYS = {
    "title": ["_yoast_wpseo_title", "rank_math_title"],
    "desc": ["_yoast_wpseo_metadesc", "rank_math_description"],
}

# (код_проблемы, вес, текст_для_таблицы)
ISSUE_WEIGHTS = {
    "thin_content": 3,
    "no_seo_title": 3,
    "broken_seo_title": 3,
    "no_seo_desc": 2,
    "no_featured": 2,
    "featured_no_alt": 1,
    "images_no_alt": 1,
    "duplicate_h1": 1,
    "no_h2": 1,
}


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[ошибка] не задана переменная окружения {name} (проверьте .env)")
    return val


def fetch_all(session, base, post_type, status):
    items = []
    page = 1
    while True:
        r = session.get(
            f"{base}/wp-json/wp/v2/{post_type}",
            params={"status": status, "per_page": PER_PAGE, "page": page, "_embed": 1},
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


def strip_scripts(html):
    return re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)


def strip_tags(html):
    return re.sub(r"<[^>]+>", " ", html)


def word_count(html):
    text = strip_tags(strip_scripts(html))
    return len(text.split())


def count_tag(html, tag):
    return len(re.findall(rf"<{tag}[\s>]", html, re.I))


def images_missing_alt(html):
    """Возвращает (всего_картинок, без_alt) для <img> внутри контента."""
    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    missing = 0
    for tag in imgs:
        m = re.search(r'alt\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if not m or not m.group(1).strip():
            missing += 1
    return len(imgs), missing


def get_seo_field(meta, field):
    for key in SEO_META_KEYS[field]:
        val = meta.get(key)
        if val:
            return val
    return ""


def get_featured_alt(post):
    embedded = post.get("_embedded", {}).get("wp:featuredmedia")
    if not embedded:
        return None
    return (embedded[0].get("alt_text") or "").strip()


def audit_item(post_type, post, min_words):
    title = re.sub(r"<[^>]+>", "", post["title"]["rendered"]).strip()
    content = post["content"]["rendered"]
    meta = post.get("meta", {}) or {}

    words = word_count(content)
    seo_title = get_seo_field(meta, "title")
    seo_desc = get_seo_field(meta, "desc")
    has_featured = bool(post.get("featured_media"))
    featured_alt = get_featured_alt(post) if has_featured else None
    n_images, n_no_alt = images_missing_alt(content)
    h1_count = count_tag(content, "h1")
    h2_count = count_tag(content, "h2")

    issues = []
    if words < min_words:
        issues.append("thin_content")
    if not seo_title:
        issues.append("no_seo_title")
    elif "%%" in seo_title:
        issues.append("broken_seo_title")
    if not seo_desc:
        issues.append("no_seo_desc")
    if not has_featured:
        issues.append("no_featured")
    elif featured_alt == "":
        issues.append("featured_no_alt")
    if n_no_alt:
        issues.append("images_no_alt")
    if h1_count > 0:
        issues.append("duplicate_h1")
    if h2_count == 0 and words >= 300:
        issues.append("no_h2")

    score = sum(ISSUE_WEIGHTS[i] for i in issues)

    return {
        "type": post_type,
        "id": post["id"],
        "title": title,
        "link": post.get("link", ""),
        "words": words,
        "seo_title": seo_title,
        "seo_desc": seo_desc,
        "has_featured": has_featured,
        "featured_alt_missing": featured_alt == "",
        "images_total": n_images,
        "images_no_alt": n_no_alt,
        "h1_count": h1_count,
        "h2_count": h2_count,
        "issues": issues,
        "score": score,
    }


def fmt_bool(v, yes="да", no="нет"):
    return yes if v else no


def print_table(rows, min_words):
    headers = [
        "Тип", "ID", "Заголовок", "Слов", "SEO title", "SEO desc",
        "Обложка", "Alt картинок", "H1", "H2", "Скор", "Проблемы",
    ]
    widths = [5, 6, 35, 6, 9, 9, 8, 12, 4, 4, 5, 40]

    def row_cells(r):
        thin = "!" if r["words"] < min_words else ""
        alt_str = f"{r['images_total'] - r['images_no_alt']}/{r['images_total']}"
        if r["images_no_alt"]:
            alt_str += "!"
        seo_title_cell = "БАГ!" if "broken_seo_title" in r["issues"] else fmt_bool(bool(r["seo_title"]))
        return [
            r["type"],
            str(r["id"]),
            (r["title"][:32] + "...") if len(r["title"]) > 35 else r["title"],
            f"{r['words']}{thin}",
            seo_title_cell,
            fmt_bool(bool(r["seo_desc"])),
            fmt_bool(r["has_featured"]) + ("(!alt)" if r["featured_alt_missing"] else ""),
            alt_str,
            str(r["h1_count"]) + ("!" if r["h1_count"] > 0 else ""),
            str(r["h2_count"]) + ("!" if r["h2_count"] == 0 else ""),
            str(r["score"]),
            ",".join(r["issues"]),
        ]

    def print_row(cells):
        print(" | ".join(c.ljust(w)[:w] for c, w in zip(cells, widths)))

    print_row(headers)
    print_row(["-" * w for w in widths])
    for r in rows:
        print_row(row_cells(r))


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "type", "id", "title", "link", "words", "seo_title", "seo_desc",
            "has_featured", "featured_alt_missing", "images_total",
            "images_no_alt", "h1_count", "h2_count", "score", "issues",
        ])
        for r in rows:
            w.writerow([
                r["type"], r["id"], r["title"], r["link"], r["words"],
                r["seo_title"], r["seo_desc"], r["has_featured"],
                r["featured_alt_missing"], r["images_total"],
                r["images_no_alt"], r["h1_count"], r["h2_count"],
                r["score"], ";".join(r["issues"]),
            ])


def write_json(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def main():
    p = argparse.ArgumentParser(description="SEO-аудит опубликованных постов и страниц WordPress.")
    p.add_argument("--status", default="publish", help="статус записей для аудита (по умолчанию publish)")
    p.add_argument("--types", default="posts,pages", help="через запятую: posts,pages (по умолчанию оба)")
    p.add_argument("--min-words", type=int, default=800, help="порог тонкого контента в словах")
    p.add_argument("--top", type=int, default=0, help="показать только N самых проблемных (0 = все)")
    p.add_argument("--csv", help="сохранить полный отчёт в CSV")
    p.add_argument("--json", help="сохранить полный отчёт в JSON")
    args = p.parse_args()

    base = env("WP_URL").rstrip("/")
    user = env("WP_USER")
    app_pw = env("WP_APP_PASSWORD")

    session = requests.Session()
    session.auth = (user, app_pw)
    session.headers.update({"User-Agent": "wp_audit.py"})

    post_types = [t.strip() for t in args.types.split(",") if t.strip()]
    rows = []
    for post_type in post_types:
        items = fetch_all(session, base, post_type, args.status)
        singular = post_type[:-1] if post_type.endswith("s") else post_type
        rows.extend(audit_item(singular, item, args.min_words) for item in items)

    if not rows:
        sys.exit(f"[внимание] записей со статусом '{args.status}' не найдено ({args.types})")

    rows.sort(key=lambda r: r["score"], reverse=True)

    shown = rows[: args.top] if args.top else rows
    print(f"[инфо] всего записей: {len(rows)}, показано: {len(shown)}\n")
    print_table(shown, args.min_words)

    if args.csv:
        write_csv(rows, args.csv)
        print(f"\n[готово] CSV сохранён: {args.csv}")
    if args.json:
        write_json(rows, args.json)
        print(f"[готово] JSON сохранён: {args.json}")


if __name__ == "__main__":
    main()
