"""Build a single-page read-aloud reader app from generated section(s).

Full-screen reader: one illustration + its text at a time. Navigate by TAPPING
(left third = back, right third = forward; faint edge chevrons hint this), or
arrows / swipe. A small gear button opens a menu with chapter + style pickers,
page position, and fallback buttons. Position is saved in localStorage. All
styles share the same text/pages; switching style just swaps the image folder.

    python3 -m pipeline.reader [book_root] [out_html]
    # default: output/dawn_treader  ->  output/dawn_treader/reader.html

Images are referenced by relative path, so serve the book_root with serve.py.
"""
import json
import sys
from pathlib import Path


def load_sections(root: Path):
    """Discover style dirs (each has a bible.json) and assemble pages. All
    styles share the same text/pages; we read from a canonical style and expose
    the style list for swapping the image folder."""
    style_dirs = sorted(
        d for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "bible.json").exists())
    if not style_dirs:
        raise SystemExit(f"no style dirs with bible.json under {root}")
    styles = [d.name for d in style_dirs]
    canonical = next((d for d in style_dirs if d.name == "watercolor"), style_dirs[0])

    bible = json.loads((canonical / "bible.json").read_text())
    title = bible.get("title", root.name)
    chapter = bible.get("chapter", "")
    pages = []
    for s in sorted(bible.get("spreads", []), key=lambda x: x["id"]):
        pages.append({
            "id": s["id"], "chapter": chapter, "title": s.get("title", ""),
            "text": s.get("read_text", ""), "scene": f"scenes/scene_{s['id']:02d}.webp",
        })
    return title, styles, canonical.name, pages


def build_html(title: str, styles: list[str], default_style: str, pages: list[dict]) -> str:
    data = json.dumps({"title": title, "styles": styles,
                       "defaultStyle": default_style, "pages": pages},
                      ensure_ascii=False)
    # f-string only for the single {data} injection; all CSS/JS braces doubled.
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{title} — read-aloud</title>
<style>
  :root {{ --bg:#f4efe6; --ink:#2b2622; --muted:#9a8a70; --panel:#fffdf8; --line:#e0d4bd; }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; height:100%; background:var(--bg); color:var(--ink);
               font-family:Georgia,'Times New Roman',serif; -webkit-tap-highlight-color:transparent; }}
  #app {{ position:relative; height:100dvh; overflow:hidden; }}

  main {{ position:absolute; inset:0; display:flex; gap:24px; padding:24px; min-height:0; }}
  .art {{ display:flex; align-items:center; justify-content:center; }}
  .art img {{ max-width:100%; border-radius:14px; box-shadow:0 8px 30px rgba(0,0,0,.22); background:#fff; }}
  .read .ch {{ font-size:.75rem; letter-spacing:2px; text-transform:uppercase; color:var(--muted);
               font-family:system-ui,sans-serif; }}
  .read h2 {{ font-size:1.5rem; margin:.15em 0 .5em; }}
  .read p {{ font-size:clamp(1.15rem, 1rem + 1vw, 1.7rem); line-height:1.75; margin:0 0 1.2em; }}

  /* Landscape: image on the LEFT, words on the RIGHT; words scroll if long. */
  @media (orientation:landscape) {{
    main {{ flex-direction:row; align-items:stretch; justify-content:center; overflow:hidden; }}
    .art {{ flex:1 1 55%; height:100%; }}
    .art img {{ max-height:100%; }}
    .read {{ flex:1 1 45%; max-width:640px; max-height:100%; overflow:auto; padding-right:4px; }}
  }}

  /* Portrait: image PINNED at the top (stays put) while the words scroll. */
  @media (orientation:portrait) {{
    main {{ flex-direction:column; align-items:stretch; gap:14px; padding:16px; overflow-y:auto;
            -webkit-overflow-scrolling:touch; }}
    .art {{ position:sticky; top:0; z-index:2; flex:none; background:var(--bg); padding-bottom:10px; }}
    .art img {{ max-height:52vh; }}
    .read {{ flex:none; padding-bottom:40vh; }}
  }}

  /* faint tap-to-navigate hints */
  .edge {{ position:fixed; top:50%; transform:translateY(-50%); z-index:5; pointer-events:none;
           font-size:2.4rem; color:rgba(43,38,34,.16); font-family:system-ui,sans-serif; user-select:none; }}
  .edge.left {{ left:10px; }}  .edge.right {{ right:10px; }}

  /* gear button + slide-in menu */
  #menuBtn {{ position:fixed; top:10px; left:10px; z-index:30; width:40px; height:40px; border-radius:50%;
              border:1px solid var(--line); background:rgba(255,253,248,.82); color:var(--ink);
              font-size:1.15rem; line-height:1; cursor:pointer; backdrop-filter:blur(3px); }}
  #menu {{ position:fixed; inset:0; z-index:40; background:rgba(0,0,0,.4);
           display:flex; align-items:center; justify-content:center; }}
  #menu[hidden] {{ display:none; }}
  .menucard {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:20px;
               width:min(360px,88vw); display:flex; flex-direction:column; gap:16px;
               box-shadow:0 12px 40px rgba(0,0,0,.3); font-family:system-ui,sans-serif; }}
  .menucard .bk {{ font-weight:bold; font-size:1.05rem; font-family:Georgia,serif; }}
  .menucard label {{ display:flex; flex-direction:column; gap:5px; font-size:.72rem;
                     letter-spacing:1px; text-transform:uppercase; color:var(--muted); }}
  .menucard select {{ font:inherit; font-size:1rem; padding:9px; border:1px solid var(--line);
                      border-radius:8px; background:var(--bg); color:var(--ink); }}
  .menucard .row {{ display:flex; align-items:center; gap:12px; }}
  .menucard button {{ font:inherit; font-size:1rem; padding:10px 18px; cursor:pointer;
                      background:var(--bg); border:1px solid var(--line); border-radius:10px; color:var(--ink); }}
  .menucard button:disabled {{ opacity:.35; }}
  .pos {{ margin:0 auto; color:var(--muted); font-size:.9rem; }}
  #close {{ width:100%; }}

  #bar {{ position:fixed; left:0; bottom:0; height:3px; background:var(--muted); z-index:20;
          transition:width .15s ease; }}
</style>
</head><body>
<div id="app">
  <button id="menuBtn" aria-label="Menu">&#9776;</button>
  <main>
    <div class="art"><img id="img" alt=""></div>
    <div class="read">
      <div class="ch" id="ch"></div>
      <h2 id="title"></h2>
      <p id="text"></p>
    </div>
  </main>
  <div class="edge left">&#8249;</div>
  <div class="edge right">&#8250;</div>
  <div id="bar"></div>

  <div id="menu" hidden>
    <div class="menucard">
      <div class="bk" id="bk"></div>
      <label>Chapter <select id="chap"></select></label>
      <label>Style <select id="style"></select></label>
      <div class="row">
        <button id="prev">&#8249; Back</button>
        <span class="pos" id="pos"></span>
        <button id="next">Next &#8250;</button>
      </div>
      <button id="fs">&#9974; Full screen</button>
      <button id="close">Close</button>
    </div>
  </div>
</div>
<script>
const DATA = {data};
const KEY = "reader:" + DATA.title;
const $ = id => document.getElementById(id);

let state = {{ i:0, style:DATA.defaultStyle }};
try {{ const s = JSON.parse(localStorage.getItem(KEY)); if (s) state = Object.assign(state, s); }} catch(e){{}}
if (!DATA.styles.includes(state.style)) state.style = DATA.defaultStyle;
state.i = Math.max(0, Math.min(state.i, DATA.pages.length - 1));

const chapters = [];
DATA.pages.forEach((p, idx) => {{
  if (!chapters.length || chapters[chapters.length-1].label !== p.chapter)
    chapters.push({{ label:p.chapter, i:idx }});
}});
$("bk").textContent = DATA.title;
$("chap").innerHTML = chapters.map(c => `<option value="${{c.i}}">${{c.label}}</option>`).join("");
$("style").innerHTML = DATA.styles.map(s => `<option value="${{s}}">${{s.replace(/_/g,' ')}}</option>`).join("");

function save(){{ localStorage.setItem(KEY, JSON.stringify(state)); }}

function render(){{
  const p = DATA.pages[state.i];
  const img = $("img");
  img.src = state.style + "/" + p.scene;
  img.alt = p.title;
  $("ch").textContent = p.chapter;
  $("title").textContent = p.title;
  $("text").textContent = p.text;
  $("pos").textContent = `Page ${{state.i+1}} / ${{DATA.pages.length}}`;
  $("prev").disabled = state.i === 0;
  $("next").disabled = state.i === DATA.pages.length - 1;
  $("bar").style.width = ((state.i+1)/DATA.pages.length*100) + "%";
  let cur = chapters[0].i;
  for (const c of chapters) if (c.i <= state.i) cur = c.i;
  $("chap").value = cur;
  $("style").value = state.style;
  document.querySelector("main").scrollTop = 0;
  const r = document.querySelector(".read"); if (r) r.scrollTop = 0;
  save();
}}

function go(n){{ state.i = Math.max(0, Math.min(DATA.pages.length-1, state.i+n)); render(); }}
function openMenu(){{ $("menu").hidden = false; }}
function closeMenu(){{ $("menu").hidden = true; }}

$("menuBtn").onclick = e => {{ e.stopPropagation(); openMenu(); }};
$("menu").onclick = e => {{ if (e.target === $("menu")) closeMenu(); e.stopPropagation(); }};
$("close").onclick = e => {{ e.stopPropagation(); closeMenu(); }};

const fsEl = document.documentElement;
function fsSupported(){{ return !!(fsEl.requestFullscreen || fsEl.webkitRequestFullscreen); }}
function inFS(){{ return document.fullscreenElement || document.webkitFullscreenElement; }}
function toggleFS(){{
  try {{
    if (inFS()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else (fsEl.requestFullscreen || fsEl.webkitRequestFullscreen).call(fsEl);
  }} catch(e) {{}}
}}
function syncFS(){{ $("fs").textContent = (inFS() ? "\\u26F6 Exit full screen" : "\\u26F6 Full screen"); }}
if (!fsSupported()) $("fs").style.display = "none";   // e.g. iOS Safari -> use Add to Home Screen
$("fs").onclick = e => {{ e.stopPropagation(); toggleFS(); }};
document.addEventListener("fullscreenchange", syncFS);
document.addEventListener("webkitfullscreenchange", syncFS);
$("prev").onclick = e => {{ e.stopPropagation(); go(-1); }};
$("next").onclick = e => {{ e.stopPropagation(); go(1); }};
$("chap").onchange = e => {{ state.i = +e.target.value; render(); }};
$("style").onchange = e => {{ state.style = e.target.value; render(); }};

// tap zones: left third = back, right third = forward, center = (reading).
// `moved` suppresses the click that follows a scroll/swipe so only true taps nav.
let tx=null, ty=null, moved=false;
document.addEventListener("touchstart", e => {{
  tx=e.touches[0].clientX; ty=e.touches[0].clientY; moved=false;
}}, {{passive:true}});
document.addEventListener("touchmove", e => {{
  if (tx!==null && (Math.abs(e.touches[0].clientX-tx)>10 || Math.abs(e.touches[0].clientY-ty)>10)) moved=true;
}}, {{passive:true}});
document.addEventListener("touchend", e => {{
  if (tx===null) return;
  const dx = e.changedTouches[0].clientX - tx, dy = e.changedTouches[0].clientY - ty; tx=null;
  if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) go(dx < 0 ? 1 : -1);  // horizontal swipe
}}, {{passive:true}});
document.addEventListener("click", e => {{
  if (moved) {{ moved=false; return; }}                 // a drag/scroll, not a tap
  if (!$("menu").hidden) return;                       // menu open: ignore taps
  if (e.target.closest("#menuBtn, #menu")) return;     // controls handle themselves
  const x = e.clientX / window.innerWidth;
  if (x < 0.33) go(-1);
  else if (x > 0.67) go(1);
}});

document.addEventListener("keydown", e => {{
  if (e.key === "ArrowRight" || e.key === " ") {{ go(1); e.preventDefault(); }}
  if (e.key === "ArrowLeft") go(-1);
  if (e.key === "Escape") closeMenu();
}});

render();
</script>
</body></html>"""


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/dawn_treader")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "reader.html"
    title, styles, default_style, pages = load_sections(root)
    html = build_html(title, styles, default_style, pages)
    out.write_text(html)
    print(f"wrote {out} ({len(html)//1024} KB) — {len(pages)} pages, "
          f"{len(styles)} styles {styles}")


if __name__ == "__main__":
    main()
