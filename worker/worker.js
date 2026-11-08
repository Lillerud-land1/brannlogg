// Brannlogg Stord – opning av admin-sperra (Cloudflare Worker).
//
// Når nokon blir sperra etter tre feil admin-kodar i appen, får eigaren varsel i den private ntfy-kanalen.
// Ber nokon om kode, får eigaren ein 6-sifra eingongskode same stad, og rett kode gir tre nye forsøk.
//
// ntfy.sh tek ikkje imot meldingar direkte herifrå: dei har ein dagskvote per IP-adresse, og Cloudflare deler
// IP-adresser med svært mange andre. Difor blir meldingane lagde i ein liten kø (Durable Object «Ko»), som
// GitHub Actions hentar kvart 10. sekund (jobben «neste» i oppdater.yml, `oppdater.py --admin-meldingar`)
// og sender vidare til den private kanalen. Køen tek berre vare på meldingar i 15 minutt.
//
// Koden sjølv blir ikkje lagra her: appen får ein signert token (HMAC med KODE_NOKKEL) som gjeld i 15 minutt.
// Meldingane seier omtrent kvar (fylke/land) og kva slags eining – aldri IP-adressa.
//
// Besøkstal: appen sender «/besok» kvar gong ho blir opna (ikkje infoskjermen). Teljaren (Durable Object «Teljar»)
// tel kvar eining éin gong per dag: IP-adresse og nettlesar blir gjort om til ein hash med eit tilfeldig dagssalt,
// og både saltet og hashane blir sletta når dagen er over. Att står berre talet på besøkande per dag (alle dagar,
// så adminpanelet kan vise totalen).
// Adminpanelet hentar tala med «/besok-tal» og ein nøkkel som blir rekna ut frå admin-koden (BESOK_NOKKEL).
// Kjem nokon via QR-koden på infoskjermen (adressa «/?qr»), sender appen {qr: true}, og kvar skanning blir talt
// (tabellen qr; berre talet per dag, maks 3 i minuttet frå same eining – sjå GRENSE_EINING). Appen sender òg kva
// slags eining det er (mobil/nettbrett/pc) og om ho er opna som app frå heimeskjermen; det blir talt saman med
// besøket (éin gong per eining per dag, tabellen typar). 8.11.2026.
//
// Infoskjermen melder frå kvart 5. minutt («/tv», med ein nøkkel som byggjeskriptet reknar ut som
// HMAC(HENT_NOKKEL, "tv-ping")), og adminpanelet viser når han sist vart sett og kor gamle dataa på skjermen er.
//
// «Oppdater no» i adminpanelet: krev BESOK_NOKKEL (frå admin-koden) OG ein 6-sifra kode som blir sendt til den
// private ntfy-kanalen (same kø som over). Koden gjeld i 15 minutt, éin gong, maks 5 feil forsøk, og det må gå
// 5 minutt mellom kvar oppdatering. Eigaren får melding både om koden og når oppdateringa blir starta.
// Køen får meldinga {type: "oppdater"}; jobben «neste» i oppdater.yml startar då neste køyring med ein gong.
//
// Hemmelegheiter (npx wrangler secret put …): KODE_NOKKEL (signerer tokenet), HENT_NOKKEL (GitHub brukar
// han for å hente køen; same verdi som GitHub-hemmelegheita HENT_NOKKEL), BESOK_NOKKEL (sjå README, bolk 11).
// Legg ut: cd worker && npx wrangler deploy   (sjå README, bolk 11)

const LOVLEGE = ["https://bls.lillerud.com", "http://localhost:8765"];
const GYLDIG_MIN = 15;
const enc = new TextEncoder();

const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64 = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));

function eining(ua) {
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows-PC"
    : /Macintosh/.test(ua) ? "Mac" : /CrOS/.test(ua) ? "Chromebook" : /Linux/.test(ua) ? "Linux" : "ukjend eining";
  const nl = /Edg\//.test(ua) ? "Edge" : /SamsungBrowser/.test(ua) ? "Samsung Internet" : /Firefox|FxiOS/.test(ua) ? "Firefox"
    : /OPR\//.test(ua) ? "Opera" : /Chrome|CriOS/.test(ua) ? "Chrome" : /Safari/.test(ua) ? "Safari" : "";
  return nl ? `${os} (${nl})` : os;
}

function stad(cf) {
  let land = cf?.country || "";
  try { land = land === "NO" ? "Noreg" : new Intl.DisplayNames(["nn", "nb"], {type: "region"}).of(land) || land; } catch (e) {}
  return [cf?.region, land].filter(Boolean).join(", ") || "ukjend stad";
}

const hmacNokkel = env => crypto.subtle.importKey("raw", enc.encode(env.KODE_NOKKEL), {name: "HMAC", hash: "SHA-256"}, false, ["sign", "verify"]);

// Token = utløpstid.tilfeldig.signatur – signaturen dekkjer koden, så koden sjølv står ikkje i tokenet.
// «formaal» skil tokena: eit token for å opne sperra kan ikkje brukast til «Oppdater no», og omvendt.
const signert = (ut, tilfeldig, kode, formaal) => enc.encode(`${formaal ? formaal + "." : ""}${ut}.${tilfeldig}.${kode}`);
async function lagToken(env, kode, formaal = "") {
  const ut = Date.now() + GYLDIG_MIN * 60000;
  const tilfeldig = b64(crypto.getRandomValues(new Uint8Array(12)));
  const sig = await crypto.subtle.sign("HMAC", await hmacNokkel(env), signert(ut, tilfeldig, kode, formaal));
  return `${ut}.${tilfeldig}.${b64(sig)}`;
}

async function sjekkToken(env, token, kode, formaal = "") {
  const [ut, tilfeldig, sig] = String(token || "").split(".");
  if (!sig || !/^\d{6}$/.test(kode) || !(Number(ut) > Date.now())) return false;
  try {
    return await crypto.subtle.verify("HMAC", await hmacNokkel(env), unb64(sig), signert(ut, tilfeldig, kode, formaal));
  } catch (e) {
    return false;
  }
}

// Nøkkelen infoskjermen sender med «/tv»: HMAC(HENT_NOKKEL, "tv-ping") – byggjeskriptet reknar ut det same
async function tvNokkel(env) {
  const k = await crypto.subtle.importKey("raw", enc.encode(env.HENT_NOKKEL || ""), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  return b64(await crypto.subtle.sign("HMAC", k, enc.encode("tv-ping")));
}
const EININGAR = ["mobil", "nettbrett", "pc"];
const OPPD_PAUSE_MIN = 5, OPPD_MAKS_FEIL = 5;

const ko = env => env.KO.get(env.KO.idFromName("eigar"));

// Legg ei melding i køen som GitHub hentar (sjå øvst)
async function tilEigaren(env, melding) {
  const r = await ko(env).fetch("https://ko/legg", {method: "POST", body: JSON.stringify({tid: Date.now(), ...melding})});
  if (!r.ok) throw new Error(`køen svarte ${r.status}`);
}

function likt(fatt, nokkel) {
  const a = enc.encode(String(fatt || "")), b = enc.encode(nokkel || "");
  return Boolean(nokkel) && a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}
const rettNokkel = (env, req) => likt(req.headers.get("Authorization"), `Bearer ${env.HENT_NOKKEL || ""}`);

const teljar = env => env.TELJAR.get(env.TELJAR.idFromName("besok"));
const osloDag = (d = new Date()) => new Intl.DateTimeFormat("sv-SE", {timeZone: "Europe/Oslo"}).format(d);   // «2026-10-30»
const ROBOT = /bot|crawl|spider|slurp|headless|lighthouse|preview/i;

// Besøksteljaren: éin instans for heile tenesta (sjå øvst)
export class Teljar {
  constructor(ctx) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS dagar (dag TEXT PRIMARY KEY, besokande INTEGER NOT NULL)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS sett (h TEXT PRIMARY KEY)");
    // Skanningar av QR-koden på infoskjermen (adressa «/?qr»): kvar skanning blir talt
    this.sql.exec("CREATE TABLE IF NOT EXISTS qr (dag TEXT PRIMARY KEY, skann INTEGER NOT NULL)");
    this.sql.exec("DROP TABLE IF EXISTS qr_sett");   // brukt nokre timar 8.11.2026, då skanningar vart talde éin gong per eining
    // Kva slags einingar som besøkjer (mobil/nettbrett/pc) og kor mange som brukar appen frå heimeskjermen (namn «app»)
    this.sql.exec("CREATE TABLE IF NOT EXISTS typar (dag TEXT NOT NULL, namn TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (dag, namn))");
  }

  async fetch(req) {
    const dag = osloDag();
    if (new URL(req.url).pathname === "/tel") {
      const {kjenne, qr, eining, app} = await req.json();
      let salt = await this.ctx.storage.get("salt");
      if (salt?.dag !== dag) {
        // Ny dag: saltet og hashane frå i går blir sletta, så ingen kan kjennast att frå dag til dag
        salt = {dag, verdi: b64(crypto.getRandomValues(new Uint8Array(16)))};
        this.sql.exec("DELETE FROM sett");
        await this.ctx.storage.put("salt", salt);
      }
      const h = b64(await crypto.subtle.digest("SHA-256", enc.encode(`${salt.verdi}|${kjenne}`)));
      const pluss = namn => this.sql.exec("INSERT INTO typar (dag, namn, n) VALUES (?, ?, 1) ON CONFLICT(dag, namn) DO UPDATE SET n = n + 1", dag, namn);
      if (this.sql.exec("INSERT OR IGNORE INTO sett (h) VALUES (?) RETURNING h", h).toArray().length) {
        this.sql.exec("INSERT INTO dagar (dag, besokande) VALUES (?, 1) ON CONFLICT(dag) DO UPDATE SET besokande = besokande + 1", dag);
        if (EININGAR.includes(eining)) pluss(eining);
        if (app === true) pluss("app");
      }
      if (qr === true) this.sql.exec("INSERT INTO qr (dag, skann) VALUES (?, 1) ON CONFLICT(dag) DO UPDATE SET skann = skann + 1", dag);
      return new Response("ok");
    }
    if (new URL(req.url).pathname === "/tv") {
      await this.ctx.storage.put("tv", await req.json());
      return new Response("ok");
    }
    const fra = osloDag(new Date(Date.now() - 29 * 864e5));
    const alle = this.sql.exec("SELECT MIN(dag) AS fyrste, COALESCE(SUM(besokande), 0) AS totalt FROM dagar").one();
    const qr = this.sql.exec("SELECT MIN(dag) AS fyrste, COALESCE(SUM(skann), 0) AS totalt FROM qr").one();
    const typar = Object.fromEntries(this.sql.exec("SELECT namn, SUM(n) AS n FROM typar WHERE dag >= ? GROUP BY namn", fra).toArray().map(r => [r.namn, r.n]));
    return Response.json({idag: dag, dagar: this.sql.exec("SELECT dag, besokande FROM dagar WHERE dag >= ? ORDER BY dag", fra).toArray(),
      fyrste: alle.fyrste, totalt: alle.totalt,
      qr: {dagar: this.sql.exec("SELECT dag, skann FROM qr WHERE dag >= ? ORDER BY dag", fra).toArray(), fyrste: qr.fyrste, totalt: qr.totalt},
      typar, tv: (await this.ctx.storage.get("tv")) || null});
  }
}

// Køen: éin instans for heile tenesta, så same melding blir aldri henta to gonger
export class Ko {
  constructor(ctx) {
    this.ctx = ctx;
  }

  async fetch(req) {
    const no = Date.now();
    // «Oppdater no»: kvart token (koden) kan brukast éin gong, maks 5 feil forsøk, og 5 minutt mellom kvar oppdatering.
    // Same instans som køen, så kontrollen gjeld for heile tenesta.
    if (new URL(req.url).pathname === "/oppdater") {
      const {tilfeldig, ut, gyldig} = await req.json();
      const s = (await this.ctx.storage.get("oppd")) || {sist: 0, tokens: {}};
      for (const [k, v] of Object.entries(s.tokens)) if (!(v.ut > no)) delete s.tokens[k];
      if (typeof tilfeldig !== "string" || !/^[\w-]{8,40}$/.test(tilfeldig) || !(ut > no) || ut > no + (GYLDIG_MIN + 1) * 60000)
        return Response.json({feil: "ugyldig"});
      if (!s.tokens[tilfeldig] && Object.keys(s.tokens).length >= 50) return Response.json({feil: "for mange"});
      const t = s.tokens[tilfeldig] ||= {feil: 0, brukt: false, ut};
      let svar;
      if (t.brukt) svar = {feil: "brukt"};
      else if (t.feil >= OPPD_MAKS_FEIL) svar = {feil: "sperra"};
      else if (!gyldig) { t.feil++; svar = {feil: "feil kode", att: OPPD_MAKS_FEIL - t.feil}; }
      else if (no - s.sist < OPPD_PAUSE_MIN * 60000) svar = {feil: "nyleg", sekund: Math.ceil((s.sist + OPPD_PAUSE_MIN * 60000 - no) / 1000)};
      else { t.brukt = true; s.sist = no; svar = {ok: true}; }
      await this.ctx.storage.put("oppd", s);
      return Response.json(svar);
    }
    const grense = no - GYLDIG_MIN * 60000;
    const liste = ((await this.ctx.storage.get("ko")) || []).filter(m => m.tid > grense);
    if (new URL(req.url).pathname === "/legg") {
      liste.push(await req.json());
      await this.ctx.storage.put("ko", liste.slice(-50));
      return new Response("ok");
    }
    await this.ctx.storage.delete("ko");
    return Response.json(liste);
  }
}

export default {
  async fetch(req, env) {
    const sti = new URL(req.url).pathname;

    // GitHub hentar køen (ikkje for nettlesarar)
    if (sti === "/hent") {
      if (!rettNokkel(env, req)) return new Response("ikkje tillate", {status: 403});
      return ko(env).fetch("https://ko/hent", {method: "POST"});
    }

    const opphav = req.headers.get("Origin") || "";
    const cors = LOVLEGE.includes(opphav) ? {"Access-Control-Allow-Origin": opphav, "Vary": "Origin"} : null;
    const svar = (data, status = 200) => new Response(JSON.stringify(data), {status,
      headers: {"Content-Type": "application/json", "Cache-Control": "no-store", ...cors}});
    if (!cors) return svar({feil: "ikkje tillate"}, 403);
    if (req.method === "OPTIONS") return new Response(null, {status: 204, headers: {...cors,
      "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400"}});
    if (req.method !== "POST") return svar({feil: "ikkje tillate"}, 405);

    const ip = req.headers.get("CF-Connecting-IP") || "ukjend";
    // Avgrensing per eining og samla, så ingen kan fylle kanalen med meldingar eller prøve mange kodar
    const innanfor = async (...nokklar) => {
      for (const [grense, key] of nokklar) if (!(await grense.limit({key})).success) return false;
      return true;
    };
    const kven = `Stad: ${stad(req.cf)} (omtrentleg)\nEining: ${eining(req.headers.get("User-Agent") || "")}`;
    const data = await req.json().catch(() => ({}));

    try {
      if (sti === "/besok") {
        const ua = req.headers.get("User-Agent") || "";
        // Maks 3 opningar i minuttet frå same eining blir talde (IP + nettlesar, sidan mange mobilar deler IP-adresse),
        // og robotar blir ikkje talde
        if (!ROBOT.test(ua) && (await innanfor([env.GRENSE_EINING, `besok:${ip}|${ua.slice(0, 300)}`])))
          await teljar(env).fetch("https://teljar/tel", {method: "POST", body: JSON.stringify({kjenne: `${ip}|${ua}`, qr: data.qr === true,
            eining: EININGAR.includes(data.eining) ? data.eining : null, app: data.app === true})});
        return svar({ok: true});
      }
      if (sti === "/tv") {
        // Infoskjermen melder frå (sjå øvst). Berre tidspunkt, skjermstorleik og kor gamle dataa på skjermen er.
        if (!(await innanfor([env.GRENSE_EINING, `tv:${ip}`]))) return svar({feil: "for mange"}, 429);
        if (!env.HENT_NOKKEL || !likt(data.nokkel, await tvNokkel(env))) return svar({feil: "ikkje tillate"}, 403);
        const skjerm = /^\d{2,5}x\d{2,5}$/.test(data.skjerm || "") ? data.skjerm : null;
        const dataTid = typeof data.data === "string" && data.data.length < 40 && !isNaN(Date.parse(data.data)) ? data.data : null;
        await teljar(env).fetch("https://teljar/tv", {method: "POST", body: JSON.stringify({sist: Date.now(), skjerm, data: dataTid})});
        return svar({ok: true});
      }
      if (sti === "/oppdater-kode") {
        // Lag 1: berre den som har admin-koden (BESOK_NOKKEL) kan be om kode. Lag 2: koden går til mobilen til eigaren.
        if (!(await innanfor([env.GRENSE_EINING, `okode:${ip}`], [env.GRENSE_ALLE, "meldingar"]))) return svar({feil: "for mange"}, 429);
        if (!likt(data.nokkel, env.BESOK_NOKKEL)) return svar({feil: "ikkje tillate"}, 403);
        const kode = String(crypto.getRandomValues(new Uint32Array(1))[0] % 1000000).padStart(6, "0");
        await tilEigaren(env, {title: `🔄 Kode for «Oppdater no»: ${kode}`, tags: ["key"], priority: 4,
          message: `Nokon bad om å starte oppdateringa frå adminpanelet. Koden gjeld i ${GYLDIG_MIN} minutt, éin gong.\n${kven}\n` +
                   "Var det ikkje deg: ikkje gi koden til nokon, og byt admin-koden."});
        return svar({token: await lagToken(env, kode, "oppdater")});
      }
      if (sti === "/oppdater") {
        if (!(await innanfor([env.GRENSE_EINING, `oppd:${ip}`]))) return svar({feil: "for mange"}, 429);
        if (!likt(data.nokkel, env.BESOK_NOKKEL)) return svar({feil: "ikkje tillate"}, 403);
        const [ut, tilfeldig] = String(data.token || "").split(".");
        const gyldig = await sjekkToken(env, data.token, String(data.kode || ""), "oppdater");
        const r = await (await ko(env).fetch("https://ko/oppdater", {method: "POST",
          body: JSON.stringify({tilfeldig, ut: Number(ut), gyldig})})).json();
        if (r.ok) {
          await tilEigaren(env, {type: "oppdater", title: "🔄 Oppdatering starta frå adminpanelet", tags: ["arrows_counterclockwise"],
            priority: 3, message: `Appen og infoskjermen hentar nye data no.\n${kven}`});
          return svar({ok: true});
        }
        if (r.feil === "feil kode" && r.att === 0)
          await tilEigaren(env, {title: "⚠️ Fem feil kodar for «Oppdater no»", tags: ["warning"], priority: 4,
            message: `Nokon skreiv feil kode fem gonger. Koden er no ugyldig.\n${kven}\nVar det ikkje deg, bør du byte admin-koden.`});
        return svar(r, r.feil === "nyleg" ? 429 : 403);
      }
      if (sti === "/besok-tal") {
        if (!(await innanfor([env.GRENSE_EINING, `tal:${ip}`]))) return svar({feil: "for mange"}, 429);
        if (!likt(data.nokkel, env.BESOK_NOKKEL)) return svar({feil: "ikkje tillate"}, 403);
        return svar(await (await teljar(env).fetch("https://teljar/tal")).json());
      }
      if (sti === "/sperra") {
        if (!(await innanfor([env.GRENSE_EINING, `sperra:${ip}`], [env.GRENSE_ALLE, "meldingar"]))) return svar({feil: "for mange"}, 429);
        await tilEigaren(env, {title: "🔒 Nokon vart sperra frå admin-panelet", tags: ["lock"], priority: 4,
          message: `Ei eining vart sperra i 24 timar etter tre feil admin-kodar.\n${kven}\n` +
                   "Var det deg, kan du trykkje «Send kode til eigaren» i bobla."});
        return svar({ok: true});
      }
      if (sti === "/kode") {
        if (!(await innanfor([env.GRENSE_EINING, `kode:${ip}`], [env.GRENSE_ALLE, "meldingar"]))) return svar({feil: "for mange"}, 429);
        const kode = String(crypto.getRandomValues(new Uint32Array(1))[0] % 1000000).padStart(6, "0");
        await tilEigaren(env, {title: `🔐 Opningskode: ${kode}`, tags: ["key"], priority: 4,
          message: `Nokon bad om kode for å opne admin-sperra. Koden gjeld i ${GYLDIG_MIN} minutt og gir tre nye forsøk.\n${kven}\n` +
                   "Var det ikkje deg, så sjå bort frå meldinga – då blir eininga verande sperra."});
        return svar({token: await lagToken(env, kode)});
      }
      if (sti === "/sjekk") {
        if (!(await innanfor([env.GRENSE_EINING, `sjekk:${ip}`]))) return svar({feil: "for mange"}, 429);
        return (await sjekkToken(env, data.token, String(data.kode || ""))) ? svar({ok: true}) : svar({feil: "feil kode"}, 403);
      }
      return svar({feil: "finst ikkje"}, 404);
    } catch (e) {
      console.error(`Feil i ${sti}: ${e.message}`);   // berre synleg i «wrangler tail», blir ikkje lagra
      return svar({feil: "teknisk feil"}, 502);
    }
  },
};
