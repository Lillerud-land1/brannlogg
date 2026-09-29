"""Hentar brannar i Stord frå Politiloggen og byggjer stordbrann.html.

Køyr:  python oppdater.py                (byggjer stordbrann.html og nettside/index.html)
       python oppdater.py --sjekk        (listar nye brannar utan nyheitskjelder, skriv ingen filer)
       python oppdater.py --send-ekstra  (sender ekstra.json til GitHub, som byggjer sida på nytt)
       python oppdater.py --nyheiter     (byggjer og leitar i tillegg etter nyheitssaker i RSS-feedar)
Bygginga kvart kvarter skjer i GitHub Actions (.github/workflows/oppdater.yml).
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

# Hendingar utanom kategorien «Brann» blir tekne med når politiet nemner brannvesenet
# (sikkert) eller «nødetatene» (brannvesenet er som regel med då).
BRANNVESEN = re.compile(
    r"brannvesen|brann og redning|brannmannskap|mannskaper fra brann|brann og politi|politi og brann|"
    r"brann, politi|politi, brann|brann og ambulanse|røykdykk|frigjør|brann er på|brann på stedet|"
    r"brann rykker|brann har|brann melder|brannbil", re.I)
NODETATAR = re.compile(r"nødetatene|nødetatane|alle nødetater|alle nødetatar", re.I)


def trådtekst(t):
    return "\n".join(m.get("text") or "" for m in t.get("messages") or [])


def gruppe_for(t):
    """«brann», «brannvesen» (brannvesenet nemnt), «nodetatar» (berre «nødetatene») eller None."""
    if (t.get("category") or "").lower() == "brann":
        return "brann"
    tekst = trådtekst(t)
    if BRANNVESEN.search(tekst):
        return "brannvesen"
    if NODETATAR.search(tekst):
        return "nodetatar"
    return None


def hent_trådar():
    """Alle brannar og andre utrykkingar med brannvesenet i Stord siste år."""
    trådar, skip = [], 0
    while True:
        q = urllib.parse.urlencode({"Municipalities": KOMMUNE, "TimeSpanType": "LastYear",
                                    "Take": 500, "Skip": skip})
        svar = hent(f"{API}/messagethreads?{q}")
        trådar += svar.get("messageThreads") or []
        if not svar.get("hasMoreResults"):
            break
        skip += 500
    return [t for t in trådar if gruppe_for(t)]


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


UTR_TYPAR = [
    ("utslepp", r"diesel|\bolje|utslipp|lekkasje|\bgass|kjemikal|overfylling"),
    ("sjo", r"i sjøen|i vannet|person i vann|båt|sjøen|kaia|kaien|drukn|ferje"),
    ("trafikk", r"trafikkulykke|trafikkuhell|kollisjon|kolliderte|påkjør|utforkjøring|singelulykke|"
                r"kjørt av vegen|kjørt av veien|\bmc\b|motorsykkel|personbil|syklist|fotgjenger|tunnel|autovern"),
    ("redning", r"savnet|leting|søk etter|redningsaksjon|fastklemt|\bheis"),
    ("dyr", r"\bdyr\b|\bkatt|\bhund\b|hjort|rådyr|\bsau\b|\bhest"),
    ("ulykke", r"ulykke|falt|isen|skadet|skadd"),
]
UTR_TITTEL = [
    (r"båtkollisjon|båt.{0,40}kollider|kollider.{0,40}båt", "Båtulykke"),
    (r"diesel", "Dieselutslepp"),
    (r"\bolje", "Oljeutslepp"),
    (r"person i vann|person i sjøen|falt i sjøen|i vannet", "Person i sjøen"),
    (r"arbeidsulykke", "Arbeidsulykke"),
    (r"\bmc\b|motorsykkel", "Trafikkulykke med MC"),
    (r"syklist", "Påkøyrsle av syklist"),
    (r"fotgjenger|\bgående\b", "Påkøyrsle av fotgjengar"),
    (r"røyk", "Røyk frå køyretøy"),
    (r"tjørn|isen|kjørt i vannet|i vatnet", "Bil i vatnet"),
]
UTR_TYPE_TITTEL = {"sjo": "Hending på sjøen", "trafikk": "Trafikkulykke", "utslepp": "Utslepp",
                   "redning": "Redningsaksjon", "dyr": "Dyr i naud", "ulykke": "Ulykke", "utrykking": "Utrykking"}


def klassifiser_utrykking(tekst):
    lå = tekst.lower()
    utype = next((t for t, m in UTR_TYPAR if re.search(m, lå)), "utrykking")
    tittel = next((t for m, t in UTR_TITTEL if re.search(m, lå)), UTR_TYPE_TITTEL[utype])
    alvorleg = har_positiv(tekst, r"alvorlig skadet|hardt skadet|sykehus|sjukehus|luftambulanse|helikopter|"
                                  r"omkom|døde|livløs|kritisk|livstruende|hjertestans|gjenoppliv")
    mild = re.search(r"ingen skadet|ingen personskade|ikke meldt om personskade|ikke skadet|uskadd|uskadet|"
                     r"mindre skader|kun materielle|materielle skader|lettere skadd|lettere skadet|ingen skal være skadet", lå)
    alvor = 3 if alvorleg else (1 if mild else 2)
    flagg = {
        "personskade": har_positiv(tekst, r"skadet|skadd|sykehus|sjukehus|luftambulanse|personskade|tilsett av helse"),
        "evakuert": har_positiv(tekst, r"evakuer"),
        "sak": bool(re.search(r"oppretter sak|opprettet sak|sak opprettet|opprettes sak|oppretta sak|oppretter politisak", lå))
               and not re.search(r"ingen politisak|ingen sak", lå),
    }
    return utype, tittel, alvor, flagg


def klassifiser_tråd(t):
    """Returnerer (gruppe, type, tittel, alvor, flagg) for ein tråd frå Politiloggen."""
    gruppe = gruppe_for(t) or "brann"
    tekst = trådtekst(t)
    if gruppe == "brann":
        btype, tittel, alvor, flagg = klassifiser(tekst)
    else:
        btype, tittel, alvor, flagg = klassifiser_utrykking(tekst)
    flagg["brannvesen"] = gruppe == "brannvesen"
    return ("brann" if gruppe == "brann" else "utrykking"), btype, tittel, alvor, flagg


# ---------- Nyheitssaker (automatisk via RSS) ----------

NYHEITSFEEDAR = [
    ("Radio Haugaland", "https://radioh.no/tag/stord/feed/", False),
    ("Radio Haugaland", "https://radioh.no/feed/", False),
    ("Sunnhordland", "https://www.sunnhordland.no/rss", False),
    ("Stord24", "https://www.stord24.no/rss", False),
    ("NRK Vestland", "https://www.nrk.no/vestland/siste.rss", False),
    ("Haugesunds Avis", "https://www.h-avis.no/service/rss", False),
    ("Bømlo-Nytt", "https://www.bomlo-nytt.no/rss", True),  # True = stadnamnet må stå i saka
]
BRANNORD = re.compile(
    r"brannen|brann i|bilbrann|lyngbrann|gressbrann|grasbrann|pipebrann|husbrann|bustadbrann|boligbrann|"
    r"skogbrann|røyk|flammar|flammer|overtent|utbrent|tok fyr|tatt fyr|brannvesen|nødetat|slokk|sløkk", re.I)
UTRORD = re.compile(
    r"ulykke|kollisjon|kolliderte|trafikk|påkjør|utforkjøring|sjøen|båt|redning|nødetat|brannvesen|"
    r"utslepp|utslipp|diesel|savn|skadd|skadet", re.I)


def les_feed(url):
    """Hentar ein RSS-feed og returnerer saker som (tittel, lenke, tekst, tidspunkt)."""
    import html
    from email.utils import parsedate_to_datetime
    tekst = hent(url, rå=True).decode("utf-8", errors="replace")
    saker = []
    for blokk in re.findall(r"<item\b.*?</item>", tekst, re.S | re.I):
        def felt(namn):
            m = re.search(rf"<{namn}\b[^>]*>(.*?)</{namn}>", blokk, re.S | re.I)
            if not m:
                return ""
            verdi = re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip())
            return html.unescape(re.sub(r"<[^>]+>", " ", verdi)).strip()
        try:
            tid = parsedate_to_datetime(felt("pubDate"))
        except (TypeError, ValueError):
            continue
        if tid.tzinfo is None:
            tid = tid.replace(tzinfo=timezone.utc)
        saker.append((felt("title"), felt("link"), felt("description"), tid))
    return saker


def finn_nyheiter(brannar, alle_saker=None, lagre=True):
    """Koplar nyheitssaker til brannar ut frå tid, stad og brannord. Lagrar i auto_kjelder.json."""
    auto = les_json("auto_kjelder.json", {}) if lagre else {}
    hent_feedar = alle_saker is None
    alle_saker = alle_saker or []
    for kjelde, url, krev_stad in (NYHEITSFEEDAR if hent_feedar else []):
        try:
            alle_saker += [(kjelde, krev_stad, *s) for s in les_feed(url)]
        except Exception as feil:
            print(f"  kunne ikkje lese {kjelde} ({url}): {feil}")
    nye = 0
    for b in brannar:
        start = datetime.fromisoformat(b["start"])
        i_vindauge = lambda t: start - timedelta(hours=1) <= t <= start + timedelta(hours=48)
        andre = [x for x in brannar if x is not b and abs(datetime.fromisoformat(x["start"]) - start) < timedelta(hours=48)]
        stadord = [w.lower() for w in re.findall(r"[A-Za-zÆØÅæøå]{4,}", b["stad"]) if w.lower() != "stord"]
        for kjelde, krev_stad, tittel, lenke, tekst, tid in alle_saker:
            heil = f"{tittel} {tekst}".lower()
            nøkkelord = BRANNORD if b.get("gruppe", "brann") == "brann" else UTRORD
            if not lenke or not i_vindauge(tid) or not nøkkelord.search(heil):
                continue
            treff_stad = any(w in heil for w in stadord)
            treff_stord = "stord" in heil or "leirvik" in heil
            if not (treff_stad or (treff_stord and not krev_stad and not andre)):
                continue
            liste = auto.setdefault(b["id"], [])
            if lenke in {k["url"] for k in liste} or lenke in {k["url"] for k in b["kjelder"]} or len(liste) >= 4:
                continue
            liste.append({"kjelde": kjelde, "tittel": tittel, "url": lenke, "dato": tid.isoformat(timespec="minutes")})
            nye += 1
    if lagre:
        skriv_json("auto_kjelder.json", auto)
    print(f"NYHEITER: {len(alle_saker)} saker lesne, {nye} nye lenker kopla til brannar")
    return auto


# ---------- Hendingar som berre står i media ----------

STORD_STADER = re.compile(
    r"\b(stord|leirvik|sagvåg|heiane|litlabø|huglo|føyno|digernes|rommetveit|hystad|eldøy|eldøyane|nysæter|"
    r"vabakken|kårevik|tjødnalio|frugarden|ådland|valvatna|hamnegata|borggata|stordabrua|stordbrua|sørstokken|"
    r"fitjarvegen|meatjønn|sponavika|storamyro|åsringen|hjortåsen)\b", re.I)
HENDINGSORD = re.compile(
    r"rykte ut|rykka ut|rykket ut|rykker ut|rykkjer ut|sløkte|sløkt|slokket|slokka|slukket|slukka|brann i|brenn i|"
    r"brann på|bålbrann|ut av kontroll|spreidd seg|spredt seg|røykutvikling|trafikkulykke|trafikkuhell|kolliderte|kollisjon|utforkøyring|"
    r"utforkjøring|påkøyrd|påkjørt|sat fast|satt fast|redningsaksjon|person i sjøen|i sjøen", re.I)
IKKJE_HENDING = re.compile(r"\b(fotball|kamp|kampen|seier|serien|cup|stord il|håndball|handball|tabell)\b", re.I)


def hent_alle_saker():
    saker = []
    for kjelde, url, krev_stad in NYHEITSFEEDAR:
        try:
            saker += [(kjelde, krev_stad, *s) for s in les_feed(url)]
        except Exception as feil:
            print(f"  kunne ikkje lese {kjelde} ({url}): {feil}")
    return saker


def finn_nyheitshendingar(brannar, alle_saker, auto, ekstra, geocache):
    """Nyheitssaker om brann/ulykke på Stord som ikkje høyrer til nokon hending i Politiloggen."""
    import hashlib
    lagra = les_json("nyheitshendingar.json", {})
    kjende_url = {k["url"] for v in auto.values() for k in v}
    kjende_url |= {k["url"] for v in ekstra.values() if isinstance(v, dict) for k in v.get("kjelder", [])}
    kjende_url |= {r["url"] for r in lagra.values()}
    nye = 0
    for kjelde, krev_stad, tittel, lenke, tekst, tid in alle_saker:
        heil = f"{tittel} {tekst}"
        if not lenke or lenke in kjende_url or IKKJE_HENDING.search(heil) or not HENDINGSORD.search(heil):
            continue
        stader = [m.group(1) for m in STORD_STADER.finditer(heil)]
        if not stader:
            continue
        # Hopp over saker som truleg gjeld ei hending som alt står i Politiloggen
        if any(abs(datetime.fromisoformat(b["start"]) - tid) < timedelta(hours=12) for b in brannar):
            continue
        spesifikk = [s for s in stader if s.lower() != "stord"]
        stad = spesifikk[0].capitalize() if spesifikk else KOMMUNE
        pos = geokod(stad, geocache) if spesifikk else None
        mid = "n-" + hashlib.sha1(lenke.encode()).hexdigest()[:10]
        lagra[mid] = {"id": mid, "kjelde": kjelde, "tittel": tittel, "url": lenke, "tekst": tekst[:600],
                      "tid": tid.astimezone(timezone.utc).isoformat(timespec="seconds"), "stad": stad, "pos": pos}
        kjende_url.add(lenke)
        nye += 1
        print(f"MEDIA: ny hending frå {kjelde}: {tittel} ({stad})")
    skriv_json("nyheitshendingar.json", lagra)
    print(f"MEDIA: {nye} nye hendingar frå nyheitene")


def lag_mediehendingar(ekstra, geocache=None):
    """Gjer om lagra nyheitshendingar og manuelle hendingar (ekstra.json «_hendingar») til vanlege hendingar."""
    ut = []
    kjelder = list(les_json("nyheitshendingar.json", {}).values())
    for m in ekstra.get("_hendingar", []):
        kjelder.append({"id": m["id"], "kjelde": None, "tittel": m["tittel"], "url": None, "tekst": m.get("tekst", ""),
                        "tid": m["tid"], "stad": m.get("stad", KOMMUNE), "pos": m.get("pos"), "manuell": m})
    for r in kjelder:
        ex = ekstra.get(r["id"], {})
        if ex.get("avvis_hending"):
            continue
        tekst = f"{r['tittel']}. {r['tekst']}"
        manuell = r.get("manuell") or {}
        if not r.get("pos") and geocache is not None and r["stad"] != KOMMUNE:
            r["pos"] = geokod(r["stad"], geocache)
        gruppe = manuell.get("gruppe") or ("brann" if re.search(r"brann|brenn|\bbål|røyk|flamm|sløkk|slokk|slukk", tekst, re.I) else "utrykking")
        btype, _, alvor, flagg = klassifiser(tekst) if gruppe == "brann" else klassifiser_utrykking(tekst)
        flagg.update({"brannvesen": False, "omkomne": bool(ex.get("omkomne")), "media": True})
        eigne = manuell.get("kjelder") or ([{"kjelde": r["kjelde"], "tittel": r["tittel"], "url": r["url"]}] if r["url"] else [])
        ut.append({
            "id": r["id"], "gruppe": gruppe, "kjelde_type": "media",
            "tittel": ex.get("tittel", r["tittel"]), "type": ex.get("type", manuell.get("type", btype)),
            "alvor": ex.get("alvor", manuell.get("alvor", min(alvor, 2))),
            "stad": r["stad"], "start": r["tid"], "sist": r["tid"], "aktiv": False,
            "pos": ex.get("pos", r["pos"]), "flagg": flagg,
            "meldingar": [{"t": r["tid"], "tekst": r["tekst"] or r["tittel"], "endra": False}],
            "kjelder": eigne + [k for k in ex.get("kjelder", []) if k["url"] not in {e["url"] for e in eigne}],
            "merknad": ex.get("merknad", "Frå media – hendinga står ikkje i Politiloggen."),
            "bilete": [],
        })
    return ut


# ---------- Brannstatistikk (DSB / BRIS) – hovudkjelda for oppdraga ----------

BRIS_API = "https://brannstatistikk.no/api/v1/missionreports/search"
ABA_ÅRSAK = {"matlaging": "matlaging", "vanndamp": "vassdamp", "ukjent": "ukjend årsak", "teknisk feil": "teknisk feil",
             "manuell melder": "manuell melder", "annen røyk": "anna røyk", "arbeid på/i bygg": "arbeid i bygget",
             "trykkfall sprinkler": "trykkfall i sprinklar", "fysisk skade på anlegget": "skade på anlegget",
             "eksos": "eksos", "røyking": "røyking", "øvelse/service/test av anlegg": "test av anlegget", "annet": "anna"}
BRIS_TITTEL = {
    "Brann i bygning": "Brann i bygning", "Brann annet": "Brann (anna)", "Brann i skorstein": "Pipebrann",
    "Brann i personbil": "Bilbrann", "Brann i gress- eller innmark": "Brann i gras eller innmark",
    "Trafikkulykke": "Trafikkulykke", "Person i vann": "Person i vatnet", "Ulykke båt eller skip": "Båtulykke",
    "Dyreoppdrag": "Dyreoppdrag", "Akutt forurensning": "Akutt forureining", "Ubetydelig forurensning": "Mindre forureining",
    "Helseoppdrag annet": "Helseoppdrag", "Helseoppdrag bære/løfte": "Helseoppdrag (bering/løft)",
    "Bistand politi": "Bistand til politiet", "Andre oppdrag": "Anna oppdrag", "Naturhendelse vind": "Vind og uvêr",
    "Berging av verdier": "Berging av verdiar", "Beredskapsoppdrag": "Beredskapsoppdrag",
    "Brannhindrende tiltak komfyr": "Brannhindrande tiltak – komfyr", "Brannhindrende annet utenfor bygg": "Brannhindrande tiltak",
    "RVR uten foregående innsats": "Restverdiredning", "Avbrutt utrykning alarm": "Avbroten utrykking (alarm)",
    "Avbrutt utrykning samtale": "Avbroten utrykking", "Unødig kontroll av melding": "Kontroll av melding",
    "Unødig andre alarmer": "Unødig alarm", "Unødig alarm privatmarked": "Unødig alarm", "Oppdrag fra andre alarmer": "Oppdrag frå annan alarm",
}


def bris_kategori(namn):
    """(gruppe, type, tittel, alvor, stor) for ein oppdragstype i brannstatistikken."""
    n = (namn or "").lower()
    if not n:
        # Brannvesenet har ikkje fylt ut typen enno – blir oppdatert ved neste henting
        return "utrykking", "utrykking", "Nytt oppdrag (type ikkje registrert enno)", 1, False
    tittel = BRIS_TITTEL.get(namn, namn)
    if n.startswith("aba "):
        return "alarm", "alarm", "Brannalarm: " + ABA_ÅRSAK.get(n[4:], n[4:]), 1, False
    if n.startswith(("avbrutt", "unødig", "oppdrag fra andre alarmer", "110-oppdrag", "politi uten", "testalarm", "øvelse")):
        return "alarm", "alarm", tittel, 1, False
    if n.startswith(("brann i ", "brann annet", "skogbrann")) or n == "brann":
        btype = ("pipe" if "skorstein" in n or "pipe" in n else
                 "kjoretoy" if re.search(r"bil|kjøretøy|buss|lastebil|motorsykkel|tungt", n) else
                 "vegetasjon" if re.search(r"gress|innmark|skog|lyng|utmark|vegetasjon", n) else
                 "baat" if re.search(r"båt|skip", n) else
                 "bygning" if "bygning" in n else "anna")
        return "brann", btype, tittel, 2, True
    if "trafikk" in n:
        return "utrykking", "trafikk", tittel, 2, True
    if "person i vann" in n or "båt" in n or "skip" in n:
        return "utrykking", "sjo", tittel, 2, True
    if "forurensning" in n:
        return ("utrykking", "utslepp", tittel, 2, True) if "akutt" in n else ("utrykking", "utslepp", tittel, 1, False)
    if "dyr" in n:
        return "utrykking", "dyr", tittel, 1, False
    if "helse" in n:
        return "utrykking", "helse", tittel, 1, False
    if "brannhindrende" in n:
        return "utrykking", "brannvern", tittel, 1, False
    if "natur" in n:
        return "utrykking", "utrykking", tittel, 2, True
    if re.search(r"redning|søk|fastklem", n):
        return "utrykking", "redning", tittel, 2, True
    return "utrykking", "utrykking", tittel, 1, False


def hent_bris():
    """Alle oppdrag i Stord siste år frå brannstatistikk.no. Lagrar i bris.json (arkiv)."""
    from datetime import date
    lagra = les_json("bris.json", {})
    skip, nye = 0, 0
    try:
        while True:
            kropp = {"hitsToReturn": 200, "skipped": skip,
                     "municipalities": {"ids": [KOMMUNENR], "isMissingValue": False},
                     "periodStart": str(date.today() - timedelta(days=370)), "periodEnd": str(date.today() + timedelta(days=1)),
                     "includeAssistanceMissions": True, "includeExercises": False,
                     "includeMissionsHandledByHundredAndTen": True, "includePoliceCauseWithNoMission": False,
                     "includeRVRMissionsFromBris": False, "includeApprovedMissions": True, "onlyApprovedMissions": False}
            req = urllib.request.Request(BRIS_API, data=json.dumps(kropp).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json", **UA})
            with urllib.request.urlopen(req, timeout=60) as svar:
                d = json.loads(svar.read().decode("utf-8"))
            treff = d.get("missionReport") or []
            for m in treff:
                if (m.get("municipality") or "").lower() != KOMMUNE.lower():
                    continue
                if m["id"] not in lagra:
                    nye += 1
                lagra[m["id"]] = {"id": m["id"], "type": m.get("revisedMissionType") or "", "tid": m["callTimeUtc"].replace("Z", "+00:00"),
                                  "brannvesen": m.get("responsibleFireDepartmentName") or ""}
            skip += len(treff)
            if not treff or skip >= (d.get("totalHits") or 0):
                break
        skriv_json("bris.json", lagra)
        print(f"BRIS: {len(lagra)} oppdrag i arkivet, {nye} nye")
    except Exception as feil:
        print(f"BRIS_FEIL: kunne ikkje hente brannstatistikk ({feil}) – brukar lagra data")
    return list(lagra.values())


def kombiner_med_bris(hendingar, bris, ekstra, geocache):
    """Brannstatistikken avgjer kva som er skjedd; Politiloggen og media gir stad og detaljar."""
    no = datetime.now(timezone.utc)
    eldste_bris = min((datetime.fromisoformat(m["tid"]) for m in bris), default=no)
    prioritet = {"brann": 0, "utrykking": 1, "alarm": 2}
    oppdrag = sorted(((m, bris_kategori(m["type"])) for m in bris), key=lambda x: (prioritet[x[1][0]], x[0]["tid"]))
    brukt = set()

    def passar(h, kat):
        g = h.get("gruppe", "brann")
        if kat[0] == "brann" or kat[1] in ("brannvern", "alarm"):
            return g == "brann" or (h.get("type") == "alarm")
        return g == "utrykking"

    for m, kat in oppdrag:
        t = datetime.fromisoformat(m["tid"])
        kand = []
        for h in hendingar:
            if h["id"] in brukt or h.get("bris"):
                continue
            diff = (datetime.fromisoformat(h["start"]) - t).total_seconds() / 60
            media = h.get("kjelde_type") == "media"
            if (-60 * 36 <= -diff <= 60) if media else (-15 <= diff <= 90):
                if passar(h, kat):
                    kand.append((0 if h.get("type") == kat[1] else 1, abs(diff), h))
        if kand:
            _, _, h = min(kand, key=lambda x: (x[0], x[1]))
            brukt.add(h["id"])
            if h.get("gruppe") != kat[0]:
                h["type"] = kat[1]
            h["gruppe"] = kat[0]
            h["bris"] = {"id": m["id"], "type": m["type"], "tid": m["tid"]}
            h["stor"] = kat[4]  # brannstatistikken avgjer om det var ei større hending
            h["flagg"]["bris"] = True
            continue
        # Oppdrag berre i brannstatistikken
        bid = "d-" + m["id"]
        ex = ekstra.get(bid, {})
        if ex.get("avvis_hending"):
            continue
        stad = ex.get("stad", KOMMUNE)
        pos = ex.get("pos") or (geokod(stad, geocache) if stad != KOMMUNE else None)
        hendingar.append({
            "id": bid, "gruppe": kat[0], "kjelde_type": "bris",
            "tittel": ex.get("tittel", kat[2]), "type": ex.get("type", kat[1]), "alvor": ex.get("alvor", kat[3]),
            "stad": stad, "start": m["tid"], "sist": m["tid"], "aktiv": False, "pos": pos,
            "flagg": {"personskade": False, "evakuert": False, "sak": False, "brannvesen": True,
                      "omkomne": bool(ex.get("omkomne")), "bris": True},
            "meldingar": [{"t": m["tid"], "tekst": ex.get("tekst") or (
                f"{m['brannvesen'] or 'Brannvesenet'} registrerte oppdraget som «{m['type']}»." if m["type"] else
                f"{m['brannvesen'] or 'Brannvesenet'} har registrert eit oppdrag. Kva slags oppdrag det var, er ikkje fylt ut enno."), "endra": False}],
            "kjelder": ex.get("kjelder", []),
            "merknad": ex.get("merknad", "Frå brannstatistikken (DSB). Staden og detaljar er ikkje oppgitt der."),
            "bilete": [], "bris": {"id": m["id"], "type": m["type"], "tid": m["tid"]}, "stor": kat[4],
        })
    # Utrykkingar frå Politiloggen som brannvesenet ikkje har registrert, var dei truleg ikkje med på
    ut = []
    for h in hendingar:
        start = datetime.fromisoformat(h["start"])
        if (h.get("gruppe") == "utrykking" and not h.get("bris") and h.get("kjelde_type") not in ("media", "bris")
                and start > eldste_bris and no - start > timedelta(days=3)):
            continue
        h.setdefault("stor", True)
        ut.append(h)
    # Oppdrag berre i brannstatistikken som Claude kan finne meir om (siste 14 dagar, større hendingar)
    grense = no - timedelta(days=14)
    skriv_json("bris_utan_info.json", [{"id": h["id"], "tid": h["start"], "type": h["bris"]["type"]} for h in ut
                                        if h.get("kjelde_type") == "bris" and h["stor"] and datetime.fromisoformat(h["start"]) > grense])
    n_bris = sum(1 for h in ut if h.get("bris"))
    print(f"BRIS: {n_bris} hendingar stadfesta av brannstatistikken, {sum(1 for h in ut if h.get('kjelde_type') == 'bris')} berre der")
    return ut


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


def les_tid(tekst):
    """Les «sokt»-verdien i ekstra.json (dato eller dato+klokkeslett, norsk tid)."""
    try:
        tid = datetime.fromisoformat(tekst)
    except (TypeError, ValueError):
        return None
    return tid if tid.tzinfo else tid.astimezone()


def sjekk():
    """Listar brannar som Claude bør søkje nyheiter for. Skriv ingen filer.

    Ein brann blir lista når han ikkje er søkt på før, eller når han er under 48 timar
    gammal, manglar kjelder og det er meir enn 3 timar sidan førre søk (nye saker kjem ofte seint).
    Lenker GitHub har funne automatisk blir viste som AUTO-linjer, så Claude kan kontrollere dei.
    """
    git("pull", "--rebase", "--autostash")
    ekstra = les_json("ekstra.json", {})
    auto = les_json("auto_kjelder.json", {})
    no = datetime.now(timezone.utc)
    grense = no - timedelta(days=VINDAUGE_DAGAR)
    nye = 0
    for t in hent_trådar():
        start = datetime.fromisoformat(t["createdOn"])
        if start < grense:
            continue
        ex = ekstra.get(t["id"], {})
        sokt = les_tid(ex.get("sokt"))
        ferskt = no - start < timedelta(hours=48) and not ex.get("kjelder")
        if sokt and not (ferskt and no - sokt > timedelta(hours=3)):
            continue
        tekst = "\n".join(m.get("text") or "" for m in t.get("messages") or [])
        tittel = klassifiser_tråd(t)[2]
        print(f"NY_UTAN_KJELDER: {t['id']} | {t['createdOn'][:16]} | {(t.get('area') or KOMMUNE).strip()} | {tittel}")
        for a in auto.get(t["id"], []):
            if a["url"] not in ex.get("avvis", []):
                print(f"  AUTO: {a['url']} | {a['kjelde']} | {a['tittel']}")
        nye += 1
    for mid, r in les_json("nyheitshendingar.json", {}).items():
        ex = ekstra.get(mid, {})
        if datetime.fromisoformat(r["tid"]) < grense or ex.get("sokt") or ex.get("avvis_hending"):
            continue
        print(f"MEDIA_HENDING: {mid} | {r['tid'][:16]} | {r['stad']} | {r['tittel']} | {r['url']}")
        nye += 1
    for m in les_json("bris_utan_info.json", []):
        ex = ekstra.get(m["id"], {})
        if ex.get("sokt") or ex.get("avvis_hending"):
            continue
        print(f"BRIS_UTAN_INFO: {m['id']} | {m['tid'][:16]} | {m['type']}")
        nye += 1
    print(f"SJEKK: OK, {nye} hendingar treng nyheitssøk eller kontroll")


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


def neste_kvarter(no_lokal):
    t = no_lokal.replace(minute=no_lokal.minute // 15 * 15, second=0, microsecond=0)
    return t + timedelta(minutes=15)


def main():
    ekstra = les_json("ekstra.json", {})
    arkiv = les_json("arkiv.json", {})
    geocache = les_json("geokode.json", {})

    print("Hentar frå Politiloggen …")
    trådar = hent_trådar()
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
        gruppe, btype, tittel, alvor, flagg = klassifiser_tråd({**t, "messages": meldingar})
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
            "gruppe": gruppe,
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

    if "--nyheiter" in sys.argv:
        saker = hent_alle_saker()
        auto = finn_nyheiter(brannar, saker)
        finn_nyheitshendingar(brannar, saker, auto, ekstra, geocache)
        skriv_json("geokode.json", geocache)
    else:
        auto = les_json("auto_kjelder.json", {})
    brannar += lag_mediehendingar(ekstra, geocache)
    brannar = kombiner_med_bris(brannar, hent_bris(), ekstra, geocache)
    skriv_json("geokode.json", geocache)
    brannar.sort(key=lambda b: b["start"], reverse=True)
    for b in brannar:
        # Claude sine kontrollerte lenker (ekstra.json) først, så GitHub sine automatiske
        # – utanom dei Claude har avvist som feil.
        vekk = {k["url"] for k in b["kjelder"]} | set(ekstra.get(b["id"], {}).get("avvis", []))
        b["kjelder"] = b["kjelder"] + [k for k in auto.get(b["id"], []) if k["url"] not in vekk]

    import hashlib
    innhald = [[b["id"], b["sist"], len(b["meldingar"]), len(b["kjelder"]), b["aktiv"]] for b in brannar]
    kontrollsum = hashlib.sha1(json.dumps(innhald).encode()).hexdigest()[:12]
    no = datetime.now(timezone.utc)
    no_lokal = datetime.now().astimezone()
    data = {
        "sum": kontrollsum,
        "oppdatert": no.isoformat(timespec="seconds"),
        "neste": neste_kvarter(no_lokal).isoformat(timespec="seconds"),
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
    # Liten fil som opne sider sjekkar for å sjå om det finst nye data
    (MAPPE / "nettside" / "status.json").write_text(
        json.dumps({"oppdatert": data["oppdatert"], "neste": data["neste"], "sum": kontrollsum}), encoding="utf-8")
    tv_mal = MAPPE / "mal-tv.html"
    if tv_mal.exists():
        (MAPPE / "nettside" / "tv.html").write_text(
            tv_mal.read_text(encoding="utf-8").replace("/*__DATA__*/null", json_tekst), encoding="utf-8")

    grense = no - timedelta(days=VINDAUGE_DAGAR)
    siste = [b for b in brannar if datetime.fromisoformat(b["start"]) >= grense]
    n_brann = sum(1 for b in brannar if b["gruppe"] == "brann")
    n_alarm = sum(1 for b in brannar if b["gruppe"] == "alarm")
    print(f"OK: {len(brannar)} hendingar totalt ({n_brann} brannar, {n_alarm} alarmar), {len(siste)} siste {VINDAUGE_DAGAR} dagar. "
          f"Skreiv stordbrann.html ({(MAPPE / 'stordbrann.html').stat().st_size // 1024} kB).")
    for b in siste:
        if not b["kjelder"] and not ekstra.get(b["id"], {}).get("sokt"):
            print(f"NY_UTAN_KJELDER: {b['id']} | {b['start'][:10]} | {b['stad']} | {b['tittel']}")
    for b in brannar:
        if not b["pos"]:
            print(f"UTAN_KARTPLASS: {b['id']} | {b['stad']}")
    if "--varsle" in sys.argv:
        send_varsel(brannar)


# ---------- Varsel på mobilen (ntfy.sh) ----------

NETTSIDE = "https://lillerud-land1.github.io/brannlogg/"
VARSEL_IKON = {"trafikk": "rotating_light", "sjo": "ocean", "ulykke": "warning", "utslepp": "droplet",
               "redning": "sos", "dyr": "paw_prints", "utrykking": "rotating_light"}


def send_varsel(brannar):
    """Sender push-varsel via ntfy.sh for nye hendingar. Kanalnamnet ligg i NTFY_TOPIC (GitHub-hemmelegheit)."""
    import os
    kanal = os.environ.get("NTFY_TOPIC", "").strip()
    if not kanal:
        print("VARSEL: ikkje sett opp (NTFY_TOPIC manglar)")
        return
    fil = MAPPE / "varsla.json"
    if not fil.exists():
        # Første gong: merk alt som finst som varsla, så telefonen ikkje får 40 gamle meldingar.
        skriv_json("varsla.json", sorted(b["id"] for b in brannar))
        print(f"VARSEL: starta, {len(brannar)} gamle hendingar merkte som varsla")
        return
    varsla = set(les_json("varsla.json", []))
    grense = datetime.now(timezone.utc) - timedelta(hours=24)

    def nøklar(b):
        # Same hending kan først kome frå Politiloggen og seinare frå brannstatistikken (eller omvendt)
        return {b["id"]} | ({"d-" + b["bris"]["id"]} if b.get("bris") else set())

    nye = [b for b in brannar if not (nøklar(b) & varsla) and b.get("gruppe") != "alarm"
           and datetime.fromisoformat(b["start"]) >= grense]
    for b in sorted(nye, key=lambda x: x["start"]):
        o = datetime.fromisoformat(b["start"]).astimezone()
        brann = b["gruppe"] == "brann"
        melding = {
            "topic": kanal,
            "title": f"{'🔥' if brann else '🚨'} {b['tittel']} – {b['stad']}",
            "message": f"kl. {o:%H:%M}: {b['meldingar'][0]['tekst'][:300]}",
            "tags": ["fire"] if brann else [VARSEL_IKON.get(b["type"], "rotating_light")],
            "priority": 4 if b["alvor"] == 3 or b["aktiv"] else 3,
            "click": f"{NETTSIDE}#b-{b['id']}",
        }
        # Alle hendingar går til hovudkanalen; brannar også til «-brann»-kanalen
        kanalar = [kanal] + ([kanal + "-brann"] if brann else [])
        try:
            for emne in kanalar:
                req = urllib.request.Request("https://ntfy.sh/", data=json.dumps({**melding, "topic": emne}).encode("utf-8"),
                                             headers={"Content-Type": "application/json", **UA}, method="POST")
                urllib.request.urlopen(req, timeout=30).read()
            varsla |= nøklar(b)
            print(f"VARSEL: sendt til {len(kanalar)} kanal(ar) – {b['tittel']} ({b['stad']})")
        except Exception as feil:
            print(f"VARSEL_FEIL: {b['id']}: {feil}")
    nye_id = {n["id"] for n in nye}
    for b in brannar:
        if b["id"] not in nye_id:
            varsla |= nøklar(b)
    skriv_json("varsla.json", sorted(varsla))
    if not nye:
        print("VARSEL: ingen nye hendingar")


if __name__ == "__main__":
    if "--sjekk" in sys.argv:
        sjekk()
    elif "--send-ekstra" in sys.argv:
        send_ekstra()
    else:
        main()
