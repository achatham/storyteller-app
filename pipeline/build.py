"""Assemble the chosen scene images + read-aloud text into a single HTML reader.

Each spread also exposes (in a collapsible panel) the exact image prompt and the
reference character-sheet thumbnails that were fed to the image model, so the
generation process is fully inspectable from the book itself.
"""
import base64
import html
import io
import json

from PIL import Image

from .config import OUT, REGISTRY, BOOK_AUTHOR
from .run import resolve_cast, member_sheet


def b64(path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def thumb_b64(path, width=150) -> str:
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=78)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    bible = json.loads((OUT / "bible.json").read_text())
    results = {r["id"]: r for r in json.loads((OUT / "results.json").read_text())["scenes"]}
    registry = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"entities": []}
    cast_index = resolve_cast(bible, registry)
    char_name = {k: m["name"] for k, m in cast_index.items()}

    # one small thumbnail per (entity,variant) sheet, reused across spreads
    ref_thumb = {}
    for key, m in cast_index.items():
        sheet = member_sheet(m)
        if sheet.exists():
            ref_thumb[key] = thumb_b64(sheet)

    spreads_html = []
    for s in bible["spreads"]:
        r = results.get(s["id"], {})
        img = OUT / "scenes" / f"scene_{s['id']:02d}.webp"
        score = r.get("score", "?")
        img_tag = (f'<img src="data:image/webp;base64,{b64(img)}" alt="{s["title"]}">'
                   if img.exists() else '<div class="missing">[no illustration]</div>')

        # references panel
        ref_ids = r.get("refs") or [
            f"{c['entity_id']}/{c.get('variant_id','default')}" for c in s.get("cast", [])]
        ref_imgs = "".join(
            f'<figure><img src="data:image/webp;base64,{ref_thumb[i]}">'
            f'<figcaption>{html.escape(char_name.get(i, i))}</figcaption></figure>'
            for i in ref_ids if i in ref_thumb)
        if not ref_imgs:
            ref_imgs = '<div class="noref">(no character references — generated from prompt only)</div>'
        prompt = r.get("prompt") or "(prompt not recorded for this scene)"
        attempt = r.get("attempt")
        crit = r.get("crit") or {}
        issues = crit.get("issues") if isinstance(crit, dict) else None
        issues_html = ("<div class='issues'><b>Critic notes:</b> "
                       + html.escape("; ".join(issues)) + "</div>") if issues else ""

        detail = f"""
        <details class="gen">
          <summary>&#9881; Prompt &amp; references{f" &middot; attempt {attempt}" if attempt else ""}</summary>
          <div class="refs">{ref_imgs}</div>
          {issues_html}
          <div class="promptlabel">Image prompt sent to Nano&nbsp;Banana&nbsp;2&nbsp;Pro:</div>
          <pre class="prompt">{html.escape(prompt)}</pre>
        </details>"""

        spreads_html.append(f"""
    <section class="spread">
      <div class="art">{img_tag}</div>
      <div class="text">
        <div class="pagenum">{s['id']}</div>
        <h2>{html.escape(s['title'])}</h2>
        <p>{html.escape(s['read_text'])}</p>
        <div class="meta">score {score} &middot; {s.get('content_note','')}</div>
        {detail}
      </div>
    </section>""")

    chars_html = []
    for key, m in cast_index.items():
        sheet = member_sheet(m)
        if sheet.exists():
            tag = "" if m["source"] == "registry" else " <small>(local)</small>"
            chars_html.append(
                f'<figure><img src="data:image/webp;base64,{b64(sheet)}">'
                f'<figcaption>{html.escape(m["name"])}{tag}</figcaption></figure>')

    html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(bible['title'])} — {html.escape(bible['chapter'])}</title>
<style>
  body {{ margin:0; font-family: Georgia, 'Times New Roman', serif; background:#f4efe6; color:#2b2622; }}
  header {{ text-align:center; padding:48px 16px 8px; }}
  header h1 {{ font-size:2.6rem; margin:0; }}
  header .sub {{ color:#8a7d6b; font-size:1.1rem; }}
  .gallery {{ display:flex; flex-wrap:wrap; gap:16px; justify-content:center; padding:24px; }}
  .gallery figure {{ margin:0; width:130px; text-align:center; }}
  .gallery img {{ width:100%; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,.15); }}
  .gallery figcaption {{ font-size:.85rem; color:#6b5f4f; margin-top:6px; }}
  .spread {{ display:flex; flex-wrap:wrap; align-items:flex-start; gap:32px; max-width:1100px;
             margin:0 auto 8px; padding:40px 24px; border-bottom:1px solid #e2d8c6; }}
  .spread:nth-child(even) {{ flex-direction:row-reverse; }}
  .art {{ flex:1 1 460px; }}
  .art img {{ width:100%; border-radius:12px; box-shadow:0 6px 24px rgba(0,0,0,.18); }}
  .text {{ flex:1 1 300px; }}
  .text .pagenum {{ font-size:.9rem; color:#b3a690; letter-spacing:2px; }}
  .text h2 {{ font-size:1.6rem; margin:.2em 0 .4em; }}
  .text p {{ font-size:1.25rem; line-height:1.7; }}
  .text .meta {{ font-size:.8rem; color:#b3a690; margin-top:12px; }}
  .missing {{ padding:80px; text-align:center; color:#b00; background:#fff; border-radius:12px; }}
  h3.section {{ text-align:center; color:#8a7d6b; margin-top:32px; }}
  details.gen {{ margin-top:16px; background:#efe7d8; border:1px solid #e0d4bd; border-radius:8px; padding:6px 12px; }}
  details.gen summary {{ cursor:pointer; font-size:.85rem; color:#7a6a52; font-family:system-ui,sans-serif; }}
  .refs {{ display:flex; gap:10px; flex-wrap:wrap; margin:12px 0; }}
  .refs figure {{ margin:0; width:80px; text-align:center; }}
  .refs img {{ width:100%; border-radius:6px; border:1px solid #d8cbb2; }}
  .refs figcaption {{ font-size:.7rem; color:#7a6a52; }}
  .noref {{ font-size:.8rem; color:#9a8a70; font-style:italic; margin:8px 0; }}
  .promptlabel {{ font-size:.75rem; color:#7a6a52; font-family:system-ui,sans-serif; margin-top:8px; }}
  pre.prompt {{ white-space:pre-wrap; font-size:.78rem; line-height:1.45; background:#fffdf8;
                border:1px solid #e0d4bd; border-radius:6px; padding:10px; color:#3a342b;
                font-family:ui-monospace,Menlo,Consolas,monospace; max-height:340px; overflow:auto; }}
  .issues {{ font-size:.8rem; color:#8a5a30; margin:4px 0; font-family:system-ui,sans-serif; }}
</style></head><body>
<header>
  <h1>{html.escape(bible['title'])}</h1>
  <div class="sub">{html.escape(bible['chapter'])} &mdash; an illustrated read-aloud edition</div>
</header>
<h3 class="section">The Characters</h3>
<div class="gallery">{''.join(chars_html)}</div>
<h3 class="section">The Story</h3>
{''.join(spreads_html)}
<footer style="text-align:center;padding:40px;color:#b3a690;">
  Illustrations generated with Gemini 3 Pro Image (Nano Banana 2 Pro).{f" Text &copy; {html.escape(BOOK_AUTHOR)}." if BOOK_AUTHOR else ""}
</footer>
</body></html>"""

    out = OUT / "book.html"
    out.write_text(html_doc)
    print(f"wrote {out} ({len(html_doc)//1024} KB)")


if __name__ == "__main__":
    main()
