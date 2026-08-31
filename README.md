# pharma-news

薬剤師の実務向けニュースを収集して、毎朝 Telegram に流すためのデータ生成リポジトリ。

閲覧用の Web UI は持たない。GitHub Actions が3時間おきに記事を集めて `data/news.json` を更新し、
hermes-vps 側の `pharma_digest.py` がそれを読んで未送信分だけ Telegram に送る。

## なぜ [RSS_news](https://github.com/ractodaisuki/RSS_news) と別なのか

汎用ニュースの `RSS_news` に薬のフィードを足す案を先に検討して、やめた理由:

- `fetch_rss.py` に汎用ニュース向けの重みづけが約200行ある（`GAME_TAG_PRIORITY`、
  `KTAI_WATCH_PRIORITY_TITLE_MARKERS`、`車中泊` 系の `FOCUS_TAGS`、`TAG_EXCLUDED_DOMAINS` など）。
  薬の記事もこの土俵に乗ってしまう。
- `RSS_news` の `news.json` は1回に300件。薬の記事は埋もれる。実際に確認した時点で
  `医療・薬` タグは300件中4件だった。
- **重要度が Gemini の判定ではなかった。** `apply_gemini_category_tags()` はタグしか反映せず、
  `news.json` の `importance` は `calc_importance()` のヒューリスティック値。Gemini の
  `importance` は `analytics.json` に保存されるだけで表示に使われていない。
  薬の記事が★3に張り付き、朝の digest（★4以上）に乗らない原因がこれ。

このリポジトリでは取得まわりの機械部分だけ移植し、評価軸を薬局実務に振り直している。
**`importance` は Gemini の判定をそのまま使う。**

## 構成

```
feeds.json                  RSS フィード定義（include_keywords で絞り込み可）
config/tag_rules.json       キーワード → タグ。並び順が実務の緊急度順
config/watch_sites.json     RSS がないサイトの CSS セレクタ監視
scripts/fetch_news.py       RSS 取得 + Gemini 解析 → data/news.json
scripts/check_websites.py   Web 監視 → data/news.json に追記
deploy/pharma_digest.py     VPS 側。news.json → Telegram（VPS へコピーして使う）
data/news.json              収集結果（最大200件）
data/analysis_cache.json    Gemini 解析のキャッシュ。同じ記事を二度解析しない
data/watch_state.json       Web 監視のハッシュ
data/status.json            最終実行の成否
```

## 情報源

| 種別 | 名前 | 備考 |
|---|---|---|
| RSS | 薬事日報 | 行政情報・プレスリリース |
| RSS | ミクスOnline | `/feed` は302。`DesktopModules/...rssmode=3` が実体 |
| RSS | 日経メディカル Online | 日経DI の記事もここに流れる |
| RSS | CareNet.com 医療ニュース | |
| RSS | 厚生労働省 新着情報 | **省全体**が流れるので `include_keywords` 必須 |
| Web監視 | m3.com 薬剤師コラム | RSS なし。CSS セレクタで取得 |

**PMDA は RSS を廃止している。** 回収・添付文書改訂・安全性速報を最速で取るなら
[PMDAメディナビ](https://www.pmda.go.jp/safety/info-services/0001.html)（無料メール配信）の登録が必要で、
これはこのリポジトリの範囲外。

m3.com の DI Station と薬剤師掲示板はログイン必須のため取得できない。

## 重要度の基準

Gemini に「保険薬局の薬剤師にとって、明日の業務がどれだけ変わるか」で判定させている。

| ★ | 基準 |
|---|---|
| 5 | 今日から行動が変わる。自主回収、イエローレター、ブルーレター、汎用薬の供給停止、禁忌の新設 |
| 4 | 近いうちに影響が出る。限定出荷の開始/解除、添付文書の重要改訂、薬価・調剤報酬の変更、汎用薬の承認 |
| 3 | 知っておくと役立つ。一般的な承認、学術情報、業界動向、実務コラム |
| 2 | 背景知識どまり。総論的な解説、イベント告知 |
| 1 | 実務に無関係。広告、PR、決算数値のみ |

`GEMINI_API_KEY` が無いときは `calc_fallback_importance()` の近似スコアになる。
こちらはタイトルに回収・緊急安全性情報などが出たときだけ★5にして、それ以外は★4止まりにしてある。

## セットアップ

1. リポジトリの Secrets に `GEMINI_API_KEY` を登録する（`RSS_news` と同じキーで良い）
2. Actions を有効にする。3時間おきに動く
3. VPS へ digest を配置する

```bash
scp deploy/pharma_digest.py hermes-vps:/tmp/
ssh hermes-vps 'sudo install -o hermes -g hermes -m 755 /tmp/pharma_digest.py /opt/data/scripts/'
# 毎朝 8:30 JST
ssh hermes-vps "sudo crontab -u hermes -l"   # 既存を確認してから追記する
```

## ローカルで試す

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/fetch_news.py       # GEMINI_API_KEY 無しでも動く
./.venv/bin/python scripts/check_websites.py
python3 deploy/pharma_digest.py --no-send      # 送らずに内容だけ見る
```

## 情報源を足すとき

- RSS があるなら `feeds.json` に `{"name", "url"}` を足すだけ。
  雑多なフィードなら `include_keywords` で絞る
- RSS がないなら `config/watch_sites.json` に CSS セレクタを書く。
  `item_selector` が一覧の1件、その中で `title_selector` / `link_selector` / `summary_selector` を指定する。
  初回から記事を出したいときは `emit_on_initialize: true`
- タグを増やすときは `config/tag_rules.json` と `scripts/fetch_news.py` の `CATEGORIES` の**両方**を直す。
  片方だけだと Gemini が返したカテゴリが `その他` に落ちる
