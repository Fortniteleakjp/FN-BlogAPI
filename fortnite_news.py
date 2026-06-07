# -*- coding: utf-8 -*-
"""
Fortnite ニュース API (Remix single-fetch / turbo-stream 形式) を解析して
各記事の「タイトル・リンク・画像・タグ」を取り出し JSON で出力するスクリプト。

対象API:
    https://www.fortnite.com/news/tag/all-news.data

このエンドポイントは通常の JSON ではなく、Remix の single-fetch 形式
（フラットな配列＋インデックス参照）でエンコードされている。
  - ペイロード全体は 1 個のフラットな配列。
  - オブジェクトは {"_K": V, ...} の形。
        キー名 = 配列[K] の文字列 / 値 = 配列[V] を再帰的に展開したもの。
  - 配列 [a, b, c] の各要素も配列インデックスへの参照。
  - 負の数 (-1, -5 など) は undefined/null 等を表すセンチネル。
本スクリプトはこの形式を汎用的に復元してから必要フィールドを抽出する。

言語は URL ではなく Accept-Language ヘッダで切り替わる（既定は日本語 ja）。

使い方:
    python fortnite_news.py                     # 日本語(ja)で取得し fortnite_news.json に保存
    python fortnite_news.py --lang en           # 英語(en)で取得
    python fortnite_news.py --out articles.json # 出力先を指定
    python fortnite_news.py --url <.data のURL> # 別タグ等のURLを指定
    python fortnite_news.py --file all-news.data# 取得済みファイルから解析（オフライン）

標準ライブラリのみで動作（追加インストール不要）。
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
import urllib.request
from urllib.parse import urljoin

DEFAULT_URL = "https://www.fortnite.com/news/tag/all-news.data"
SITE_BASE = "https://www.fortnite.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# 取得
# --------------------------------------------------------------------------- #
def fetch(url: str, lang: str = "ja-JP,ja;q=0.9", timeout: int = 30) -> str:
    """API から生テキストを取得する。

    言語は URL ではなく ``Accept-Language`` ヘッダで切り替わる
    （例: ``ja-JP,ja;q=0.9`` → 日本語、``en-US,en;q=0.9`` → 英語）。

    また fortnite.com は Akamai 系 WAF が TLS フィンガープリントと
    User-Agent を検査しており、短い UA や Python の urllib は 403 になる。
    Windows 10/11 や多くの環境に標準搭載の curl は許可されるため、curl を
    優先し、無ければフル UA を付けた urllib にフォールバックする。
    """
    curl = shutil.which("curl")
    if curl:
        try:
            out = subprocess.run(
                [curl, "-sSL", "--compressed",
                 "-A", USER_AGENT,
                 "-H", "Accept-Language: " + lang,
                 url],
                capture_output=True, timeout=timeout, check=True,
            )
            text = out.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except (subprocess.SubprocessError, OSError):
            pass  # urllib にフォールバック

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": lang,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# --------------------------------------------------------------------------- #
# Remix single-fetch (turbo-stream) デコーダ
# --------------------------------------------------------------------------- #
def decode_remix_payload(text: str):
    """フラット配列＋インデックス参照の形式を通常の Python オブジェクトに復元。"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 複数行ストリームの場合は先頭行（メインのペイロード）を採用
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        parsed = json.loads(first)

    cache = {}  # index -> 復元済みオブジェクト（共有参照・循環参照に対応）

    def hydrate(index):
        if not isinstance(index, int):
            return index
        if index < 0:                 # -1=undefined, -5=-Infinity 等のセンチネル
            return None
        if index in cache:
            return cache[index]

        value = parsed[index]

        # オブジェクト: {"_K": V}  →  {配列[K]: hydrate(V)}
        if isinstance(value, dict):
            obj = {}
            cache[index] = obj        # 先に登録して循環に備える
            for raw_key, raw_val in value.items():
                key_name = parsed[int(str(raw_key).lstrip("_"))]
                obj[key_name] = hydrate(raw_val) if isinstance(raw_val, int) else raw_val
            return obj

        # 配列
        if isinstance(value, list):
            # 先頭が文字列なら型付き値 (Date='D' 等)。中身はそのまま返す。
            if value and isinstance(value[0], str):
                cache[index] = value
                return value
            arr = []
            cache[index] = arr
            for elem in value:
                arr.append(hydrate(elem) if isinstance(elem, int) else elem)
            return arr

        # プリミティブ
        cache[index] = value
        return value

    return hydrate(0)


# --------------------------------------------------------------------------- #
# ニュース項目の取り出し
# --------------------------------------------------------------------------- #
def find_news_items(root):
    """復元済みデータから newsItems(記事リスト) を取得する。"""
    # 既知のパスを優先
    try:
        return root["routes/news.tag.$tag"]["data"]["recentNewsItems"]["newsItems"]
    except (KeyError, TypeError):
        pass

    # フォールバック: "heading" と "link"/"slug" を持つ dict のリストを再帰探索
    def walk(node):
        if isinstance(node, dict):
            if "newsItems" in node and isinstance(node["newsItems"], list):
                return node["newsItems"]
            for v in node.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            if node and isinstance(node[0], dict) and "heading" in node[0]:
                return node
            for v in node:
                found = walk(v)
                if found:
                    return found
        return None

    return walk(root) or []


def extract_articles(items):
    """記事ごとに「タイトル・リンク・画像・タグ」を抽出して整形する。"""
    articles = []
    for it in items:
        if not isinstance(it, dict):
            continue
        link = it.get("link") or it.get("slug") or ""
        tags = [
            {"tagId": t.get("tagId"), "label": t.get("label")}
            for t in (it.get("tags") or [])
            if isinstance(t, dict)
        ]
        articles.append({
            "title": (it.get("heading") or "").strip(),
            "link": urljoin(SITE_BASE, link) if link else None,
            "image": it.get("imgSrc"),
            "imageAlt": it.get("imgAlt") or "",
            "tags": tags,
            "date": it.get("date"),
        })
    return articles


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fortnite ニュース .data を解析しタイトル/リンク/画像/タグを JSON 出力する"
    )
    ap.add_argument("--url", default=DEFAULT_URL, help="取得する .data エンドポイントURL")
    ap.add_argument("--lang", default="ja",
                    help="言語（Accept-Language）。例: ja, en, ja-JP, en-US。既定は ja(日本語)")
    ap.add_argument("--file", help="取得済みの .data ファイルから解析（指定時は --url を無視）")
    ap.add_argument("--out", default="fortnite_news.json", help="出力JSONファイル名")
    ap.add_argument("--indent", type=int, default=2, help="JSON のインデント幅")
    ap.add_argument("--stdout", action="store_true", help="ファイルに保存せず標準出力に出す")
    args = ap.parse_args(argv)

    # 簡易指定(ja / en)を正式な Accept-Language 文字列に展開
    lang_aliases = {
        "ja": "ja-JP,ja;q=0.9",
        "ja-jp": "ja-JP,ja;q=0.9",
        "en": "en-US,en;q=0.9",
        "en-us": "en-US,en;q=0.9",
    }
    accept_language = lang_aliases.get(args.lang.lower(), args.lang)

    # 日本語Windowsコンソールでの文字化け対策（出力はUTF-8に統一）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    # データ取得
    if args.file:
        with io.open(args.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = fetch(args.url, lang=accept_language)

    # 解析 → 抽出
    root = decode_remix_payload(text)
    items = find_news_items(root)
    articles = extract_articles(items)

    # 実際に返ってきたロケール（記事の locale フィールドから判定）
    locale = next((it.get("locale") for it in items
                   if isinstance(it, dict) and it.get("locale")), None)

    result = {
        "source": args.file or args.url,
        "locale": locale,
        "count": len(articles),
        "articles": articles,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=args.indent)

    # 出力
    if args.stdout:
        print(payload)
    else:
        with io.open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"{len(articles)} 件の記事を {args.out} に保存しました。", file=sys.stderr)
        # 確認用に先頭1件を表示
        if articles:
            print(json.dumps(articles[0], ensure_ascii=False, indent=2), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
