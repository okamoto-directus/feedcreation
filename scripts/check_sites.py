#!/usr/bin/env python3
"""
site-watcher: 指定したWebページの変更を検知して、サイトごとにRSSフィードを生成する。

流れ:
  1. config/sites.yaml から監視対象を読み込む
  2. 各サイトを取得し、指定セレクタ（無ければ本文全体）のテキストを抽出
  3. 前回のハッシュ（state.json）と比較し、変わっていれば「更新イベント」を記録
  4. サイトごとに docs/feeds/<id>.xml というRSSフィードを書き出す
  5. docs/index.html に一覧ページを生成する

GitHub Actions から定期実行される想定。state.json と docs/ はリポジトリにコミットされ、
docs/ が GitHub Pages で公開されることで、生成されたXMLが外部からURLで読めるようになる。
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sites.yaml"
STATE_PATH = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
FEEDS_DIR = DOCS_DIR / "feeds"

# フィードに残しておく最大アイテム数（サイトごと）
MAX_ITEMS = 30

# GitHub PagesのベースURL。ワークフロー実行時に環境変数から上書きされる。
import os
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; site-watcher/1.0; "
        "+https://github.com/) personal RSS generator"
    )
}


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


def fetch_text(url: str, selector: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # scriptやstyleは比較対象から除外（中身が変わってもページの見た目は変わらないため）
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    if selector:
        node = soup.select_one(selector)
        if node is None:
            raise ValueError(f"セレクタ '{selector}' に一致する要素が見つかりません")
        text = node.get_text("\n", strip=True)
    else:
        body = soup.body or soup
        text = body.get_text("\n", strip=True)

    # 空白行を整理
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_rss(site: dict, items: list) -> str:
    """itemsは新しい順（先頭が最新）のリスト。各itemは{title, link, pubDate, description}"""
    now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    site_name = escape(site["name"])
    site_url = escape(site["url"])

    item_xml_parts = []
    for it in items[:MAX_ITEMS]:
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
    <title>{site_name}（更新監視）</title>
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
        selector = (site.get("selector") or "").strip()

        print(f"[check] {name} ({url})")
        site_state = state.get(site_id, {"hash": None, "items": []})

        try:
            text = fetch_text(url, selector)
        except Exception as e:  # noqa: BLE001
            print(f"  !! 取得エラー: {e}", file=sys.stderr)
            any_error = True
            # 取得エラーでも既存フィードは保持して再生成だけしておく
            rss = build_rss(site, site_state.get("items", []))
            (FEEDS_DIR / f"{site_id}.xml").write_text(rss, encoding="utf-8")
            continue

        new_hash = text_hash(text)
        old_hash = site_state.get("hash")

        if old_hash is None:
            # 初回実行: ベースラインとして保存するだけ。アイテムは追加しない。
            print("  -> 初回実行のため、ベースラインとして保存します。")
        elif new_hash != old_hash:
            print("  -> 変更を検知しました。フィードに追加します。")
            now = datetime.now(timezone.utc)
            snippet = text[:400].replace("\n", " ")
            item = {
                "title": f"{name} が更新されました（{now.strftime('%Y-%m-%d %H:%M UTC')}）",
                "link": url,
                "guid": f"{site_id}-{new_hash[:12]}-{int(now.timestamp())}",
                "pubDate": now.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "description": snippet,
            }
            site_state.setdefault("items", []).insert(0, item)
            site_state["items"] = site_state["items"][:MAX_ITEMS]
        else:
            print("  -> 変更なし。")

        site_state["hash"] = new_hash
        state[site_id] = site_state

        rss = build_rss(site, site_state.get("items", []))
        (FEEDS_DIR / f"{site_id}.xml").write_text(rss, encoding="utf-8")

    (DOCS_DIR / "index.html").write_text(build_index_html(sites), encoding="utf-8")
    save_state(state)

    if any_error:
        # エラーがあってもワークフロー全体は失敗させない（一部サイトの一時的な障害を許容）
        print("一部サイトの取得に失敗しましたが、処理を続行しました。", file=sys.stderr)


if __name__ == "__main__":
    main()
