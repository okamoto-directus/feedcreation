#!/usr/bin/env python3
"""
site-watcher: 指定したWebページの変更を検知して、サイトごとにRSSフィードを生成する。

2つのモードがある:
  - mode: "diff" (デフォルト): ページ全体（またはセレクタで指定した範囲）のテキストを
      ハッシュ比較し、変化があれば「更新されました」という1件の通知アイテムを追加する。
      更新お知らせページなど、個々の記事リンクを持たないページ向け。
  - mode: "list": ブログ一覧ページのように「記事タイトル+リンク」が並んでいるページ向け。
      heading_selector に一致する要素（見出し内のリンク）を全部拾い、
      記事ごとにRSSアイテムを作る。初回実行時から現在ある記事は全部フィードに載る。
      次回以降、新しく増えたリンクだけが新着として追加される（消えた記事は残り続ける）。

GitHub Actions から定期実行される想定。state.json と docs/ はリポジトリにコミットされ、
docs/ が GitHub Pages で公開されることで、生成されたXMLが外部からURLで読めるようになる。
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sites.yaml"
STATE_PATH = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
FEEDS_DIR = DOCS_DIR / "feeds"

MAX_ITEMS = 50

import os
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; site-watcher/1.0; "
        "+https://github.com/) personal RSS generator"
    )
}

# 「June 30, 2026」のような日付テキストを見つけるための正規表現
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)


def load_config():
    if not CONFIG_PATH.exists():
        print(f"設定ファイルが見つかりません: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        print("sites.yaml はリスト形式である必要があります。", file=sys.stderr)
        sys.exit(1)
    return data


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# diff モード（ページ全体/一部の変更検知）
# ---------------------------------------------------------------------------

def process_diff_site(site: dict, soup: BeautifulSoup, site_state: dict) -> dict:
    selector = (site.get("selector") or "").strip()
    if selector:
        node = soup.select_one(selector)
        if node is None:
            raise ValueError(f"セレクタ '{selector}' に一致する要素が見つかりません")
        text = node.get_text("\n", strip=True)
    else:
        body = soup.body or soup
        text = body.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)

    new_hash = text_hash(text)
    old_hash = site_state.get("hash")

    if old_hash is None:
        print("  -> 初回実行のため、ベースラインとして保存します。")
    elif new_hash != old_hash:
        print("  -> 変更を検知しました。フィードに追加します。")
        now = datetime.now(timezone.utc)
        snippet = text[:400].replace("\n", " ")
        item = {
            "title": f"{site['name']} が更新されました（{now.strftime('%Y-%m-%d %H:%M UTC')}）",
            "link": site["url"],
            "guid": f"{site['id']}-{new_hash[:12]}-{int(now.timestamp())}",
            "pubDate": now.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "description": snippet,
        }
        site_state.setdefault("items", []).insert(0, item)
        site_state["items"] = site_state["items"][:MAX_ITEMS]
    else:
        print("  -> 変更なし。")

    site_state["hash"] = new_hash
    return site_state


# ---------------------------------------------------------------------------
# list モード（記事一覧ページ）
# ---------------------------------------------------------------------------

def parse_article_date(heading_tag) -> datetime | None:
    """見出しタグの直後にある兄弟要素から日付らしきテキストを探す"""
    sib = heading_tag
    for _ in range(5):
        sib = sib.find_next_sibling()
        if sib is None:
            break
        text = sib.get_text(" ", strip=True)
        m = DATE_RE.search(text)
        if m:
            try:
                return datetime.strptime(m.group(0), "%B %d, %Y").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
    return None


def process_list_site(site: dict, soup: BeautifulSoup, site_state: dict) -> dict:
    heading_selector = site.get("heading_selector") or "h2 a, h3 a"
    base_url = site["url"]

    anchors = soup.select(heading_selector)
    if not anchors:
        raise ValueError(
            f"heading_selector '{heading_selector}' に一致する記事が見つかりません"
        )

    articles = site_state.get("articles", {})  # link -> {title, link, pubDate}
    now = datetime.now(timezone.utc)
    seen_links = set()

    for a in anchors:
        title = a.get_text(strip=True)
        href = a.get("href")
        if not title or not href:
            continue
        link = urljoin(base_url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        if link in articles:
            # 既存記事はタイトルだけ最新化（日付やguidは変えない）
            articles[link]["title"] = title
            continue

        # 新規記事: ページ上の見出しの近くから日付を探す。見つからなければ「今」を使う。
        heading_tag = a.find_parent(["h1", "h2", "h3", "h4"]) or a
        article_date = parse_article_date(heading_tag) or now

        print(f"  -> 新しい記事を検知: {title}")
        articles[link] = {
            "title": title,
            "link": link,
            "pubDate_iso": article_date.isoformat(),
        }

    site_state["articles"] = articles
    return site_state


def build_rss_items_from_state(site_state: dict, mode: str) -> list:
    if mode == "list":
        articles = list(site_state.get("articles", {}).values())
        # 日付の新しい順に並べる
        def sort_key(a):
            try:
                return datetime.fromisoformat(a["pubDate_iso"])
            except Exception:  # noqa: BLE001
                return datetime.min.replace(tzinfo=timezone.utc)

        articles.sort(key=sort_key, reverse=True)
        items = []
        for a in articles[:MAX_ITEMS]:
            try:
                dt = datetime.fromisoformat(a["pubDate_iso"])
            except Exception:  # noqa: BLE001
                dt = datetime.now(timezone.utc)
            items.append(
                {
                    "title": a["title"],
                    "link": a["link"],
                    "guid": a["link"],
                    "pubDate": dt.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "description": a["title"],
                }
            )
        return items
    else:
        return site_state.get("items", [])[:MAX_ITEMS]


def build_rss(site: dict, items: list) -> str:
    now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    site_name = escape(site["name"])
    site_url = escape(site["url"])

    item_xml_parts = []
    for it in items:
        item_xml_parts.append(
            f"""    <item>
      <title>{escape(it['title'])}</title>
      <link>{escape(it['link'])}</link>
      <guid isPermaLink="false">{escape(it['guid'])}</guid>
      <pubDate>{it['pubDate']}</pubDate>
      <description>{escape(it['description'])}</description>
    </item>"""
        )
    items_xml = "\n".join(item_xml_parts)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{site_name}</title>
    <link>{site_url}</link>
    <description>{site_name} の更新を自動検知して生成したフィードです。</description>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
{items_xml}
  </channel>
</rss>
"""


def build_index_html(sites: list) -> str:
    rows = []
    for s in sites:
        feed_rel = f"feeds/{s['id']}.xml"
        feed_url = f"{PAGES_BASE_URL}/{feed_rel}" if PAGES_BASE_URL else feed_rel
        rows.append(
            f'<li><strong>{escape(s["name"])}</strong> — '
            f'<a href="{escape(s["url"])}">元ページ</a> / '
            f'<a href="{escape(feed_rel)}">フィードXML</a>'
            f'<br><code>{escape(feed_url)}</code></li>'
        )
    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>site-watcher フィード一覧</title>
<style>
body {{ font-family: sans-serif; max-width: 700px; margin: 2em auto; line-height: 1.6; }}
code {{ background: #f2f2f2; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>site-watcher フィード一覧</h1>
<p>下のフィードURLをInoreaderなどのRSSリーダーに登録してください。</p>
<ul>
{rows_html}
</ul>
<p><small>最終更新: {datetime.now(timezone.utc).isoformat()}</small></p>
</body>
</html>
"""


def main():
    sites = load_config()
    state = load_state()

    FEEDS_DIR.mkdir(parents=True, exist_ok=True)

    any_error = False

    for site in sites:
        site_id = site["id"]
        name = site.get("name", site_id)
        url = site["url"]
        mode = (site.get("mode") or "diff").strip()

        print(f"[check] {name} ({url}) mode={mode}")
        site_state = state.get(site_id, {})

        try:
            soup = fetch_soup(url)
            if mode == "list":
                site_state = process_list_site(site, soup, site_state)
            else:
                site_state = process_diff_site(site, soup, site_state)
        except Exception as e:  # noqa: BLE001
            print(f"  !! エラー: {e}", file=sys.stderr)
            any_error = True
            items = build_rss_items_from_state(site_state, mode)
            rss = build_rss(site, items)
            (FEEDS_DIR / f"{site_id}.xml").write_text(rss, encoding="utf-8")
            state[site_id] = site_state
            continue

        state[site_id] = site_state
        items = build_rss_items_from_state(site_state, mode)
        rss = build_rss(site, items)
        (FEEDS_DIR / f"{site_id}.xml").write_text(rss, encoding="utf-8")

    (DOCS_DIR / "index.html").write_text(build_index_html(sites), encoding="utf-8")
    save_state(state)

    if any_error:
        print("一部サイトの取得に失敗しましたが、処理を続行しました。", file=sys.stderr)


if __name__ == "__main__":
    main()
