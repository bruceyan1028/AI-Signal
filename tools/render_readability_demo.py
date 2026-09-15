from pathlib import Path
import html, json

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "output/extractor-readability-2026-09-14.json"
dst = ROOT / "output/extractor-readability-2026-09-14.html"
data = json.loads(src.read_text(encoding="utf-8"))

def pane(label, item):
    noise = ", ".join(item.get("noise") or []) or "无"
    preview = html.escape(item.get("preview") or "（未抽取到正文）")
    return f'<section class="pane"><h3>{html.escape(label)} <span>{item["chars"]:,} 字</span></h3><p class="meta">样板噪音：{html.escape(noise)}　JSON污染：{"有" if item.get("junk") else "无"}</p><pre>{preview}</pre></section>'

cards = []
for i, row in enumerate(data["rows"], 1):
    cards.append(f'<article><div class="title"><b>{i}. {html.escape(str(row.get("source") or ""))}</b><a href="{html.escape(row["url"])}" target="_blank">打开原文</a><h2>{html.escape(row["title"])}</h2></div><div class="grid">{pane("当前抽取器", row["current"])}{pane("Mozilla Readability", row["readability"])}</div></article>')

style = '*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#1f2937;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1200px;margin:0 auto;padding:28px 18px}h1{font-size:24px;margin:0 0 4px}.sub{color:#6b7280}article{background:#fff;border:1px solid #e5e7eb;border-radius:8px;margin:14px 0;overflow:hidden}.title{padding:14px 16px 10px;border-bottom:1px solid #eef0f2}.title b{color:#2563eb}.title a{float:right;color:#2563eb;text-decoration:none}h2{font-size:16px;line-height:1.4;margin:6px 0 0;font-weight:600}.grid{display:grid;grid-template-columns:1fr 1fr}.pane{padding:14px 16px;min-width:0}.pane+.pane{border-left:1px solid #eef0f2}h3{font-size:14px;margin:0 0 2px}h3 span{float:right;color:#111827}.meta{font-size:12px;color:#6b7280;margin:0 0 9px}pre{white-space:pre-wrap;word-break:break-word;margin:0;background:#f8fafc;border-radius:6px;padding:10px;color:#374151;font:13px/1.65 ui-monospace,monospace;min-height:90px}@media(max-width:720px){.grid{grid-template-columns:1fr}.pane+.pane{border-left:0;border-top:1px solid #eef0f2}}'
doc = f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>正文抽取对比 · 2026-09-14</title><style>{style}</style><main><header><h1>2026-09-14 正文抽取对比</h1><div class="sub">同一份 HTML · 当前抽取器 vs Mozilla Readability · 显示正文前 320 字</div></header>{"".join(cards)}</main>'
dst.write_text(doc, encoding="utf-8")
print(dst)
