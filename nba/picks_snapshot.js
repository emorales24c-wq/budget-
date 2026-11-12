// Abre el dashboard en un navegador sin ventana y devuelve los Mejores props del modelo de cada día
// (los mismos que ve el usuario con los filtros por defecto, sin cambios manuales).
// Uso: node picks_snapshot.js ruta/al/proyecciones.html  -> JSON por stdout
const path = require("path");
const { chromium } = require("playwright");
(async () => {
  const opts = {};
  if (process.env.PW_CHROMIUM) opts.executablePath = process.env.PW_CHROMIUM;
  const b = await chromium.launch(opts);
  const pg = await b.newPage();
  const errs = [];
  pg.on("pageerror", e => errs.push(e.message));
  await pg.goto("file://" + path.resolve(process.argv[2]));
  await pg.waitForTimeout(300);
  const out = await pg.evaluate(() => {
    const res = {};
    for (const d of DAYS) res[d] = [];
    const srcs = typeof hasKal === "function" ? ["ref", "k"] : ["ref"];
    for (const src of srcs) for (const d of DAYS) {
      if (src === "k" && !hasKal(d)) continue;
      Object.assign(BP, { day: d, mk: "", side: "", minp: 55, minm: 20, q: "", game: "", src });
      res[d].push(...bestRows().map(r => {
        const h = typeof hitRate === "function" ? hitRate(r.p.name, r.k, r.line, r.side) : null;
        return { d, n: r.p.name, tm: r.p.tm, opp: r.opp, h: r.home ? 1 : 0, k: r.k, line: r.line, side: r.side,
                 prob: +r.prob.toFixed(4), edge: +r.edge.toFixed(4), proj: +(+r.proj).toFixed(2), h10: h,
                 src, odds: r.odds, tk: r.kal ? r.kal.tk : undefined, c: r.kal ? r.kal.c : undefined };
      }));
    }
    return { sched: SCH, picks: res, b: (DATA.pkcal && DATA.pkcal.b) || 1 };
  });
  await b.close();
  if (errs.length) out.errors = errs;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.message); process.exit(1); });
