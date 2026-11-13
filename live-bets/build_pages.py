"""Arma nba.html y nfl.html para la app Live Bets a partir de las mismas plantillas de la página web.

La única diferencia con la página web: los datos no vienen metidos en el archivo, la app los baja de GitHub
cada vez que se abre (los actualizan las tareas automáticas de las 4:39pm y 6:50pm), y si no hay internet
usa la última copia guardada. Se corre en GitHub Actions antes de compilar el APK.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = "https://raw.githubusercontent.com/emorales24c-wq/budget-/ccr-55aff67a-ugtp23/"
SHRINK = "3 (8 para touchdowns e intercepciones)"

PAGES = [
    ("nba", "nba/plantilla_dashboard.html", "nba/data/nba_data.json", "🏈 NFL", "nfl.html"),
    ("nfl", "nfl/plantilla_nfl.html", "nfl/data/nfl_data.json", "🏀 NBA", "nba.html"),
]

LOADER = '''<script>
// La app baja los datos más nuevos de GitHub; sin internet usa la última copia guardada
(async () => {
  const URL = "%(url)s", KEY = "lbapp-%(lg)s-data", box = document.getElementById("appload");
  let d = null, cached = false;
  try {
    const r = await fetch(URL + "?t=" + Date.now(), {cache: "no-store"});
    if (r.ok) { d = await r.json(); try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) {} }
  } catch (e) {}
  if (!d) { try { d = JSON.parse(localStorage.getItem(KEY)); cached = !!d; } catch (e) {} }
  if (!d) { box.textContent = "No pude bajar los datos. Revisa tu internet y vuelve a abrir."; return; }
  window.__DATA = d;
  const fill = {fecha: d.fecha, dvpGames: d.dvpGames, semanas: d.semanas, shrink: "%(shrink)s"};
  document.querySelectorAll("[data-f]").forEach(el => { el.textContent = fill[el.dataset.f] ?? ""; });
  const s = document.createElement("script");
  s.text = document.getElementById("mainsrc").textContent;
  document.body.appendChild(s);
  // Dentro de la app, "Copiar para Live Bets" manda los picks directo a Live Bets
  if (window.Android && typeof window.copyLB === "function" && typeof window.lbCode === "function") {
    window.copyLB = (rows) => { try { localStorage.setItem("lb_import", lbCode(rows)); } catch (e) {} location.href = "index.html#import"; };
  }
  box.remove();
  if (cached) { const n = document.createElement("div"); n.className = "meta"; n.style.cssText = "text-align:center;padding:6px"; n.textContent = "Sin internet: mostrando la última copia guardada (" + d.fecha + ")"; document.body.prepend(n); }
})();
</script>
'''

NAV = '''<div style="position:fixed;top:calc(8px + env(safe-area-inset-top,0px));right:10px;z-index:40;display:flex;gap:6px">
  <a href="index.html" style="background:var(--accent);color:var(--accent-ink);border-radius:999px;padding:6px 12px;font-weight:600;font-size:.8rem;text-decoration:none">← Live Bets</a>
  <a href="%(other)s" style="background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-weight:600;font-size:.8rem;text-decoration:none">%(other_lab)s</a>
</div>
<div id="appload" style="position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--muted);font-size:1rem">Bajando los datos más nuevos…</div>
'''


def build(lg, tpl, data, other_lab, other):
    t = open(os.path.join(ROOT, tpl), encoding="utf-8").read()
    i = t.index("<script>")
    j = t.index("</script>", i)
    head, code, tail = t[:i], t[i + len("<script>"):j], t[j + len("</script>"):]
    assert "__FECHA__" not in code and "</script" not in code
    code = code.replace("const DATA=__DATA__;", "const DATA=window.__DATA;")
    assert "window.__DATA" in code
    for ph, key in [("__FECHA__", "fecha"), ("__DVPGAMES__", "dvpGames"), ("__SEMANAS__", "semanas"), ("__SHRINK__", "shrink")]:
        head = head.replace(ph, f'<span data-f="{key}"></span>')
        tail = tail.replace(ph, f'<span data-f="{key}"></span>')
    # la plantilla no trae <body>: la barra y la pantalla de carga van al principio del contenido
    k = head.index("<div class=\"wrap\">") if "<div class=\"wrap\">" in head else 0
    head = head[:k] + NAV % {"other": other, "other_lab": other_lab} + head[k:]
    page = ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>'
            + head + '<script type="text/plain" id="mainsrc">' + code + "</script>"
            + LOADER % {"url": RAW + data, "lg": lg, "shrink": SHRINK} + tail + "</body></html>")
    open(os.path.join(ROOT, "live-bets", lg + ".html"), "w", encoding="utf-8").write(page)
    print("ok", lg, len(page))


for args in PAGES:
    build(*args)
