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
// Hemmelegheiter (npx wrangler secret put …): KODE_NOKKEL (signerer tokenet), HENT_NOKKEL (GitHub brukar
// han for å hente køen; same verdi som GitHub-hemmelegheita HENT_NOKKEL).
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

// Token = utløpstid.tilfeldig.signatur – signaturen dekkjer koden, så koden sjølv står ikkje i tokenet
async function lagToken(env, kode) {
  const ut = Date.now() + GYLDIG_MIN * 60000;
  const tilfeldig = b64(crypto.getRandomValues(new Uint8Array(12)));
  const sig = await crypto.subtle.sign("HMAC", await hmacNokkel(env), enc.encode(`${ut}.${tilfeldig}.${kode}`));
  return `${ut}.${tilfeldig}.${b64(sig)}`;
}

async function sjekkToken(env, token, kode) {
  const [ut, tilfeldig, sig] = String(token || "").split(".");
  if (!sig || !/^\d{6}$/.test(kode) || !(Number(ut) > Date.now())) return false;
  try {
    return await crypto.subtle.verify("HMAC", await hmacNokkel(env), unb64(sig), enc.encode(`${ut}.${tilfeldig}.${kode}`));
  } catch (e) {
    return false;
  }
}

const ko = env => env.KO.get(env.KO.idFromName("eigar"));

// Legg ei melding i køen som GitHub hentar (sjå øvst)
async function tilEigaren(env, melding) {
  const r = await ko(env).fetch("https://ko/legg", {method: "POST", body: JSON.stringify({tid: Date.now(), ...melding})});
  if (!r.ok) throw new Error(`køen svarte ${r.status}`);
}

function rettNokkel(env, req) {
  const fatt = enc.encode(req.headers.get("Authorization") || ""), venta = enc.encode(`Bearer ${env.HENT_NOKKEL || ""}`);
  return Boolean(env.HENT_NOKKEL) && fatt.byteLength === venta.byteLength && crypto.subtle.timingSafeEqual(fatt, venta);
}

// Køen: éin instans for heile tenesta, så same melding blir aldri henta to gonger
export class Ko {
  constructor(ctx) {
    this.ctx = ctx;
  }

  async fetch(req) {
    const grense = Date.now() - GYLDIG_MIN * 60000;
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
