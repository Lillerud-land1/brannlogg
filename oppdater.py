"""Hentar brannar i Stord frå Politiloggen og byggjer stordbrann.html.

Køyr:  python oppdater.py                (byggjer stordbrann.html og nettside/index.html)
       python oppdater.py --sjekk        (listar nye brannar utan nyheitskjelder, skriv ingen filer)
       python oppdater.py --send-ekstra  (sender ekstra.json til GitHub, som byggjer sida på nytt)
       python oppdater.py --vakt         (varsel til privat kanal om nettsida er over ein time gammal)
       python oppdater.py --vakt-test    (testmelding til den private kanalen)
       python oppdater.py --nyheiter     (byggjer og leitar i tillegg etter nyheitssaker i RSS-feedar)
Bygginga kvart kvarter skjer i GitHub Actions (.github/workflows/oppdater.yml).
Kjelder: Politiloggen API (Politiet, NLOD 2.0), Kartverket (adresser/stadnamn).
"""
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
        tittel = "Røyk i " + tittel[len("Brann i "):]   # kort, så tittelen ikkje blir kutta på TV-lista

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


# ---------- Nyheiter: berre lenker til hendingar frå Politiloggen og brannstatistikken ----------

def hent_alle_saker():
    saker = []
    for kjelde, url, krev_stad in NYHEITSFEEDAR:
        try:
            saker += [(kjelde, krev_stad, *s) for s in les_feed(url)]
        except Exception as feil:
            print(f"  kunne ikkje lese {kjelde} ({url}): {feil}")
    return saker


# ---------- Brannstatistikk (DSB / BRIS) – hovudkjelda for oppdraga ----------

BRIS_API = "https://brannstatistikk.no/api/v1/missionreports/search"
ABA_ÅRSAK = {"matlaging": "matlaging", "vanndamp": "vassdamp", "ukjent": "ukjend årsak", "teknisk feil": "teknisk feil",
             "manuell melder": "meldeknapp", "annen røyk": "anna røyk", "arbeid på/i bygg": "byggearbeid",
             "trykkfall sprinkler": "sprinklar", "fysisk skade på anlegget": "øydelagd",
             "eksos": "eksos", "røyking": "røyking", "øvelse/service/test av anlegg": "test", "annet": "anna"}
BRIS_TITTEL = {
    "Brann i bygning": "Brann i bygning", "Brann annet": "Brann (anna)", "Brann i skorstein": "Pipebrann",
    "Brann i personbil": "Bilbrann", "Brann i gress- eller innmark": "Brann i gras eller innmark",
    "Trafikkulykke": "Trafikkulykke", "Person i vann": "Person i vatnet", "Ulykke båt eller skip": "Båtulykke",
    "Dyreoppdrag": "Dyreoppdrag", "Akutt forurensning": "Akutt forureining", "Ubetydelig forurensning": "Mindre forureining",
    "Helseoppdrag annet": "Helseoppdrag", "Helseoppdrag bære/løfte": "Helse: bering og løft",
    "Bistand politi": "Bistand til politiet", "Andre oppdrag": "Anna oppdrag", "Naturhendelse vind": "Vind og uvêr",
    "Berging av verdier": "Berging av verdiar", "Beredskapsoppdrag": "Beredskapsoppdrag",
    "Brannhindrende tiltak komfyr": "Brannhindrande: komfyr", "Brannhindrende annet utenfor bygg": "Brannhindrande tiltak",
    "RVR uten foregående innsats": "Restverdiredning", "Avbrutt utrykning alarm": "Avbroten utrykking",
    "Avbrutt utrykning samtale": "Avbroten utrykking", "Unødig kontroll av melding": "Kontroll av melding",
    "Unødig andre alarmer": "Unødig alarm", "Unødig alarm privatmarked": "Unødig alarm", "Oppdrag fra andre alarmer": "Oppdrag frå annan alarm",
}


def bris_kategori(namn):
    """(gruppe, type, tittel, alvor, stor) for ein oppdragstype i brannstatistikken."""
    n = (namn or "").lower()
    if not n:
        # Brannvesenet har ikkje fylt ut typen enno – blir oppdatert ved neste henting
        return "utrykking", "utrykking", "Oppdrag – detaljar kjem", 1, False
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


def bris_sok(start, slutt):
    """Alle oppdrag i Stord kommune i perioden (datoar som tekst, «ÅÅÅÅ-MM-DD») frå brannstatistikk.no."""
    ut, skip = [], 0
    while True:
        kropp = {"hitsToReturn": 200, "skipped": skip,
                 "municipalities": {"ids": [KOMMUNENR], "isMissingValue": False},
                 "periodStart": start, "periodEnd": slutt,
                 "includeAssistanceMissions": True, "includeExercises": False,
                 "includeMissionsHandledByHundredAndTen": True, "includePoliceCauseWithNoMission": False,
                 "includeRVRMissionsFromBris": False, "includeApprovedMissions": True, "onlyApprovedMissions": False}
        req = urllib.request.Request(BRIS_API, data=json.dumps(kropp).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json", **UA})
        with urllib.request.urlopen(req, timeout=60) as svar:
            d = json.loads(svar.read().decode("utf-8"))
        treff = d.get("missionReport") or []
        ut += [m for m in treff if (m.get("municipality") or "").lower() == KOMMUNE.lower()]
        skip += len(treff)
        if not treff or skip >= (d.get("totalHits") or 0):
            return ut


def hent_bris():
    """Alle oppdrag i Stord siste år frå brannstatistikk.no. Lagrar i bris.json (arkiv)."""
    from datetime import date
    lagra = les_json("bris.json", {})
    nye = 0
    try:
        for m in bris_sok(str(date.today() - timedelta(days=370)), str(date.today() + timedelta(days=1))):
            if m["id"] not in lagra:
                nye += 1
            lagra[m["id"]] = {"id": m["id"], "type": m.get("revisedMissionType") or "", "tid": m["callTimeUtc"].replace("Z", "+00:00"),
                              "brannvesen": m.get("responsibleFireDepartmentName") or ""}
        skriv_json("bris.json", lagra)
        print(f"BRIS: {len(lagra)} oppdrag i arkivet, {nye} nye")
    except Exception as feil:
        print(f"BRIS_FEIL: kunne ikkje hente brannstatistikk ({feil}) – brukar lagra data")
    return list(lagra.values())


def hent_fjor():
    """Talet på oppdrag i brannstatistikken frå 1. januar i fjor til same dato i fjor (til TV-sida).
    Gir None om det ikkje går – det skal aldri stoppe bygginga."""
    oslo = ZoneInfo("Europe/Oslo")
    idag = datetime.now(oslo).date()
    try:
        same_dag = idag.replace(year=idag.year - 1)
    except ValueError:                      # 29. februar
        same_dag = idag.replace(year=idag.year - 1, day=28)
    start = same_dag.replace(month=1, day=1)
    try:
        oppdrag = bris_sok(str(start - timedelta(days=1)), str(same_dag + timedelta(days=2)))   # ein dag ekstra på kvar side (tidssone)
    except Exception as feil:
        print(f"FJOR_FEIL: {feil}")
        return None
    tal = sum(1 for m in oppdrag
              if start <= datetime.fromisoformat(m["callTimeUtc"].replace("Z", "+00:00")).astimezone(oslo).date() <= same_dag)
    print(f"FJOR: {tal} oppdrag frå {start} til {same_dag}")
    return {"aar": same_dag.year, "tal": tal}


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
            if -15 <= diff <= 90:
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
        med_csp(NETTSIDE_HOVUD + hovud + "\n</head>\n<body>\n" + side[delepunkt:] + "\n" + NETTSIDE_SLUTT), encoding="utf-8")
    (mappe / ".nojekyll").touch()


# Infoskjermen ligg på ei hemmeleg adresse som berre står i GitHub-hemmelegheita TV_ADRESSE – ikkje i koden.
# Adminpanelet i appen har adressa kryptert med admin-passordet (TV_KRYPTERT i mal.html, sjå README, bolk 7).
TV_GAMAL = """<!doctype html>
<html lang="nn">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Infoskjermen har ny adresse</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;background:#0A0D11;color:#E9EDF1;font:20px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;text-align:center}h1{font-size:1.6em;margin:0 0 .4em}p{margin:0 auto;max-width:34em;color:#A9B4BF}</style>
</head>
<body>
<main><h1>Infoskjermen har fått ny adresse</h1>
<p>Denne lenka er ikkje i bruk lenger. Den nye lenka får du i Brannlogg-appen under Innstillingar → Admin-tilgang.</p></main>
</body>
</html>
"""


# Brannvern-bodskap som berre blir viste på sjølve dagen (TV-en og appen), på nynorsk, bokmål og engelsk.
# Kjelder: Røykvarslardagen 1. desember og brannvernuka i veke 38 (Norsk brannvernforening/DSB),
# bålforbod 15. april–15. september (forskrift om brannforebygging § 3), fyrverkeri kl. 18–02 nyttårsaftan (DSB).
TEMA = {
    "balforbod": {"nn": ("Bålforbodet startar", "Frå i dag til 15. september er det forbode å gjere opp eld i og nær skog og utmark."),
                  "nb": ("Bålforbudet starter", "Fra i dag til 15. september er det forbudt å gjøre opp ild i og nær skog og utmark."),
                  "en": ("Fire ban begins", "From today until 15 September, lighting fires in or near forests and open land is prohibited.")},
    "sankthans": {"nn": ("Sankthansaftan", "Sjekk om det er lov å brenne bål der du er. Ha vatn klart, og sløkk bålet heilt."),
                  "nb": ("Sankthansaften", "Sjekk om det er lov å brenne bål der du er. Ha vann klart, og slukk bålet helt."),
                  "en": ("Midsummer Eve", "Check whether bonfires are allowed where you are. Have water ready and put the fire out completely.")},
    "brannvernuka": {"nn": ("Brannvernuka", "Test røykvarslarane, sjekk sløkkjeutstyret og øv på kva de gjer om det brenn heime."),
                     "nb": ("Brannvernuka", "Test røykvarslerne, sjekk slokkeutstyret og øv på hva dere gjør hvis det brenner hjemme."),
                     "en": ("Fire Safety Week", "Test your smoke alarms, check your extinguisher and practise what to do if there is a fire at home.")},
    "royk": {"nn": ("Røykvarslardagen", "Test røykvarslarane og byt batteri om det trengst. Desember er månaden med flest brannar."),
             "nb": ("Røykvarslerdagen", "Test røykvarslerne og bytt batteri om det trengs. Desember er måneden med flest branner."),
             "en": ("Smoke Alarm Day", "Test your smoke alarms and change the batteries if needed. December is the month with the most fires.")},
    "jul": {"nn": ("God jul!", "Sløkk stearinlysa før du går frå rommet, og hald levande lys unna gardiner og pynt."),
            "nb": ("God jul!", "Slukk stearinlysene før du går fra rommet, og hold levende lys unna gardiner og pynt."),
            "en": ("Merry Christmas!", "Put out candles before you leave the room, and keep them away from curtains and decorations.")},
    "nyttar": {"nn": ("Nyttårsaftan", "Fyrverkeri er berre lov kl. 18–02, og ikkje for dei under 18 år. Bruk vernebriller og hald avstand."),
               "nb": ("Nyttårsaften", "Fyrverkeri er bare lov kl. 18–02, og ikke for dem under 18 år. Bruk vernebriller og hold avstand."),
               "en": ("New Year's Eve", "Fireworks are only allowed from 6 pm to 2 am, and not for anyone under 18. Wear safety glasses and keep your distance.")},
}
TEMA_DATO = {(4, 15): "balforbod", (6, 23): "sankthans", (12, 1): "royk", (12, 24): "jul", (12, 31): "nyttar"}


def temadagar(idag=None):
    """Temadagane i dag og i morgon (norsk tid). Sida vel sjølv rett dag, så ho stemmer òg like over midnatt."""
    idag = idag or datetime.now(ZoneInfo("Europe/Oslo")).date()
    ut = []
    for dag in (idag, idag + timedelta(days=1)):
        namn = "brannvernuka" if dag.month == 9 and dag.isocalendar()[1] == 38 else TEMA_DATO.get((dag.month, dag.day))
        if namn:
            ut.append({"dato": dag.isoformat(), **{sprak: {"tittel": t, "tekst": x} for sprak, (t, x) in TEMA[namn].items()}})
    return ut


def tv_namn():
    """Den hemmelege adressa til infoskjermen utan .html, t.d. «tv-k7m2qx» (6–16 små bokstavar/tal etter «tv-»)."""
    namn = os.environ.get("TV_ADRESSE", "").strip()
    if re.fullmatch(r"tv-[0-9a-z]{6,16}", namn):
        return namn
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.exit("TV_ADRESSE manglar eller er ugyldig – infoskjermen kan ikkje byggjast")
    return "tv-lokal"


def skriv_tv(json_tekst):
    """Byggjer infoskjermen på den hemmelege adressa. Den gamle adressa (tv.html) seier berre at ho er flytta."""
    tv_mal = MAPPE / "mal-tv.html"
    if not tv_mal.exists():
        return
    namn, mappe = tv_namn(), MAPPE / "nettside"
    side = tv_mal.read_text(encoding="utf-8").replace("/*__DATA__*/null", json_tekst)
    if side.count('href="tv.webmanifest"') != 1:
        sys.exit('Fann ikkje href="tv.webmanifest" i mal-tv.html')
    side = side.replace('href="tv.webmanifest"', f'href="{namn}.webmanifest"')
    (mappe / f"{namn}.html").write_text(med_csp(side), encoding="utf-8")
    manifest = json.loads((MAPPE / "mal-tv.webmanifest").read_text(encoding="utf-8"))
    manifest.update(id=f"{namn}.html", start_url=f"{namn}.html", scope=f"{namn}.html")
    (mappe / f"{namn}.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (mappe / "tv.html").write_text(TV_GAMAL, encoding="utf-8")


# ---------- Tryggleik ----------
# Alt som kjem utanfrå (Politiloggen, brannstatistikken, nyheitsfeedar, MET og ekstra.json, som Claude skriv etter
# nettsøk) blir kontrollert her før det kjem inn i sida, så ingen kan smugle inn skript eller farlege lenker.

TYPAR_OK = {"bygning", "kjoretoy", "vegetasjon", "pipe", "alarm", "industri", "baat", "anna", "trafikk", "sjo",
            "ulykke", "redning", "dyr", "utslepp", "utrykking", "helse", "brannvern"}   # same som TYPE i mal.html
GRUPPER_OK = {"brann", "utrykking", "alarm"}
# Nyheitslenker blir berre viste når dei går til ein av desse nettstadene (eller eit underdomene av dei).
LENKE_DOMENE = ("radioh.no", "sunnhordland.no", "stord24.no", "nrk.no", "h-avis.no", "bomlo-nytt.no", "vg.no", "bt.no",
                "tv2.no", "dagbladet.no", "aftenposten.no", "nettavisen.no", "e24.no", "abcnyheter.no", "framtida.no",
                "kvinnheringen.no", "grenda.no", "tysnesbladet.no", "politiet.no", "dsb.no", "brannstatistikk.no",
                "stord.kommune.no", "vegvesen.no", "kystverket.no", "hovedredningssentralen.no", "met.no", "yr.no")
MET_DOMENE = ("met.no", "yr.no")
CSP_MERKE = "__SKRIPT_HASHAR__"


def trygg_url(url, domene=LENKE_DOMENE):
    """Gir lenka att om ho er ei vanleg http(s)-lenke til ein godkjend nettstad, elles None.
    Stoppar mellom anna javascript:-lenker og lenker til ukjende nettstader."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000 or re.search(r"[\s\x00-\x1f\x7f\"<>\\`]", url):
        return None
    try:
        delar = urllib.parse.urlsplit(url)
        vert = (delar.hostname or "").lower()
    except ValueError:
        return None
    if delar.scheme not in ("https", "http") or "@" in delar.netloc:
        return None
    return url if any(vert == d or vert.endswith("." + d) for d in domene) else None


def _tekst(v):
    return v if isinstance(v, str) else "" if v is None else str(v)


def _tal(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _heiltal(v, lag, hog, standard):
    try:
        return min(hog, max(lag, int(v)))
    except (TypeError, ValueError, OverflowError):
        return standard


def rens_hending(b):
    """Kontrollerer éi hending før ho blir lagd inn i sida. Gir None om ho ikkje kan visast trygt."""
    bid = b.get("id")
    if not isinstance(bid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", bid):
        print(f"AVVIST_HENDING: ugyldig id {bid!r}")
        return None
    meldingar = [{"t": _tekst(m.get("t")), "tekst": _tekst(m.get("tekst")), "endra": bool(m.get("endra"))}
                 for m in (b.get("meldingar") or []) if isinstance(m, dict)]
    if not meldingar:
        print(f"AVVIST_HENDING: {bid} har ingen meldingar")
        return None
    gruppe = b.get("gruppe") if b.get("gruppe") in GRUPPER_OK else "brann"
    pos = b.get("pos")
    pos_ok = (isinstance(pos, (list, tuple)) and len(pos) == 2 and all(_tal(x) is not None for x in pos)
              and -90 <= pos[0] <= 90 and -180 <= pos[1] <= 180)
    kjelder = []
    for k in b.get("kjelder") or []:
        url = trygg_url(k.get("url")) if isinstance(k, dict) else None
        if not url:
            print(f"AVVIST_LENKE: {bid}: {(k.get('url') if isinstance(k, dict) else k)!r}")
            continue
        kjelder.append({"kjelde": _tekst(k.get("kjelde")), "tittel": _tekst(k.get("tittel")), "url": url,
                        **({"dato": _tekst(k["dato"])} if "dato" in k else {})})
    ut = {
        "id": bid,
        "gruppe": gruppe,
        "tittel": _tekst(b.get("tittel")),
        "type": b.get("type") if b.get("type") in TYPAR_OK else ("anna" if gruppe == "brann" else gruppe),
        "alvor": _heiltal(b.get("alvor"), 1, 3, 1),
        "stad": _tekst(b.get("stad")),
        "start": _tekst(b.get("start")),
        "sist": _tekst(b.get("sist")),
        "aktiv": bool(b.get("aktiv")),
        "pos": [pos[0], pos[1]] if pos_ok else None,
        "flagg": {str(k): bool(v) for k, v in b["flagg"].items()} if isinstance(b.get("flagg"), dict) else {},
        "meldingar": meldingar,
        "kjelder": kjelder,
        "merknad": _tekst(b.get("merknad")),
        # Bilete frå Politiloggen blir lagra som data:-adresser. Alt anna blir fjerna.
        "bilete": [{"src": p["src"], "kreditt": _tekst(p.get("kreditt"))} for p in (b.get("bilete") or [])
                   if isinstance(p, dict) and isinstance(p.get("src"), str)
                   and re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}", p["src"])],
    }
    if "stor" in b:
        ut["stor"] = bool(b["stor"])
    if "bris" in b:
        br = b["bris"]
        ut["bris"] = {"id": _tekst(br.get("id")), "tid": _tekst(br.get("tid")),
                      "type": br.get("type") if isinstance(br.get("type"), str) else None} if isinstance(br, dict) else None
    if b.get("kjelde_type") in ("bris", "media"):
        ut["kjelde_type"] = b["kjelde_type"]
    return ut


def rens_brannfare(bf):
    """Kontrollerer skogbrannfaren (vêrdata frå Open-Meteo og farevarsel frå MET) før han kjem inn i sida."""
    if not isinstance(bf, dict):
        return None
    sist_regn = bf.get("sist_regn")
    ut = {"tid": _tekst(bf.get("tid")), "fwi": _tal(bf.get("fwi")), "klasse": _heiltal(bf.get("klasse"), 0, 5, 0),
          "prosent": _heiltal(bf.get("prosent"), 0, 100, 0),
          "sist_regn": sist_regn if isinstance(sist_regn, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", sist_regn) else None,
          "regn_7d": _tal(bf.get("regn_7d")), "temp": _tal(bf.get("temp")), "fukt": _tal(bf.get("fukt")),
          "vind": _tal(bf.get("vind")), "varsel": None}
    v = bf.get("varsel")
    if isinstance(v, dict) and isinstance(v.get("farge"), str) and re.fullmatch(r"[a-z]{1,20}", v["farge"]):
        ut["varsel"] = {"farge": v["farge"], "til": _tekst(v.get("til")), "url": trygg_url(v.get("url"), MET_DOMENE),
                        "tekst": _tekst(v.get("tekst")) if v.get("tekst") is not None else None}
    return ut


def json_til_skript(data):
    """JSON som kan stå trygt inne i <script>: alle «<» blir \\u003c, så teksten aldri kan avslutte skriptet."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def med_csp(html):
    """Fyller inn sha256-hashar for skripta i sida i Content-Security-Policy-taggen.
    Då får berre desse skripta køyre – skript som nokon måtte klare å smugle inn, blir blokkerte av nettlesaren."""
    skript = re.findall(r"<script>(.*?)</script>", html, re.S)
    if html.count(CSP_MERKE) != 1 or len(skript) != len(re.findall(r"<script", html, re.I)) \
            or len(skript) != len(re.findall(r"</script", html, re.I)):
        sys.exit("Content-Security-Policy: fann ikkje __SKRIPT_HASHAR__ éin gong, eller skripta i malen har uventa form")
    hashar = dict.fromkeys("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
                           for s in skript)
    return html.replace(CSP_MERKE, " ".join(hashar))


def git(*arg):
    return subprocess.run(["git", "-C", str(MAPPE), *arg], capture_output=True, text=True, encoding="utf-8")


def les_tid(tekst):
    """Les «sokt»-verdien i ekstra.json (dato eller dato+klokkeslett, norsk tid)."""
    try:
        tid = datetime.fromisoformat(tekst)
    except (TypeError, ValueError):
        return None
    return tid if tid.tzinfo else tid.astimezone()


REPO = "Lillerud-land1/brannlogg"


VAKT_GRENSE = timedelta(hours=1)        # varsel når nettsida er eldre enn dette
VAKT_PAMINNING = timedelta(hours=3)     # ny påminning om problemet varer


def vakt_kanal():
    """Den private varselkanalen i ntfy: GitHub-hemmelegheita NTFY_VAKT, eller vakt_kanal.txt på PC-en.
    Står ikkje i appen eller i README – berre eigaren abonnerer på han."""
    kanal = os.environ.get("NTFY_VAKT", "").strip()
    fil = MAPPE / "vakt_kanal.txt"
    if not kanal and fil.exists():
        kanal = fil.read_text(encoding="utf-8").strip()
    return kanal if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", kanal) else ""


def vakt_send(kanal, melding):
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps({**melding, "topic": kanal}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def vakt_varsle(feil, tekst):
    """Varsel til den private kanalen når nettsida ikkje blir oppdatert (påminning kvar 3. time),
    og éi melding når ho verkar igjen. Kva som alt er sendt, blir lese frå kanalen (ntfy tek vare på meldingar i 12 timar)."""
    kanal = vakt_kanal()
    if not kanal:
        print("VAKT_VARSEL: ingen privat kanal sett opp")
        return
    siste = None
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{kanal}/json?poll=1&since=12h", headers=UA),
                                    timeout=30) as r:
            meldingar = [json.loads(l) for l in r.read().decode("utf-8").splitlines() if l.strip()]
        siste = max((m for m in meldingar if m.get("event") == "message"), key=lambda m: m["time"], default=None)
    except Exception as e:
        print(f"VAKT_VARSEL: kunne ikkje lese kanalen ({e})")
    alarm = bool(siste) and "warning" in (siste.get("tags") or [])
    if feil:
        if alarm and datetime.now(timezone.utc).timestamp() - siste["time"] < VAKT_PAMINNING.total_seconds():
            print("VAKT_VARSEL: alt varsla")
            return
        melding = {"title": "⚠️ Brannlogg blir ikkje oppdatert", "message": tekst, "tags": ["warning"],
                   "priority": 4, "click": f"https://github.com/{REPO}/actions"}
    elif alarm:
        melding = {"title": "✅ Brannlogg oppdaterer seg igjen", "message": tekst, "tags": ["white_check_mark"],
                   "priority": 3, "click": NETTSIDE}
    else:
        return
    try:
        vakt_send(kanal, melding)
        print(f"VAKT_VARSEL: sendt – {melding['title']}")
    except Exception as e:
        print(f"VAKT_VARSEL_FEIL: {e}")


def vakt():
    """Sjekkar at nettsida blir oppdatert. Er ho meir enn ein time gammal: avbryt køyringar som heng,
    startar oppdateringa på GitHub og sender varsel til den private kanalen.
    Køyrer på GitHub kvart kvarter (vakt.yml, --vakt) og på PC-en som ein del av --sjekk."""
    from urllib.error import HTTPError
    try:
        url = f"{NETTSIDE}status.json?t={int(datetime.now().timestamp())}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            oppdatert = datetime.fromisoformat(json.loads(r.read())["oppdatert"])
    except HTTPError as feil:
        print(f"VAKT: nettsida svarar med feil ({feil.code})")
        vakt_varsle(True, f"Nettsida svarar ikkje som ho skal (HTTP {feil.code}).")
        return
    except Exception as feil:
        print(f"VAKT: kunne ikkje lese status.json ({feil})")
        return
    alder = datetime.now(timezone.utc) - oppdatert
    minutt = int(alder.total_seconds() // 60)
    if alder < VAKT_GRENSE:
        print(f"VAKT: OK, nettsida vart oppdatert for {minutt} min sidan")
        vakt_varsle(False, f"Nettsida vart oppdatert for {minutt} min sidan.")
        return
    o = oppdatert.astimezone(ZoneInfo("Europe/Oslo"))
    tekst = ((f"Nettsida vart sist oppdatert for {minutt} min sidan" if minutt < 120 else
              f"Nettsida vart sist oppdatert for {minutt // 60} timar sidan")
             + f" (kl. {o:%H:%M}, {o.day}.{o.month}.). Normalt skjer det kvart kvarter.")
    try:
        svar = subprocess.run(["gh", "run", "list", "-R", REPO, "--workflow", "oppdater.yml", "--limit", "50",
                               "--json", "databaseId,status,createdAt"], capture_output=True, text=True, timeout=60)
        køyringar = json.loads(svar.stdout) if svar.returncode == 0 and svar.stdout.strip() else []
        ikkje_ferdige = [k for k in køyringar if k["status"] != "completed"]
        # Ei køyring som har hengt i over 60 minutt, blokkerer alle andre – avbryt henne (same som jobben «rydd»)
        heng = [k for k in ikkje_ferdige
                if datetime.now(timezone.utc) - datetime.fromisoformat(k["createdAt"].replace("Z", "+00:00")) > timedelta(minutes=60)]
        for k in heng:
            stopp = subprocess.run(["gh", "api", "-X", "POST", f"repos/{REPO}/actions/runs/{k['databaseId']}/force-cancel"],
                                   capture_output=True, text=True, timeout=60)
            print(f"VAKT: avbraut køyring {k['databaseId']} som hadde hengt i over 60 min" if stopp.returncode == 0
                  else f"VAKT_FEIL: kunne ikkje avbryte køyring {k['databaseId']}: {stopp.stderr.strip()}")
        if heng:
            tekst += f" Vakta avbraut {len(heng)} køyring(ar) som hang."
        if len(ikkje_ferdige) > len(heng):
            print(f"VAKT: nettsida er {minutt} min gammal, men ei oppdatering går alt")
            tekst += " Ei oppdatering går no."
        else:
            start = subprocess.run(["gh", "workflow", "run", "oppdater.yml", "-R", REPO, "--ref", "main"],
                                   capture_output=True, text=True, timeout=60)
            if start.returncode == 0:
                print(f"VAKT: nettsida var {minutt} min gammal – starta oppdateringa på GitHub")
                tekst += " Vakta har starta oppdateringa på nytt."
            else:
                print(f"VAKT_FEIL: kunne ikkje starte oppdateringa: {start.stderr.strip()}")
                tekst += " Vakta klarte ikkje å starte oppdateringa."
    except Exception as feil:
        print(f"VAKT_FEIL: {feil}")
    vakt_varsle(True, tekst)


def vakt_test():
    """Sender ei testmelding til den private kanalen."""
    kanal = vakt_kanal()
    if not kanal:
        sys.exit("Ingen privat kanal (NTFY_VAKT / vakt_kanal.txt)")
    vakt_send(kanal, {"title": "🔔 Test frå Brannlogg-vakta",
                      "message": "Varsel er sett opp. Du får melding her om nettsida ikkje blir oppdatert på meir enn "
                                 "ein time, og når ho verkar igjen.",
                      "tags": ["bell"], "priority": 3, "click": NETTSIDE})
    print("VAKT_TEST: sendt")


def sjekk():
    """Listar brannar som Claude bør søkje nyheiter for. Skriv ingen filer.

    Ein brann blir lista når han ikkje er søkt på før, eller når han er under 48 timar
    gammal, manglar kjelder og det er meir enn 3 timar sidan førre søk (nye saker kjem ofte seint).
    Lenker GitHub har funne automatisk blir viste som AUTO-linjer, så Claude kan kontrollere dei.
    """
    vakt()
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
    else:
        auto = les_json("auto_kjelder.json", {})
    # Hendingar kjem berre frå Politiloggen og brannstatistikken. Nyheiter blir aldri eigne hendingar
    # (ei sak kan nemne Stord utan at noko skjedde her), berre lenker og tilleggsinfo.
    bris = hent_bris()
    brannar = kombiner_med_bris(brannar, bris, ekstra, geocache)
    skriv_json("geokode.json", geocache)
    brannar.sort(key=lambda b: b["start"], reverse=True)
    for b in brannar:
        # Claude sine kontrollerte lenker (ekstra.json) først, så GitHub sine automatiske
        # – utanom dei Claude har avvist som feil.
        vekk = {k["url"] for k in b["kjelder"]} | set(ekstra.get(b["id"], {}).get("avvis", []))
        b["kjelder"] = b["kjelder"] + [k for k in auto.get(b["id"], []) if k["url"] not in vekk]
    brannar = [r for r in map(rens_hending, brannar) if r]

    innhald = [[b["id"], b["sist"], len(b["meldingar"]), len(b["kjelder"]), b["aktiv"]] for b in brannar]
    kontrollsum = hashlib.sha1(json.dumps(innhald).encode()).hexdigest()[:12]
    no = datetime.now(timezone.utc)
    no_lokal = datetime.now().astimezone()
    data = {
        "sum": kontrollsum,
        "oppdatert": no.isoformat(timespec="seconds"),
        "neste": (no_lokal + timedelta(minutes=15)).replace(second=0, microsecond=0).isoformat(timespec="seconds"),
        "vindauge": VINDAUGE_DAGAR,
        "brannar": brannar,
        "kart": les_json("kart.json", None),
        "brannfare": rens_brannfare(hent_brannfare()),
        "fjor": hent_fjor(),
        "temadagar": temadagar(),
    }
    json_tekst = json_til_skript(data)
    mal = (MAPPE / "mal.html").read_text(encoding="utf-8")
    if "/*__DATA__*/null" not in mal:
        sys.exit("Fann ikkje /*__DATA__*/null i mal.html")
    side = mal.replace("/*__DATA__*/null", json_tekst)
    (MAPPE / "stordbrann.html").write_text(med_csp(side), encoding="utf-8")
    skriv_nettside(side)
    # Liten fil som opne sider sjekkar for å sjå om det finst nye data
    (MAPPE / "nettside" / "status.json").write_text(
        json.dumps({"oppdatert": data["oppdatert"], "neste": data["neste"], "sum": kontrollsum,
                    "brannfare": data["brannfare"]}, ensure_ascii=False), encoding="utf-8")
    skriv_tv(json_tekst)

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
        send_brannfare_varsel(data["brannfare"])
        send_manadssamandrag(bris)


# ---------- Skogbrannfare (Fire Weather Index frå vêrdata) ----------
# Vêrdata frå Open-Meteo.com (CC BY 4.0), farevarsel frå MET Norway (api.met.no, CC BY 4.0).
# Fire Weather Index (FWI) er det kanadiske systemet som også EU (EFFIS) brukar. Det blir rekna dag for dag
# frå vêret kl. 12: temperatur, luftfukt, vind og nedbør siste døgn. Grensene for fareklassane er EFFIS sine.


VER_POS = (59.78, 5.50)   # Leirvik
MET_UA = {"User-Agent": "brannlogg-stord/1.0 github.com/Lillerud-land1/brannlogg"}
FWI_GRENSER = [0, 5.2, 11.2, 21.3, 38.0, 50.0]   # svært låg, låg, moderat, høg, svært høg, ekstrem
DMC_DAGLENGD = [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0]
DC_DAGLENGD = [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6]


def fwi_dag(forrige, temp, fukt, vind, regn, mnd):
    """Eitt døgn i FWI-systemet (Van Wagner 1987). vind i km/t, regn i mm siste 24 t. Gir (ffmc, dmc, dc, fwi)."""
    ffmc0, dmc0, dc0 = forrige
    fukt = min(fukt, 100.0)
    # Fine Fuel Moisture Code
    mo = 147.2 * (101 - ffmc0) / (59.5 + ffmc0)
    if regn > 0.5:
        rf = regn - 0.5
        auke = 42.5 * rf * math.exp(-100 / (251 - mo)) * (1 - math.exp(-6.93 / rf))
        if mo > 150:
            auke += 0.0015 * (mo - 150) ** 2 * math.sqrt(rf)
        mo = min(mo + auke, 250)
    ed = 0.942 * fukt ** 0.679 + 11 * math.exp((fukt - 100) / 10) + 0.18 * (21.1 - temp) * (1 - math.exp(-0.115 * fukt))
    if mo > ed:
        ko = 0.424 * (1 - (fukt / 100) ** 1.7) + 0.0694 * math.sqrt(vind) * (1 - (fukt / 100) ** 8)
        m = ed + (mo - ed) * 10 ** (-ko * 0.581 * math.exp(0.0365 * temp))
    else:
        ew = 0.618 * fukt ** 0.753 + 10 * math.exp((fukt - 100) / 10) + 0.18 * (21.1 - temp) * (1 - math.exp(-0.115 * fukt))
        if mo < ew:
            k1 = 0.424 * (1 - ((100 - fukt) / 100) ** 1.7) + 0.0694 * math.sqrt(vind) * (1 - ((100 - fukt) / 100) ** 8)
            m = ew - (ew - mo) * 10 ** (-k1 * 0.581 * math.exp(0.0365 * temp))
        else:
            m = mo
    ffmc = max(0.0, min(101.0, 59.5 * (250 - m) / (147.2 + m)))
    # Duff Moisture Code
    if regn > 1.5:
        rw = 0.92 * regn - 1.27
        wmi = 20 + 280 / math.exp(0.023 * dmc0)
        b = 100 / (0.5 + 0.3 * dmc0) if dmc0 <= 33 else (14 - 1.3 * math.log(dmc0) if dmc0 <= 65 else 6.2 * math.log(dmc0) - 17.2)
        wmr = wmi + 1000 * rw / (48.77 + b * rw)
        pr = max(0.0, 43.43 * (5.6348 - math.log(wmr - 20)))
    else:
        pr = dmc0
    rk = 1.894 * (max(temp, -1.1) + 1.1) * (100 - fukt) * DMC_DAGLENGD[mnd - 1] * 1e-4
    dmc = max(0.0, pr + rk)
    # Drought Code
    if regn > 2.8:
        rw = 0.83 * regn - 1.27
        smi = 800 * math.exp(-dc0 / 400)
        dr = max(0.0, dc0 - 400 * math.log(1 + 3.937 * rw / smi))
    else:
        dr = dc0
    pe = max(0.0, (0.36 * (max(temp, -2.8) + 2.8) + DC_DAGLENGD[mnd - 1]) / 2)
    dc = dr + pe
    # Initial Spread Index, Buildup Index og Fire Weather Index
    fm = 147.2 * (101 - ffmc) / (59.5 + ffmc)
    isi = 19.115 * math.exp(-0.1386 * fm) * (1 + fm ** 5.31 / 4.93e7) * math.exp(0.05039 * vind)
    if dmc == 0 and dc == 0:
        bui = 0.0
    elif dmc <= 0.4 * dc:
        bui = 0.8 * dc * dmc / (dmc + 0.4 * dc)
    else:
        bui = dmc - (1 - 0.8 * dc / (dmc + 0.4 * dc)) * (0.92 + (0.0114 * dmc) ** 1.7)
    bui = max(0.0, bui)
    bb = 0.1 * isi * (0.626 * bui ** 0.809 + 2) if bui <= 80 else 0.1 * isi * (1000 / (25 + 108.64 * math.exp(-0.023 * bui)))
    fwi = bb if bb <= 1 else math.exp(2.72 * (0.434 * math.log(bb)) ** 0.647)
    return ffmc, dmc, dc, fwi


def fwi_prosent(fwi):
    """Plassering på skalaen svært låg–ekstrem som prosent (kvar fareklasse får like stor del av skalaen)."""
    for i in range(5, -1, -1):
        if fwi >= FWI_GRENSER[i]:
            lo = FWI_GRENSER[i]
            hi = FWI_GRENSER[i + 1] if i < 5 else 75.0
            return i, round(min(100.0, (i + min(1.0, (fwi - lo) / (hi - lo))) / 6 * 100))
    return 0, 0


def hent_brannfare():
    """Skogbrannfare for Stord no. Gir None om vêrdata manglar – det skal aldri stoppe bygginga."""
    lat, lon = VER_POS
    try:
        req = urllib.request.Request(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation"
            "&past_days=92&forecast_days=2&timezone=Europe%2FOslo", headers=MET_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            h = json.loads(r.read().decode("utf-8"))["hourly"]
    except Exception as feil:
        print(f"BRANNFARE_FEIL: {feil}")
        return None
    tider = h["time"]
    rad = {t: i for i, t in enumerate(tider)}
    no = datetime.now(ZoneInfo("Europe/Oslo")).replace(tzinfo=None)
    no_tekst = no.strftime("%Y-%m-%dT%H:00")
    idag = no.strftime("%Y-%m-%d")
    # FWI dag for dag, frå kl. 12 til kl. 12. Startverdiane er standardverdiane for systemet.
    koder, fwi, dagar = (85.0, 6.0, 15.0), 0.0, sorted({t[:10] for t in tider})
    for dag in dagar[1:]:
        i = rad.get(dag + "T12:00")
        if i is None or i < 24 or dag > idag:
            continue
        regn = sum(x or 0 for x in h["precipitation"][i - 23:i + 1])
        vals = [h["temperature_2m"][i], h["relative_humidity_2m"][i], h["wind_speed_10m"][i]]
        if None in vals:
            continue
        *koder_ny, fwi = fwi_dag(koder, *vals, regn, int(dag[5:7]))
        koder = tuple(koder_ny)
    klasse, prosent = fwi_prosent(fwi)
    # Sist det regna minst 1 mm på eit døgn, og regn siste 7 dagar
    per_dag = {}
    for t, mm in zip(tider, h["precipitation"]):
        if t <= no_tekst and mm is not None:
            per_dag[t[:10]] = per_dag.get(t[:10], 0) + mm
    regndagar = [d for d, mm in per_dag.items() if mm >= 1.0]
    sist_regn = max(regndagar) if regndagar else None
    j = rad.get(no_tekst, len(tider) - 1)
    regn_7d = sum(x or 0 for x in h["precipitation"][max(0, j - 167):j + 1])
    ut = {"tid": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fwi": round(fwi, 1), "klasse": klasse, "prosent": prosent,
          "sist_regn": sist_regn, "regn_7d": round(regn_7d, 1), "temp": h["temperature_2m"][j],
          "fukt": h["relative_humidity_2m"][j], "vind": round(h["wind_speed_10m"][j] / 3.6, 1), "varsel": None}
    # Offisielt farevarsel om skogbrannfare frå MET, om det finst
    try:
        req = urllib.request.Request(f"https://api.met.no/weatherapi/metalerts/2.0/current.json?lat={lat}&lon={lon}", headers=MET_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            for f in json.loads(r.read().decode("utf-8")).get("features", []):
                p = f["properties"]
                if p.get("event") == "forestFire":
                    nivaa = p.get("awareness_level", "2; yellow").split(";")
                    ut["varsel"] = {"farge": nivaa[1].strip(), "til": f["when"]["interval"][1], "url": p.get("web"),
                                    "tekst": p.get("description")}
                    break
    except Exception as feil:
        print(f"FAREVARSEL_FEIL: {feil}")
    return ut


# ---------- Varsel på mobilen (ntfy.sh) ----------

NETTSIDE = "https://bls.lillerud.com/"
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


BF_NAMN = ["svært låg", "låg", "moderat", "høg", "svært høg", "ekstrem"]
MET_FARGE = {"yellow": ("gult", "🟡"), "orange": ("oransje", "🟠"), "red": ("raudt", "🔴")}


def send_ntfy(melding, kanalar=("", "-brann")):
    """Sender same melding til hovudkanalen og «-brann»-kanalen (eller berre dei som er nemnde i kanalar)."""
    kanal = os.environ.get("NTFY_TOPIC", "").strip()
    if not kanal:
        return False
    for emne in (kanal + k for k in kanalar):
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps({**melding, "topic": emne}).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **UA}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()
    return True


def send_brannfare_varsel(bf):
    """Varsel når skogbrannfaren stig til høg eller meir, og når MET sender farevarsel om skogbrann.
    Kvart nivå blir varsla éin gong. Nivåa blir nullstilte når faren fell til låg, så det ikkje kjem
    nye meldingar kvar gong faren vippar rundt grensa."""
    if not bf:
        return
    varsla = set(les_json("varsla.json", []))
    før = set(varsla)
    k = bf["klasse"]
    if k <= 1:
        varsla -= {f"skogbrannfare-{n}" for n in (3, 4, 5)}
    nye = [n for n in (3, 4, 5) if n <= k and f"skogbrannfare-{n}" not in varsla]
    if nye:
        d = datetime.fromisoformat(bf["sist_regn"]) if bf["sist_regn"] else None
        regn = f" Sist det regna minst 1 mm var {d.day}.{d.month}." if d else " Det har ikkje regna minst 1 mm på over tre månader."
        melding = {
            "title": f"🌲🔥 {BF_NAMN[k].capitalize()} skogbrannfare på Stord",
            "message": f"Skogbrannfaren er no {BF_NAMN[k]} ({bf['prosent']} %).{regn} Ver forsiktig med eld ute, grilling og sigarettar.",
            "tags": ["evergreen_tree", "fire"], "priority": 4 if k >= 4 else 3, "click": NETTSIDE,
        }
        try:
            if send_ntfy(melding):
                varsla |= {f"skogbrannfare-{n}" for n in (3, 4, 5) if n <= k}
                print(f"VARSEL: skogbrannfare {BF_NAMN[k]} ({bf['prosent']} %)")
        except Exception as feil:
            print(f"VARSEL_FEIL: skogbrannfare: {feil}")
    v = bf.get("varsel")
    if not v:
        varsla = {x for x in varsla if not x.startswith("met-skogbrann-")}
    elif f"met-skogbrann-{v['farge']}" not in varsla:
        namn, merke = MET_FARGE.get(v["farge"], (v["farge"], "⚠️"))
        til = datetime.fromisoformat(v["til"]).astimezone(ZoneInfo("Europe/Oslo"))
        melding = {
            "title": f"{merke} Farevarsel om skogbrannfare – {namn} nivå",
            "message": (v.get("tekst") or "Meteorologisk institutt har sendt ut farevarsel om skogbrannfare.") + f" Gjeld til {til:%d.%m. kl. %H:%M}.",
            "tags": ["warning", "evergreen_tree"], "priority": 4 if v["farge"] in ("orange", "red") else 3,
            "click": v.get("url") or NETTSIDE,
        }
        try:
            if send_ntfy(melding):
                varsla.add(f"met-skogbrann-{v['farge']}")
                print(f"VARSEL: farevarsel skogbrann ({namn})")
        except Exception as feil:
            print(f"VARSEL_FEIL: farevarsel skogbrann: {feil}")
    if varsla != før:
        skriv_json("varsla.json", sorted(varsla))


MND_NAMN = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"]


def manadssamandrag(bris, idag):
    """(tittel, tekst) for førre månad: oppdrag i brannstatistikken etter gruppe, og same månad i fjor."""
    oslo = ZoneInfo("Europe/Oslo")
    slutt = idag.replace(day=1) - timedelta(days=1)                 # siste dag i førre månad
    start = slutt.replace(day=1)
    dato = lambda tid: datetime.fromisoformat(tid.replace("Z", "+00:00")).astimezone(oslo).date()
    mnd = [m for m in bris if start <= dato(m["tid"]) <= slutt]
    grupper = {"brann": 0, "utrykking": 0, "alarm": 0}
    for m in mnd:
        if m["type"]:
            grupper[bris_kategori(m["type"])[0]] += 1
    utan_type = sum(1 for m in mnd if not m["type"])
    fl = lambda n, ein, fleire: f"{n} {ein if n == 1 else fleire}"
    tekst = (f"{fl(grupper['brann'], 'brann', 'brannar')}, {fl(grupper['utrykking'], 'anna oppdrag', 'andre oppdrag')} "
             f"og {fl(grupper['alarm'], 'alarm', 'alarmar')} utan brann.")
    if utan_type:
        tekst += f" {fl(utan_type, 'oppdrag har', 'oppdrag har')} ikkje fått type enno."
    try:
        start_f = start.replace(year=start.year - 1)
        slutt_f = (start_f.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        fjor = sum(1 for m in bris_sok(str(start_f - timedelta(days=1)), str(slutt_f + timedelta(days=2)))
                   if start_f <= dato(m["callTimeUtc"]) <= slutt_f)
        tekst += f" Same månad i fjor: {fjor} oppdrag."
    except Exception as feil:
        print(f"SAMANDRAG: fekk ikkje tal for i fjor ({feil})")
    tittel = f"📊 {MND_NAMN[start.month - 1].capitalize()} {start.year}: {fl(len(mnd), 'oppdrag', 'oppdrag')}"
    return tittel, tekst + " (Tal frå brannstatistikken.)"


def send_manadssamandrag(bris, no=None):
    """Samandrag av førre månad til hovudkanalen, éin gong: første køyring etter kl. 9 den 1. i månaden
    (eller seinast den 3., om GitHub skulle ha stoppa)."""
    no = no or datetime.now(ZoneInfo("Europe/Oslo"))
    if no.day > 3 or (no.day == 1 and no.hour < 9):
        return
    nokkel = f"manad-{(no.date().replace(day=1) - timedelta(days=1)):%Y-%m}"
    varsla = set(les_json("varsla.json", []))
    if nokkel in varsla:
        return
    tittel, tekst = manadssamandrag(bris, no.date())
    melding = {"title": tittel, "message": tekst, "tags": ["bar_chart"], "priority": 3, "click": NETTSIDE + "#statistikk"}
    try:
        if send_ntfy(melding, kanalar=("",)):
            varsla.add(nokkel)
            skriv_json("varsla.json", sorted(varsla))
            print(f"VARSEL: månadssamandrag sendt – {tittel}")
    except Exception as feil:
        print(f"VARSEL_FEIL: månadssamandrag: {feil}")


if __name__ == "__main__":
    if "--sjekk" in sys.argv:
        sjekk()
    elif "--send-ekstra" in sys.argv:
        send_ekstra()
    elif "--vakt" in sys.argv:
        vakt()
    elif "--vakt-test" in sys.argv:
        vakt_test()
    else:
        main()
