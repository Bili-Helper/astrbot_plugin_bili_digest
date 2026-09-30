"""Read and export the archive without importing AstrBot or modifying SQLite."""

import argparse
import html
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="查看或导出省流归档（本地管理员工具）")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--scope", help="仅显示指定 AstrBot unified_msg_origin")
    parser.add_argument("--query", default="")
    parser.add_argument("--format", choices=["list", "jsonl", "html"], default="list")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = (args.data_dir / "archive.sqlite3").resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        docs = [
            json.loads(r[0])
            for r in db.execute("SELECT document FROM runs ORDER BY created_at DESC")
        ]
    finally:
        db.close()
    docs = [
        d
        for d in docs
        if (not args.scope or d["scope"] == args.scope)
        and args.query.casefold() in json.dumps(d, ensure_ascii=False).casefold()
    ]
    if args.format == "jsonl":
        output = "\n".join(json.dumps(d, ensure_ascii=False) for d in docs)
    elif args.format == "html":
        cards = []
        for d in docs:
            title = (d.get("metadata") or {}).get("title", d["url"])
            transcript = "\n".join(
                str(s.get("text", s.get("content", ""))) for s in d.get("segments", [])
            )
            cards.append(
                "<article><h2>"
                + html.escape(title)
                + "</h2><p>"
                + html.escape(d["status"] + " | " + d["id"] + " | " + d["scope"])
                + "</p><pre>"
                + html.escape(d.get("summary", d.get("error", "")))
                + "</pre><details><summary>元数据与转录</summary><pre>"
                + html.escape(
                    json.dumps(d.get("metadata", {}), ensure_ascii=False, indent=2)
                    + "\n\n"
                    + transcript
                )
                + "</pre></details></article>"
            )
        output = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            "<title>视频省流归档</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;"
            "background:#f3f5f7;color:#202830}article{background:white;padding:24px;margin:20px 0;border-radius:12px}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}p{color:#566}summary{cursor:pointer}</style>"
            "<h1>视频省流归档</h1><p>本地静态导出，可用浏览器搜索标题和内容。</p>"
            + "".join(cards)
            + "</html>"
        )
    else:
        output = "\n".join(
            f"{d['id'][:8]} {d['status']:12} {(d.get('metadata') or {}).get('title', d['url'])}"
            for d in docs
        )
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
