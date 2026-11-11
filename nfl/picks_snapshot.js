// Abre el dashboard NFL sin ventana y devuelve los Mejores props y los Anotador TD de cada día
// (filtros por defecto, sin cambios manuales). Uso: node picks_snapshot.js proyecciones-nfl.html
const path = require("path");
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const pg = await b.newPage();
  const errs = [];
  pg.on("pageerror", e => errs.push(e.message));
  await pg.goto("file://" + path.resolve(process.argv[2]));
  await pg.waitForTimeout(300);
  const out = await pg.evaluate(() => {
    const res = {};
    for (const d of DAYS) {
      const rows = [];
      for (const src of (typeof hasKal === "function" ? ["ref", "k"] : ["ref"])) {
        if (src === "k" && !hasKal(d)) continue;
        Object.assign(BP, { day: d, pos: "", mk: "", side: "", minp: 55, q: "", game: "", src });
        rows.push(...bestRows().map(r => ({ d, wk: r.g ? r.g[0] : null, n: r.p.name, tm: r.p.tm, pos: r.p.pos, opp: r.opp, h: r.home ? 1 : 0,
          k: r.k, line: r.line, side: r.side, prob: +r.prob.toFixed(4), edge: +r.edge.toFixed(4), proj: +(+r.proj).toFixed(2), src, odds: r.odds })));
      }
      if (typeof tdRows === "function") {
        Object.assign(TD, { day: d, pos: "", min: 25, q: "" });
        tdRows().filter(r => !r.out).forEach(r => rows.push({ d, wk: r.g[0], n: r.p.name, tm: r.p.tm, pos: r.p.pos, opp: r.opp, h: r.home ? 1 : 0,
          k: "td", line: 0.5, side: "O", prob: +r.prob.toFixed(4), edge: 0, proj: +r.lam.toFixed(3) }));
      }
      res[d] = rows;
    }
    return { sched: NEXT.map(g => [g[1], g[2], g[3], g[4], g[0]]), picks: res, b: (DATA.pkcal && DATA.pkcal.b) || 1 };
  });
  await b.close();
  if (errs.length) out.errors = errs;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.message); process.exit(1); });
