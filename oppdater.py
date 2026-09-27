"""Hentar brannar i Stord frå Politiloggen og byggjer stordbrann.html.

Køyr:  python oppdater.py                (byggjer stordbrann.html og nettside/index.html)
       python oppdater.py --sjekk        (listar nye brannar utan nyheitskjelder, skriv ingen filer)
       python oppdater.py --send-ekstra  (sender ekstra.json til GitHub, som byggjer sida på nytt)
Bygginga kvar heile time skjer i GitHub Actions (.github/workflows/oppdater.yml).
Kjelder: Politiloggen API (Politiet, NLOD 2.0), Kartverket (adresser/stadnamn).
"""
import base64
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

MAPPE = Path(__file__).parent
API = "https://api.politiloggen.politiet.no"
UA = {"User-Agent": "brannlogg-stord/1.0 (privat app)"}
KOMMUNE, KOMMUNENR = "Stord", "4614"
VINDAUGE_DAGAR = 100


def hent(url, rå=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if rå else json.loads(data.decode("utf-8"))


def les_json(namn, standard):
    fil = MAPPE / namn
    return json.loads(fil.read_text(encoding="utf-8")) if fil.exists() else standard


def skriv_json(namn, data):
    (MAPPE / namn).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- Politiloggen ----------

def hent_brann_trådar():
    trådar, skip = [], 0
    while True:
        q = urllib.parse.urlencode({"Municipalities": KOMMUNE, "TimeSpanType": "LastYear",
                                    "Take": 500, "Skip": skip})
        svar = hent(f"{API}/messagethreads?{q}")
        trådar += svar.get("messageThreads") or []
        if not svar.get("hasMoreResults"):
            break
        skip += 500
    return [t for t in trådar if (t.get("category") or "").lower() == "brann"]


def hent_bilete(melding_id):
    try:
        data = hent(f"{API}/messages/{melding_id}/image?scale=0.5", rå=True)
        if len(data) < 500:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception:
        return None


# ---------- Stadfesting (Kartverket) ----------

def geokod(namn, cache):
    if not namn:
        return None
    if namn in cache:
        return cache[namn]
    pos = None
    try:
        q = urllib.parse.urlencode({"sok": namn, "kommunenummer": KOMMUNENR, "treffPerSide": 100})
        treff = [a for a in hent(f"https://ws.geonorge.no/adresser/v1/sok?{q}").get("adresser", [])
                 if (a.get("adressenavn") or "").lower() == namn.lower()]
        if treff:
            lat = sum(a["representasjonspunkt"]["lat"] for a in treff) / len(treff)
            lon = sum(a["representasjonspunkt"]["lon"] for a in treff) / len(treff)
            pos = [round(lat, 5), round(lon, 5)]
        if not pos:
            for fuzzy in ("false", "true"):
                q = urllib.parse.urlencode({"sok": namn, "knr": KOMMUNENR, "treffPerSide": 1,
                                            "utkoordsys": 4258, "fuzzy": fuzzy})
                navn = hent(f"https://ws.geonorge.no/stedsnavn/v1/navn?{q}").get("navn", [])
                if navn:
                    p = navn[0]["representasjonspunkt"]
                    pos = [round(p["nord"], 5), round(p["øst"], 5)]
                    break
    except Exception as feil:
        print("  geokoding feila for", namn, feil)
        return None
    cache[namn] = pos
    return pos


def stad_kandidatar(område, tekst):
    ut = []
    if område:
        ut.append(område)
        utan_veg = re.sub(r"^(E|Rv\.?|Fv\.?)\s?\d+\s+(ved|i|på)\s+", "", område, flags=re.I)
        if utan_veg != område:
            ut.append(utan_veg.strip().capitalize())
        if " " in område:
            ut.append(område.split()[-1])
    ut += re.findall(r"\b([A-ZÆØÅ][a-zæøå]+(?:vegen|veien|gata|gate|vei))\b", tekst)
    ut += [m for m in re.findall(r"\bpå ([A-ZÆØÅ][a-zæøå]+)\b", tekst) if m not in ("Stord",)]
    return list(dict.fromkeys(ut))


# ---------- Klassifisering ----------

TYPAR = [
    ("pipe", r"pipebrann|skorstein|fyringsanlegg"),
    ("kjoretoy", r"bilbrann|kjøretøy|\bbil\b|bilen|buss|lastebil|traktor|motorsykkel"),
    ("baat", r"\bbåt|fritidsbåt|skip\b|ferje"),
    ("vegetasjon", r"gress|lyng|skog|kratt|\bbål|vegetasjon|kontrollert brann"),
    ("alarm", r"brannalarm|utløst alarm"),
    ("industri", r"bedrift|avfall|miljøverk|industri|verft|lagerhall|sorteringshall|fabrikk"),
    ("bygning", r"bolig|hus|leilighet|hotell|skole|høyskole|bygg|kleding|kledning|madrass|sikringsskap|matlaging|vegg|etasje"),
]

TITTEL = [
    (r"matlaging", "Røyk frå matlaging"),
    (r"sikringsskap", "Varmgang i sikringsskap"),
    (r"enebolig", "Brann i einebustad"),
    (r"rekkehus", "Brann i rekkjehus"),
    (r"leilighet", "Brann i leilegheit"),
    (r"hotell", "Brann på hotell"),
    (r"folkehøy|skole|skule", "Brann på skule"),
    (r"madrass", "Brann i bustad"),
    (r"kleding|kledning|yttervegg", "Brann i husvegg"),
    (r"eldre bolig|bolig|hus\b|huset", "Brann i bustad"),
]
TYPE_TITTEL = {"pipe": "Pipebrann", "kjoretoy": "Bilbrann", "baat": "Båtbrann",
               "vegetasjon": "Gras- og lyngbrann", "alarm": "Utløyst brannalarm",
               "industri": "Brann i industri/avfall", "bygning": "Brann i bygning", "anna": "Brann"}

NEKTING = re.compile(r"\b(ingen|ikke|ikkje|uten|utan|uvisst|usikkert)\b", re.I)


def setningar(tekst):
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", tekst) if s.strip()]


def har_positiv(tekst, mønster):
    return any(re.search(mønster, s, re.I) and not NEKTING.search(s) for s in setningar(tekst))


def klassifiser(tekst):
    lå = tekst.lower()
    btype = next((t for t, m in TYPAR if re.search(m, lå)), "anna")
    tittel = TYPE_TITTEL[btype]
    if btype == "bygning":
        tittel = next((t for m, t in TITTEL if re.search(m, lå)), tittel)

    alvorleg = har_positiv(tekst, r"overtent|full fyr|evakuer|sykehus|sjukehus|brannskade|omkom|kraftig|mye røyk|god del røyk")
    mild = re.search(r"ingen åpne flammer|ingen synlige flammer|men ikke flammer|ikke (vært )?åpne flammer|matlaging|kontrollert brann|ingen brann|ikke vært brann|varmgang|feil funnet|pipebrann", lå)
    flammar = har_positiv(tekst, r"har vært åpne flammer|inhalert|flammer i|slukket|slukking|brannen")
    alvor = 3 if alvorleg else (1 if mild and not flammar else 2)
    if alvor == 1 and tittel.startswith("Brann i "):
        tittel = "Røykutvikling i " + tittel[len("Brann i "):]

    flagg = {
        "personskade": har_positiv(tekst, r"inhalert|brannskade|til sykehus|til sjukehus|sykehuset|personskade|skadet|fått i seg røyk"),
        "evakuert": har_positiv(tekst, r"evakuer"),
        "sak": bool(re.search(r"oppretter sak|opprettet sak|sak opprettet|opprettes sak|opprettes det sak|oppretta sak|oppretter politisak", lå))
               and not re.search(r"ingen politisak|ingen sak", lå),
    }
    return btype, tittel, alvor, flagg


# ---------- Bygging ----------

NETTSIDE_HOVUD = """<!doctype html>
<html lang="nn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
"""
NETTSIDE_SLUTT = """<script>if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});</script>
</body>
</html>
"""


def skriv_nettside(side):
    """Lagar ein fullstendig HTML-dokument for GitHub Pages av same innhald."""
    delepunkt = side.index("</style>") + len("</style>")
    hovud = side[:delepunkt].replace('<meta charset="utf-8">', "", 1)
    mappe = MAPPE / "nettside"
    mappe.mkdir(exist_ok=True)
    (mappe / "index.html").write_text(
        NETTSIDE_HOVUD + hovud + "\n</head>\n<body>\n" + side[delepunkt:] + "\n" + NETTSIDE_SLUTT, encoding="utf-8")
    (mappe / ".nojekyll").touch()


def git(*arg):
    return subprocess.run(["git", "-C", str(MAPPE), *arg], capture_output=True, text=True, encoding="utf-8")


def sjekk():
    """Listar brannar siste 50 dagar som manglar nyheitskjelder. Skriv ingen filer."""
    git("pull", "--rebase", "--autostash")
    ekstra = les_json("ekstra.json", {})
    grense = datetime.now(timezone.utc) - timedelta(days=VINDAUGE_DAGAR)
    nye = 0
    for t in hent_brann_trådar():
        if datetime.fromisoformat(t["createdOn"]) < grense:
            continue
        ex = ekstra.get(t["id"], {})
        if ex.get("kjelder") or ex.get("sokt"):
            continue
        tekst = "\n".join(m.get("text") or "" for m in t.get("messages") or [])
        tittel = klassifiser(tekst)[1]
        print(f"NY_UTAN_KJELDER: {t['id']} | {t['createdOn'][:10]} | {(t.get('area') or KOMMUNE).strip()} | {tittel}")
        nye += 1
    print(f"SJEKK: OK, {nye} brannar manglar nyheitskjelder")


def send_ekstra():
    """Sender endringar i ekstra.json til GitHub. GitHub byggjer då sida på nytt."""
    git("pull", "--rebase", "--autostash")
    git("add", "ekstra.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("SENDT: ingen endringar")
        return
    git("commit", "-m", f"Nyheitslenker {datetime.now():%Y-%m-%d}\n\n"
                        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    svar = git("push")
    if svar.returncode != 0:
        print("SENDT_FEIL:", svar.stderr.strip())
        sys.exit(1)
    print("SENDT: OK – GitHub byggjer nettsida på nytt om eitt par minutt")


def neste_heile_time(no_lokal):
    return no_lokal.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def main():
    ekstra = les_json("ekstra.json", {})
    arkiv = les_json("arkiv.json", {})
    geocache = les_json("geokode.json", {})

    print("Hentar frå Politiloggen …")
    trådar = hent_brann_trådar()
    for t in trådar:
        arkiv[t["id"]] = t
    skriv_json("arkiv.json", arkiv)

    brannar = []
    for tid, t in arkiv.items():
        meldingar = sorted(t.get("messages") or [], key=lambda m: m["createdOn"])
        meldingar = [m for m in meldingar if m.get("type") != "Removed"]
        if not meldingar:
            continue
        tekst = "\n".join(m.get("text") or "" for m in meldingar)
        btype, tittel, alvor, flagg = klassifiser(tekst)
        ex = ekstra.get(tid, {})
        område = (t.get("area") or "").strip()

        pos = ex.get("pos")
        stad = område
        if not pos:
            for kand in stad_kandidatar(område, tekst):
                pos = geokod(kand, geocache)
                if pos:
                    stad = stad or kand
                    break
        if not stad:
            stad = KOMMUNE

        bilete = []
        for m in meldingar:
            if m.get("hasImage"):
                src = hent_bilete(m["id"])
                if src:
                    bilete.append({"src": src, "kreditt": "Foto: Politiet (NLOD 2.0)"})

        brannar.append({
            "id": tid,
            "tittel": ex.get("tittel", tittel),
            "type": ex.get("type", btype),
            "alvor": ex.get("alvor", alvor),
            "stad": stad,
            "start": t["createdOn"],
            "sist": t.get("lastMessageOn") or t.get("updatedOn") or t["createdOn"],
            "aktiv": bool(t.get("isActive")),
            "pos": pos,
            "flagg": {**flagg, "omkomne": bool(ex.get("omkomne"))},
            "meldingar": [{"t": m["createdOn"], "tekst": (m.get("text") or "").strip(),
                           "endra": m.get("type") == "Edited"} for m in meldingar],
            "kjelder": ex.get("kjelder", []),
            "merknad": ex.get("merknad", ""),
            "bilete": bilete,
        })
    skriv_json("geokode.json", geocache)
    brannar.sort(key=lambda b: b["start"], reverse=True)

    no = datetime.now(timezone.utc)
    no_lokal = datetime.now().astimezone()
    data = {
        "oppdatert": no.isoformat(timespec="seconds"),
        "neste": neste_heile_time(no_lokal).isoformat(timespec="seconds"),
        "vindauge": VINDAUGE_DAGAR,
        "brannar": brannar,
        "kart": les_json("kart.json", None),
    }
    json_tekst = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    mal = (MAPPE / "mal.html").read_text(encoding="utf-8")
    if "/*__DATA__*/null" not in mal:
        sys.exit("Fann ikkje /*__DATA__*/null i mal.html")
    side = mal.replace("/*__DATA__*/null", json_tekst)
    (MAPPE / "stordbrann.html").write_text(side, encoding="utf-8")
    skriv_nettside(side)
    tv_mal = MAPPE / "mal-tv.html"
    if tv_mal.exists():
        (MAPPE / "nettside" / "tv.html").write_text(
            tv_mal.read_text(encoding="utf-8").replace("/*__DATA__*/null", json_tekst), encoding="utf-8")

    grense = no - timedelta(days=VINDAUGE_DAGAR)
    siste = [b for b in brannar if datetime.fromisoformat(b["start"]) >= grense]
    print(f"OK: {len(brannar)} brannar totalt, {len(siste)} siste {VINDAUGE_DAGAR} dagar. "
          f"Skreiv stordbrann.html ({(MAPPE / 'stordbrann.html').stat().st_size // 1024} kB).")
    for b in siste:
        if not b["kjelder"] and not ekstra.get(b["id"], {}).get("sokt"):
            print(f"NY_UTAN_KJELDER: {b['id']} | {b['start'][:10]} | {b['stad']} | {b['tittel']}")
    for b in brannar:
        if not b["pos"]:
            print(f"UTAN_KARTPLASS: {b['id']} | {b['stad']}")


if __name__ == "__main__":
    if "--sjekk" in sys.argv:
        sjekk()
    elif "--send-ekstra" in sys.argv:
        send_ekstra()
    else:
        main()
