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
// Adminpanelet hentar tala med «/besok-tal» og eit innloggingsbevis (admin-kode + PIN, sjå «Innlogging i adminpanelet»).
// Kjem nokon via QR-koden på infoskjermen (adressa «/?qr»), sender appen {qr: true}, og kvar skanning blir talt
// (tabellen qr; berre talet per dag, maks 3 i minuttet frå same eining – sjå GRENSE_EINING). Appen sender òg kva
// slags eining det er (mobil/nettbrett/pc) og om ho er opna som app frå heimeskjermen; det blir talt saman med
// besøket (éin gong per eining per dag, tabellen typar). 8.11.2026.
//
// Infoskjermen melder frå kvart 5. minutt («/tv», med ein nøkkel som byggjeskriptet reknar ut som
// HMAC(HENT_NOKKEL, "tv-ping")), og adminpanelet viser når han sist vart sett og kor gamle dataa på skjermen er.
//
// «Oppdater no» i adminpanelet: krev innlogging med full tilgang (admin-kode + PIN) OG ein 6-sifra kode som blir sendt til den
// private ntfy-kanalen (same kø som over). Koden gjeld i 15 minutt, éin gong, maks 5 feil forsøk, og det må gå
// 5 minutt mellom kvar oppdatering. Eigaren får melding både om koden og når oppdateringa blir starta.
// Køen får meldinga {type: "oppdater"}; jobben «neste» i oppdater.yml startar då neste køyring med ein gong.
//
// Hemmelegheiter (npx wrangler secret put …): PINAR (PIN-kodane, berre hashar) og TV_ADRESSE (lenka til infoskjermen) –
// begge set av admin_oppsett.py –, KODE_NOKKEL (signerer token og innloggingsbevis), HENT_NOKKEL (GitHub brukar
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
// Adresse → posisjon (Geonorge, berre Stord kommune), til kartet i alarmmodus
async function geokod(adr) {
  try {
    const r = await fetch("https://ws.geonorge.no/adresser/v1/sok?" + new URLSearchParams({sok: adr, kommunenummer: "4614", treffPerSide: "10"}),
      {headers: {"User-Agent": "brannlogg-stord/1.0"}});
    const liste = (await r.json()).adresser || [];
    const norm = x => String(x || "").toLowerCase().replace(/\s+/g, " ").trim();
    const a = liste.find(x => norm(x.adressetekst) === norm(adr)) || liste[0];
    if (!a || !a.representasjonspunkt) return null;
    return {pos: [a.representasjonspunkt.lat, a.representasjonspunkt.lon], stad: [a.adressetekst, a.poststed].filter(Boolean).join(", ")};
  } catch (e) {
    return null;
  }
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

/* ---- Innlogging i adminpanelet (8.11.2026, ønske frå brukaren: «ikkje mogleg å kome forbi utan rett passord»).
   Steg 1: admin-koden (appen reknar ut BESOK_NOKKEL med PBKDF2 og sender berre nøkkelen, «/admin-sjekk»).
   Steg 2: personleg PIN-kode («/logginn»). PIN-kodane ligg i hemmelegheita PINAR som JSON:
     [{"namn": "Leander", "rolle": "full" | "avgrensa", "salt": "<b64>", "hash": "<b64 HMAC-SHA256(salt, pin)>"}]
   (laga av C:\Claude\brannlogg-forslag\admin_oppsett.py – PIN-kodane sjølve står ingen stad).
   Rett admin-kode + rett PIN gir eit signert innloggingsbevis (HMAC med KODE_NOKKEL) som gjeld i OKT_MIN minutt.
   Alt i panelet som ikkje er offentleg (besøkstal, «Oppdater no», lenka til infoskjermen) krev beviset, og
   «full» krevst for lenka til infoskjermen og «Oppdater no». Lenka står berre her (hemmelegheita TV_ADRESSE).
   Gjetting: 5 feil PIN frå same eining = sperra i 15 minutt; 20 feil totalt på ein time = alle sperra i ein time;
   10 feil admin-kodar frå same eining på ein time = sperra i ein time. Eigaren får melding om sperrer og
   om kvar innlogging. Einingar blir kjende att på ein hash av IP-adressa, aldri sjølve adressa. ---- */
const OKT_MIN = 30;
const ROLLER = ["full", "avgrensa"];
async function hmacB64(nokkelBytes, tekst) {
  const k = await crypto.subtle.importKey("raw", nokkelBytes, {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  return b64(await crypto.subtle.sign("HMAC", k, enc.encode(tekst)));
}
const ipHash = async (env, ip) => (await hmacB64(enc.encode(env.KODE_NOKKEL || ""), `ip.${ip}`)).slice(0, 22);
function pinListe(env) {
  try {
    const l = JSON.parse(env.PINAR || "[]");
    return Array.isArray(l) ? l.filter(p => p && typeof p.namn === "string" && ROLLER.includes(p.rolle) && p.salt && p.hash) : [];
  } catch (e) {
    return [];
  }
}
async function finnPin(env, pin) {
  if (!/^\d{4,8}$/.test(pin)) return null;
  let funnen = null;
  for (const p of pinListe(env)) {   // går gjennom alle, så tida ikkje avslører kven som var nær
    let salt;
    try { salt = unb64(p.salt); } catch (e) { continue; }
    if (likt(await hmacB64(salt, pin), p.hash) && !funnen) funnen = {namn: p.namn.slice(0, 40), rolle: p.rolle};
  }
  return funnen;
}
async function lagOkt(env, brukar) {
  const ut = Date.now() + OKT_MIN * 60000, namn = b64(enc.encode(brukar.namn));
  const sig = await hmacB64(enc.encode(env.KODE_NOKKEL), `okt.${ut}.${brukar.rolle}.${namn}`);
  return {token: `${ut}.${brukar.rolle}.${namn}.${sig}`, ut};
}
async function sjekkOkt(env, token) {
  const [ut, rolle, namn, sig] = String(token || "").split(".");
  if (!sig || !ROLLER.includes(rolle) || !(Number(ut) > Date.now()) || Number(ut) > Date.now() + (OKT_MIN + 1) * 60000) return null;
  if (!likt(sig, await hmacB64(enc.encode(env.KODE_NOKKEL || ""), `okt.${ut}.${rolle}.${namn}`))) return null;
  try { return {rolle, namn: new TextDecoder().decode(unb64(namn))}; } catch (e) { return null; }
}
// Sperre mot gjetting, lagra i Durable Object «Ko» (éin instans for heile tenesta)
const sperre = async (env, type, id, utfall) => (await ko(env).fetch("https://ko/sperre", {method: "POST",
  body: JSON.stringify({type, id, utfall, ...(type === "pin" ? {maks: 5, sperreMin: 15, alleMaks: 20} : {maks: 10, sperreMin: 60, alleMaks: 100})})})).json();

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
    // «Brann pågår»: eit oppslag som set infoskjermen i alarmmodus til det blir avslutta (maks 8 timar)
    if (new URL(req.url).pathname === "/pagar") {
      const d = await req.json();
      if (d.handling === "sett") await this.ctx.storage.put("pagar", {tittel: d.tittel, start: d.start || new Date().toISOString(), ut: Date.now() + 8 * 36e5, av: d.av, pos: d.pos || null, stad: d.stad || null, utanTid: d.utanTid === true});
      else if (d.handling === "av") await this.ctx.storage.delete("pagar");
      const p = await this.ctx.storage.get("pagar");
      if (p && p.ut < Date.now()) { await this.ctx.storage.delete("pagar"); return Response.json({}); }
      return Response.json(p ? {tittel: p.tittel, start: p.start, av: p.av, pos: p.pos, stad: p.stad, utanTid: !!p.utanTid} : {});
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
    // Sperre mot gjetting av PIN («pin») og admin-kode («admin») – sjå «Innlogging i adminpanelet» øvst.
    // utfall «sjekk» (før forsøket), «feil» eller «ok». Feil eldre enn ein time blir gløymde.
    if (new URL(req.url).pathname === "/sperre") {
      const {type, id, utfall, maks, sperreMin, alleMaks} = await req.json();
      if (!["pin", "admin"].includes(type) || typeof id !== "string" || id.length > 40) return Response.json({feil: "ugyldig"});
      const lager = "sp_" + type, s = (await this.ctx.storage.get(lager)) || {einingar: {}, alle: [], alleTil: 0};
      for (const [k, v] of Object.entries(s.einingar)) if (v.til < no && no - v.sist > 3600000) delete s.einingar[k];
      s.alle = s.alle.filter(t => no - t < 3600000);
      const e = s.einingar[id] ||= {feil: 0, til: 0, sist: no};
      const sperraTil = Math.max(e.til, s.alleTil || 0);
      let svar = {ok: true};
      if (sperraTil > no) svar = {feil: "sperra", sekund: Math.ceil((sperraTil - no) / 1000), alle: (s.alleTil || 0) > no};
      else if (utfall === "ok") delete s.einingar[id];
      else if (utfall === "feil") {
        e.feil++; e.sist = no; s.alle.push(no);
        svar = {ok: true, att: Math.max(0, maks - e.feil)};
        if (e.feil >= maks) { e.til = no + sperreMin * 60000; e.feil = 0; svar = {feil: "sperra", sekund: sperreMin * 60, nySperre: true}; }
        if (s.alle.length >= alleMaks) { s.alleTil = no + 3600000; s.alle = []; svar = {feil: "sperra", sekund: 3600, alle: true, nySperre: true}; }
      }
      if (Object.keys(s.einingar).length > 500) s.einingar = {};   // vern mot at lageret veks utan grense
      await this.ctx.storage.put(lager, s);
      return Response.json(svar);
    }
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
      // Innloggingsbeviset (sjå «Innlogging i adminpanelet» øvst): 401 utan gyldig bevis, 403 utan rett rolle
      const krevOkt = async rolle => {
        const o = await sjekkOkt(env, data.okt);
        if (!o) return [null, svar({feil: "logg inn"}, 401)];
        if (rolle === "full" && o.rolle !== "full") return [null, svar({feil: "ikkje tilgang"}, 403)];
        return [o, null];
      };
      if (sti === "/admin-sjekk") {
        // Steg 1: er admin-koden rett? (appen sender berre nøkkelen som er rekna ut frå koden)
        if (!(await innanfor([env.GRENSE_EINING, `asjekk:${ip}`]))) return svar({feil: "for mange"}, 429);
        const id = await ipHash(env, ip), forst = await sperre(env, "admin", id, "sjekk");
        if (!forst.ok) return svar(forst, 423);
        if (likt(data.nokkel, env.BESOK_NOKKEL)) { await sperre(env, "admin", id, "ok"); return svar({ok: true}); }
        const r = await sperre(env, "admin", id, "feil");
        if (r.nySperre)
          await tilEigaren(env, {title: "⚠️ Mange feil admin-kodar", tags: ["warning"], priority: 4,
            message: `${r.alle ? "Alle einingar" : "Ei eining"} er sperra frå admin-innlogginga i ein time etter mange feil kodar.\n${kven}`});
        return svar(r.nySperre ? r : {feil: "feil kode"}, r.nySperre ? 423 : 403);
      }
      if (sti === "/logginn") {
        // Steg 2: personleg PIN-kode. Utan rett admin-kode blir PIN-koden ikkje ein gong sjekka.
        if (!(await innanfor([env.GRENSE_EINING, `pin:${ip}`]))) return svar({feil: "for mange"}, 429);
        if (!likt(data.nokkel, env.BESOK_NOKKEL)) return svar({feil: "ikkje tillate"}, 403);
        const id = await ipHash(env, ip), forst = await sperre(env, "pin", id, "sjekk");
        if (!forst.ok) return svar(forst, 423);
        const brukar = await finnPin(env, String(data.pin || ""));
        if (!brukar) {
          const r = await sperre(env, "pin", id, "feil");
          if (r.nySperre)
            await tilEigaren(env, {title: "⚠️ Feil PIN-kodar i adminpanelet", tags: ["warning"], priority: 4,
              message: `${r.alle ? "For mange feil PIN-kodar totalt – all innlogging er sperra i ein time." : "Nokon med rett admin-kode skreiv feil PIN-kode fem gonger. Eininga er sperra i 15 minutt."}\n${kven}\n` +
                       "Var det ikkje nokon du kjenner: byt admin-koden (admin_oppsett.py)."});
          return svar(r.nySperre ? r : {feil: "feil pin", att: r.att}, r.nySperre ? 423 : 403);
        }
        await sperre(env, "pin", id, "ok");
        const {token, ut} = await lagOkt(env, brukar);
        await tilEigaren(env, {title: `🔓 ${brukar.namn} logga inn i adminpanelet`, tags: ["unlock"], priority: 3,
          message: `${brukar.rolle === "full" ? "Full tilgang" : "Avgrensa tilgang"}, i ${OKT_MIN} minutt.\n${kven}\n` +
                   "Var det ikkje deg eller nokon du har gitt PIN-kode: byt admin-koden og PIN-kodane (admin_oppsett.py)."});
        return svar({token, ut, namn: brukar.namn, rolle: brukar.rolle});
      }
      if (sti === "/pagar-hent") {
        // Infoskjermen (TV-nøkkel) og adminpanelet (innlogging) les om «Brann pågår» er på
        const erTv = env.HENT_NOKKEL && likt(data.nokkel, await tvNokkel(env));
        if (!(await innanfor([env.GRENSE_EINING, `pghent:${ip}`]))) return svar({feil: "for mange"}, 429);
        if (!erTv) { const [, nei] = await krevOkt("full"); if (nei) return nei; }
        return svar(await (await teljar(env).fetch("https://teljar/pagar", {method: "POST", body: "{}"})).json());
      }
      if (sti === "/pagar-sett" || sti === "/pagar-av") {
        if (!(await innanfor([env.GRENSE_EINING, `pgsett:${ip}`]))) return svar({feil: "for mange"}, 429);
        const [okt, nei] = await krevOkt("full");
        if (nei) return nei;
        const tittel = String(data.tittel || "").replace(/[ -<>]/g, "").trim().slice(0, 60);
        if (sti === "/pagar-sett" && tittel.length < 2) return svar({feil: "skriv tittel"}, 400);
        // Klokkeslettet det starta (valfritt): ein tidspunkt mellom 12 timar sidan og no; elles blir «no» brukt
        const tS = Date.parse(data.start), start = tS > Date.now() - 12 * 36e5 && tS <= Date.now() + 2 * 6e4 ? new Date(tS).toISOString() : new Date().toISOString();
        const adr = String(data.adresse || "").replace(/[ -<>]/g, "").trim().slice(0, 80);
        const g = sti === "/pagar-sett" && adr ? await geokod(adr) : null;
        const r = await (await teljar(env).fetch("https://teljar/pagar", {method: "POST",
          body: JSON.stringify({handling: sti === "/pagar-sett" ? "sett" : "av", tittel, av: okt.namn, pos: g && g.pos, stad: g && g.stad, start, utanTid: data.utanTid === true})})).json();
        if (adr && !g) r.adresseFeil = true;
        await tilEigaren(env, {title: sti === "/pagar-sett" ? `🚨 «Brann pågår» på infoskjermen: ${tittel}` : "✅ «Brann pågår» avslutta på infoskjermen",
          tags: [sti === "/pagar-sett" ? "rotating_light" : "mute"], priority: 3, message: `${okt.namn}.
${kven}`});
        return svar(r);
      }
      if (sti === "/tv-adresse") {
        // Lenka til infoskjermen – berre med full tilgang
        if (!(await innanfor([env.GRENSE_EINING, `tvadr:${ip}`]))) return svar({feil: "for mange"}, 429);
        const [, nei] = await krevOkt("full");
        if (nei) return nei;
        if (!/^tv-[0-9a-z]{6,13}$/.test(env.TV_ADRESSE || "")) return svar({feil: "ikkje sett opp"}, 503);
        return svar({adresse: `https://bls.lillerud.com/${env.TV_ADRESSE}`});
      }
      if (sti === "/oppdater-kode") {
        // Lag 1: innlogga med full tilgang (admin-kode + PIN). Lag 2: koden går til mobilen til eigaren.
        if (!(await innanfor([env.GRENSE_EINING, `okode:${ip}`], [env.GRENSE_ALLE, "meldingar"]))) return svar({feil: "for mange"}, 429);
        const [okt, nei] = await krevOkt("full");
        if (nei) return nei;
        const kode = String(crypto.getRandomValues(new Uint32Array(1))[0] % 1000000).padStart(6, "0");
        await tilEigaren(env, {title: `🔄 Kode for «Oppdater no»: ${kode}`, tags: ["key"], priority: 4,
          message: `${okt.namn} bad om å starte oppdateringa frå adminpanelet. Koden gjeld i ${GYLDIG_MIN} minutt, éin gong.\n${kven}\n` +
                   "Var det ikkje deg: ikkje gi koden til nokon, og byt admin-koden og PIN-kodane."});
        return svar({token: await lagToken(env, kode, "oppdater")});
      }
      if (sti === "/oppdater") {
        if (!(await innanfor([env.GRENSE_EINING, `oppd:${ip}`]))) return svar({feil: "for mange"}, 429);
        const [okt, nei] = await krevOkt("full");
        if (nei) return nei;
        const [ut, tilfeldig] = String(data.token || "").split(".");
        const gyldig = await sjekkToken(env, data.token, String(data.kode || ""), "oppdater");
        const r = await (await ko(env).fetch("https://ko/oppdater", {method: "POST",
          body: JSON.stringify({tilfeldig, ut: Number(ut), gyldig})})).json();
        if (r.ok) {
          await tilEigaren(env, {type: "oppdater", title: "🔄 Oppdatering starta frå adminpanelet", tags: ["arrows_counterclockwise"],
            priority: 3, message: `${okt.namn} starta henne. Appen og infoskjermen hentar nye data no.\n${kven}`});
          return svar({ok: true});
        }
        if (r.feil === "feil kode" && r.att === 0)
          await tilEigaren(env, {title: "⚠️ Fem feil kodar for «Oppdater no»", tags: ["warning"], priority: 4,
            message: `Nokon skreiv feil kode fem gonger. Koden er no ugyldig.\n${kven}\nVar det ikkje deg, bør du byte admin-koden.`});
        return svar(r, r.feil === "nyleg" ? 429 : 403);
      }
      if (sti === "/besok-tal") {
        if (!(await innanfor([env.GRENSE_EINING, `tal:${ip}`]))) return svar({feil: "for mange"}, 429);
        const [, nei] = await krevOkt("avgrensa");   // begge rollene kan sjå tala
        if (nei) return nei;
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
