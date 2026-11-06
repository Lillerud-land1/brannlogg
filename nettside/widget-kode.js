// brannlogg-widget-kode
// Brannlogg Stord – widget for iPhone og iPad (Scriptable, https://scriptable.app). Forslag A «Klassisk», godkjent 6.11.2026.
// Skriptet som blir lima inn i Scriptable (sjå «Widget på iPhone» under Info i appen), hentar denne fila kvar gong,
// så endringar her kjem ut til alle utan at nokon må lime inn på nytt. Data: widget.json (oppdater.py, skriv_widget).
//
// Lys eller mørk: hald fingeren på widgeten, vel «Rediger widget» og skriv «lys» eller «mørk» i «Parameter».
// Tomt felt = følgjer telefonen.

const APP = 'https://bls.lillerud.com/';
const DATA = APP + 'widget.json';
const IKON = APP + 'ikon-192.png';

const PARAM = ((typeof args !== 'undefined' && args.widgetParameter) || '').trim().toLowerCase();
const TEMA = /m[øo]rk|dark|svart/.test(PARAM) ? 'mork' : /lys|kvit|light/.test(PARAM) ? 'lys' : 'auto';
const F = (lys, mork) => TEMA === 'lys' ? new Color(lys) : TEMA === 'mork' ? new Color(mork)
  : Color.dynamic(new Color(lys), new Color(mork));

// Same fargar som appen (mal.html, :root)
const BG = F('#FFFFFF', '#121A22');
const BLEKK = F('#17222D', '#E8EDF2');
const BLEKK2 = F('#3D4A57', '#C2CCD5');
const DEMPA = F('#5E6B78', '#9AA6B2');
const LINJE = F('#E2E6EA', '#2A3642');
const ELD = F('#C0361F', '#FF6B4E');
const GRUPPE = { brann: ELD, utrykking: F('#275FB0', '#6AA3FF'), alarm: F('#8A949E', '#76828E') };
const BF = ['#2E9E62', '#7DBA3C', '#E3BC00', '#F08A12', '#E0301E', '#A3123A'];
const BF_NAMN = ['Svært låg', 'Låg', 'Moderat', 'Høg', 'Svært høg', 'Ekstrem'];
const KVIT = new Color('#FFFFFF');
const DAGNAMN = ['sundag', 'måndag', 'tysdag', 'onsdag', 'torsdag', 'fredag', 'laurdag'];
const MND = ['jan.', 'feb.', 'mars', 'apr.', 'mai', 'juni', 'juli', 'aug.', 'sep.', 'okt.', 'nov.', 'des.'];

// ---------- Tid (telefonen si klokke, som er norsk tid i Noreg) ----------
const dagNr = d => Math.round(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 864e5);
const to = n => String(n).padStart(2, '0');
const kl = d => `${to(d.getHours())}:${to(d.getMinutes())}`;
const dagar = n => `${n} ${n === 1 ? 'dag' : 'dagar'}`;
function naar(iso, idag) {
  const d = new Date(iso), diff = dagNr(idag) - dagNr(d);
  if (diff <= 0) return `i dag ${kl(d)}`;
  if (diff === 1) return `i går ${kl(d)}`;
  if (diff < 7) return `${DAGNAMN[d.getDay()]} ${kl(d)}`;
  return `${d.getDate()}. ${MND[d.getMonth()]}`;
}
// «Brannalarm: ukjend årsak» → «Brannalarm», «Oppdrag – detaljar kjem» → «Oppdrag»
const kortTittel = t => String(t || '').split(/:| – /)[0];

// ---------- Små byggjeklossar ----------
function tekst(stack, t, font, farge, { linjer = 1, skala = 0.6 } = {}) {
  const x = stack.addText(String(t));
  x.font = font;
  x.textColor = farge;
  x.lineLimit = linjer;
  x.minimumScaleFactor = skala;
  return x;
}
function rad(stack) { const s = stack.addStack(); s.layoutHorizontally(); s.centerAlignContent(); return s; }
function kol(stack) { const s = stack.addStack(); s.layoutVertically(); return s; }

function topp(stack, ikon, namn) {
  const r = rad(stack);
  r.spacing = 6;
  if (ikon) { const i = r.addImage(ikon); i.imageSize = new Size(18, 18); i.cornerRadius = 5; }
  tekst(r, namn, Font.boldSystemFont(12.5), BLEKK);
  return r;
}

function brannfareStrek(stack, klasse, breidd) {
  const r = rad(stack);
  r.spacing = 3;
  const w = (breidd - 5 * 3) / 6;
  for (let i = 0; i < 6; i++) {
    const s = r.addStack();
    s.size = new Size(w, 7);
    s.cornerRadius = 2;
    s.backgroundColor = i <= klasse ? new Color(BF[klasse]) : LINJE;
  }
}

function linje(stack, namn, verdi, tillegg) {
  const r = rad(stack);
  tekst(r, namn, Font.systemFont(12), DEMPA);
  r.addSpacer();
  tekst(r, verdi, Font.boldSystemFont(17), BLEKK);
  if (tillegg) { r.addSpacer(4); tekst(r, tillegg, Font.mediumSystemFont(11), DEMPA); }
}

// ---------- Når noko pågår ----------
function pagar(w, a, idag, stor) {
  const g = new LinearGradient();
  g.colors = [new Color('#D2412A'), new Color('#A82615')];
  g.locations = [0, 1];
  g.startPoint = new Point(0, 0);
  g.endPoint = new Point(1, 1);
  w.backgroundGradient = g;
  w.url = `${APP}#b-${encodeURIComponent(a.id)}`;
  tekst(w, '●  PÅGÅR NO', Font.heavySystemFont(11), KVIT);
  w.addSpacer(stor ? 12 : 8);
  tekst(w, a.tittel, Font.heavySystemFont(stor ? 25 : 20), KVIT, { linjer: 2, skala: 0.6 });
  w.addSpacer(4);
  tekst(w, `${a.stad} · starta kl. ${kl(new Date(a.start))}`, Font.mediumSystemFont(stor ? 13 : 11.5), KVIT, { linjer: 2, skala: 0.8 }).textOpacity = 0.92;
  w.addSpacer();
  if (stor && a.melding) {
    tekst(w, `Siste melding ${kl(new Date(a.sist))}: ${a.melding}`, Font.systemFont(11), KVIT, { linjer: 3, skala: 0.8 }).textOpacity = 0.88;
  }
}

// ---------- Vanleg: dagar sidan siste hending ----------
function liten(w, d, idag, ikon) {
  const s = d.siste[0];
  const n = dagNr(idag) - dagNr(new Date(s.start));
  topp(w, ikon, 'Brannlogg');
  w.addSpacer();
  tekst(w, n, Font.heavySystemFont(60), ELD, { skala: 0.4 });
  w.addSpacer(2);
  tekst(w, `${n === 1 ? 'DAG' : 'DAGAR'} SIDAN SISTE HENDING`, Font.boldSystemFont(10), DEMPA, { linjer: 2, skala: 0.8 });
  w.addSpacer(6);
  const r = rad(w);
  tekst(r, kortTittel(s.tittel), Font.semiboldSystemFont(11.5), BLEKK2, { skala: 0.7 });
  tekst(r, ` · ${naar(s.start, idag)}`, Font.systemFont(11.5), DEMPA, { skala: 0.7 });
}

function venstre(stack, d, idag, ikon) {
  const s = d.siste[0];
  const n = dagNr(idag) - dagNr(new Date(s.start));
  topp(stack, ikon, 'Brannlogg Stord');
  stack.addSpacer(10);
  tekst(stack, 'DAGAR SIDAN SISTE HENDING', Font.boldSystemFont(10), DEMPA, { skala: 0.8 });
  const r = rad(stack);
  r.bottomAlignContent();
  tekst(r, n, Font.heavySystemFont(56), ELD, { skala: 0.4 });
  r.addSpacer(5);
  const e = kol(r);
  tekst(e, n === 1 ? 'dag' : 'dagar', Font.semiboldSystemFont(15), ELD);
  e.addSpacer(7);
  stack.addSpacer();
  const b = rad(stack);
  tekst(b, kortTittel(s.tittel), Font.semiboldSystemFont(11.5), BLEKK2, { skala: 0.7 });
  tekst(b, ` · ${naar(s.start, idag)}`, Font.systemFont(11.5), DEMPA, { skala: 0.7 });
}

function hogre(stack, d, idag) {
  stack.spacing = 9;
  if (d.siste_brann) linje(stack, 'Siste brann', String(dagNr(idag) - dagNr(new Date(d.siste_brann.start))), 'dagar');
  if (typeof d.i_aar === 'number') linje(stack, 'I år', String(d.i_aar), d.fjor ? `(i fjor ${d.fjor.tal})` : '');
  if (d.brannfare) {
    const k = Math.max(0, Math.min(5, d.brannfare.klasse | 0));
    const b = kol(stack);
    const r = rad(b);
    tekst(r, 'Skogbrannfare', Font.systemFont(12), DEMPA);
    r.addSpacer();
    tekst(r, BF_NAMN[k], Font.boldSystemFont(12), new Color(BF[k]));
    b.addSpacer(5);
    brannfareStrek(b, k, 140);
  }
  stack.addSpacer();
  tekst(stack, `Oppdatert kl. ${kl(new Date(d.oppdatert))}`, Font.systemFont(9.5), DEMPA);
}

function skiljelinje(stack, hogd) {
  const s = stack.addStack();
  s.size = new Size(1, hogd);
  s.backgroundColor = LINJE;
}

function middels(w, d, idag, ikon) {
  const h = rad(w);
  h.topAlignContent();
  const v = kol(h);
  v.size = new Size(156, 140);
  venstre(v, d, idag, ikon);
  h.addSpacer(14);
  skiljelinje(h, 140);
  h.addSpacer(14);
  const r = kol(h);
  r.size = new Size(0, 140);
  hogre(r, d, idag);
}

function stor(w, d, idag, ikon) {
  middels(w, d, idag, ikon);
  w.addSpacer(14);
  const l = w.addStack();
  l.size = new Size(0, 1);
  l.backgroundColor = LINJE;
  w.addSpacer(12);
  tekst(w, 'SISTE HENDINGAR', Font.boldSystemFont(10), DEMPA);
  w.addSpacer(8);
  const liste = kol(w);
  liste.spacing = 8;
  for (const b of d.siste.slice(0, 6)) {
    const r = rad(liste);
    r.spacing = 8;
    const p = r.addStack();
    p.size = new Size(8, 8);
    p.cornerRadius = 4;
    p.backgroundColor = GRUPPE[b.gruppe] || GRUPPE.alarm;
    tekst(r, b.tittel, Font.mediumSystemFont(13), BLEKK, { skala: 0.8 });
    r.addSpacer();
    tekst(r, naar(b.start, idag), Font.systemFont(11.5), DEMPA);
  }
  w.addSpacer();
}

// ---------- Låseskjermen ----------
function laas(w, familie, d, a, idag) {
  const s = d.siste[0];
  const n = dagNr(idag) - dagNr(new Date(s.start));
  if (familie === 'accessoryInline') {
    tekst(w, a ? `🔥 Pågår: ${kortTittel(a.tittel)}, ${a.stad}` : `🔥 ${dagar(n)} sidan siste hending`, Font.systemFont(12), KVIT);
    return;
  }
  if (familie === 'accessoryCircular') {
    w.addAccessoryWidgetBackground = true;
    const o = rad(w); o.addSpacer(); tekst(o, a ? '🔥' : n, Font.boldSystemFont(24), KVIT, { skala: 0.5 }); o.addSpacer();
    const u = rad(w); u.addSpacer(); tekst(u, a ? 'PÅGÅR' : n === 1 ? 'dag' : 'dagar', Font.semiboldSystemFont(10), KVIT); u.addSpacer();
    return;
  }
  // accessoryRectangular
  if (a) {
    tekst(w, `Pågår no: ${kortTittel(a.tittel)}`, Font.boldSystemFont(13), KVIT);
    tekst(w, `${a.stad}, starta kl. ${kl(new Date(a.start))}`, Font.systemFont(12), KVIT, { linjer: 2, skala: 0.8 });
  } else {
    tekst(w, `${dagar(n)} sidan siste hending`, Font.boldSystemFont(13), KVIT, { linjer: 2, skala: 0.8 });
    tekst(w, `${kortTittel(s.tittel)}, ${naar(s.start, idag)}`, Font.systemFont(12), KVIT, { skala: 0.8 });
  }
}

// ---------- Henting, med lagra kopi når det ikkje er nett ----------
async function hent() {
  const fm = FileManager.local();
  const fil = fm.joinPath(fm.libraryDirectory(), 'brannlogg-widget.json');
  try {
    const req = new Request(`${DATA}?t=${Date.now()}`);
    req.timeoutInterval = 15;
    const d = await req.loadJSON();
    if (req.response && req.response.statusCode === 200 && Array.isArray(d.siste) && d.siste.length) {
      fm.writeString(fil, JSON.stringify(d));
      return d;
    }
  } catch (e) { /* ingen nett: bruk kopien */ }
  if (fm.fileExists(fil)) {
    try { return JSON.parse(fm.readString(fil)); } catch (e) { /* øydelagd kopi */ }
  }
  return null;
}

async function hentIkon() {
  const fm = FileManager.local();
  const fil = fm.joinPath(fm.libraryDirectory(), 'brannlogg-ikon.png');
  if (fm.fileExists(fil)) return fm.readImage(fil);
  try {
    const bilete = await new Request(IKON).loadImage();
    fm.writeImage(fil, bilete);
    return bilete;
  } catch (e) { return null; }
}

function bygg(d, ikon, familie, no) {
  const w = new ListWidget();
  w.url = APP;
  const idag = no;
  if (!d) {
    w.backgroundColor = BG;
    tekst(w, 'Brannlogg Stord', Font.boldSystemFont(14), BLEKK);
    w.addSpacer(4);
    tekst(w, 'Fekk ikkje henta data. Sjekk nettet.', Font.systemFont(12), DEMPA, { linjer: 3 });
    return w;
  }
  // Pågår: aktiv i Politiloggen og ny melding siste 3 timar (same regel som appen og infoskjermen)
  const a = d.siste.find(b => b.aktiv && no - new Date(b.sist) < 3 * 36e5);
  if (familie.startsWith('accessory')) { laas(w, familie, d, a, idag); return w; }
  w.setPadding(15, 16, 13, 16);
  if (a) { pagar(w, a, idag, familie !== 'small'); return w; }
  w.backgroundColor = BG;
  if (familie === 'small') liten(w, d, idag, ikon);
  else if (familie === 'medium') middels(w, d, idag, ikon);
  else stor(w, d, idag, ikon);
  return w;
}

module.exports.run = async () => {
  const no = new Date();
  const familie = config.widgetFamily || 'medium';
  const [d, ikon] = await Promise.all([hent(), hentIkon()]);
  const w = bygg(d, ikon, familie, no);
  // Nye data kjem kvart kvarter. Like over midnatt blir talet på dagar rekna på nytt.
  const midnatt = new Date(no.getFullYear(), no.getMonth(), no.getDate() + 1, 0, 1);
  w.refreshAfterDate = new Date(Math.min(midnatt.getTime(), no.getTime() + 15 * 60e3));
  if (config.runsInWidget) Script.setWidget(w);
  else if (familie === 'small') await w.presentSmall();
  else if (familie === 'large') await w.presentLarge();
  else await w.presentMedium();
  Script.complete();
};

// For testing utanfor Scriptable
if (typeof module !== 'undefined') Object.assign(module.exports, { bygg, naar, kortTittel, dagNr });
