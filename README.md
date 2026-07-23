# site-watcher

指定したWebページの更新を自動検知して、サイトごとにRSSフィード（XML）を生成する
自分専用ツールです。GitHub Actions（無料）で定期チェックし、GitHub Pages（無料）で
フィードを公開します。生成されたフィードURLをInoreaderなどのRSSリーダーに登録すれば、
「新規フィード作成が有料」という制限を回避できます。

料金: 完全無料（GitHubの無料枠の範囲内。パブリックリポジトリならActionsの実行時間も無制限）

---

## セットアップ手順

### 1. GitHubアカウントを用意する
すでにお持ちならスキップしてください。無料です。

### 2. 新しいリポジトリを作る
GitHub上で「New repository」から `site-watcher` などの名前でリポジトリを作成します
（Public / Private どちらでも可。Privateの場合もPagesは公開設定にできます）。

### 3. このフォルダの中身をアップロードする
今回渡したファイル一式（`config/`, `scripts/`, `.github/`, `requirements.txt`,
`README.md`）を、そのリポジトリの直下にそのままアップロード（もしくは `git push`）します。

GitHub Desktop や、Web上の「Add file → Upload files」からドラッグ&ドロップでもOKです。

### 4. 監視したいサイトを設定する
`config/sites.yaml` を編集し、監視したいURLを追加します。

```yaml
- id: my_target_site
  name: "○○社のお知らせページ"
  url: "https://example.com/news"
  selector: ""   # 特定の部分だけ見たい場合はCSSセレクタを指定
```

- `id` はファイル名になるので半角英数字のみ
- `selector` は省略可（省略時はページ本文全体を比較対象にします）
- セレクタの調べ方: Chromeでページを開く →
  監視したい部分を右クリック →「検証」→ ハイライトされた要素を右クリック →
  Copy → Copy selector

複数サイトをリストにどんどん追加できます。

### 5. GitHub Pagesを有効にする
リポジトリの Settings → Pages →
- Source: 「Deploy from a branch」
- Branch: `main` / フォルダ: `/docs`
を選んで Save。

数分待つと `https://<あなたのユーザー名>.github.io/site-watcher/` が公開されます。

### 6.（任意）PAGES_BASE_URL を設定する
Settings → Secrets and variables → Actions → Variables タブ →
`PAGES_BASE_URL` という名前で `https://<あなたのユーザー名>.github.io/site-watcher` を登録すると、
一覧ページ (`docs/index.html`) にフィードのフルURLが表示されて分かりやすくなります
（設定しなくても動作自体には影響ありません）。

### 7. Actionsを有効化・実行する
リポジトリの Actions タブを開き、ワークフローを有効化します。
初回は「Run workflow」ボタンから手動実行して、正常に動くか確認してください。
以降は30分ごとに自動実行されます（頻度は `.github/workflows/check-updates.yml` の
`cron` を編集すれば変更可能）。

### 8. Inoreaderにフィードを登録する
実行後、`https://<あなたのユーザー名>.github.io/site-watcher/feeds/<id>.xml`
というURLが生成されます（`docs/index.html` を開くと一覧とリンクが確認できます）。
このURLをInoreaderの「フィード購読」に通常のRSS URLとして登録すれば完了です。
Inoreader側では既存フィードの購読なので、追加料金はかかりません。

---

## 仕組みの概要

1. GitHub Actionsが cron で定期的に `scripts/check_sites.py` を実行
2. 各サイトを取得し、指定範囲のテキストをハッシュ化して前回と比較
3. 変化があれば新しいアイテムとして `docs/feeds/<id>.xml` に追記
4. 変更差分（`state.json`, `docs/`）を自動でリポジトリにコミット
5. GitHub Pagesが `docs/` を自動で再公開 → フィードURLの中身が更新される

## 制限・注意点

- JavaScriptで動的にレンダリングされるサイト（SPAなど）は、素の `requests` では
  正しく取得できない場合があります。その場合は `selector` を調整するか、
  必要であれば Playwright 等を使った拡張が必要です（今回はまず最小構成にしています）。
- サイト側の規約によってはスクレイピングが禁止されている場合があります。
  個人の利用範囲・利用規約の範囲内でご利用ください。
- 更新頻度（cron）を上げすぎるとサイトに負荷をかける可能性があるので、
  必要以上に短い間隔にしないことをおすすめします。
