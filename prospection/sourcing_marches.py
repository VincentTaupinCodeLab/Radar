"""Prospects pour Radar Marchés attribués : négoces de matériaux, loueurs de matériel et
échafaudeurs indépendants, repérés dans OpenStreetMap (données ouvertes, ODbL).

Pour chaque établissement ayant un site web, on lit le site (accueil, contact, mentions
légales) et on ne retient que l'adresse générique publiée par l'entreprise elle-même
(scrape.scan). Les grandes enseignes nationales sont écartées : leurs agences n'achètent pas.

Sortie : prospects_marches.csv (même format que prospects.csv)
Lancement : python sourcing_marches.py [--max 120]
"""
import concurrent.futures as cf
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from scrape import scan  # noqa: E402

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
# Une requête par type (une requête unique sur toute la France dépasse le délai des serveurs).
FILTRES = [
    '["shop"="trade"]["trade"~"building_supplies|timber|bricks"]',
    '["shop"="tool_hire"]',
    '["craft"="scaffolder"]',
    '["shop"="trade"]["trade"~"sanitary|plumbing|tiles|electrical"]',
    '["shop"="trade"][!"trade"]',
]
FRANCE = "(41.3,-5.2,51.1,9.6)"  # métropole et Corse


# Enseignes nationales ou groupes intégrés : agences sans pouvoir d'achat local.
EXCLUS = re.compile(
    r"point\.?\s?p|kiloutou|loxam|leroy|castorama|brico|weldom|mr\.?\s?bricolage|cedeo|rexel|sonepar|"
    r"w[uü]rth|dispano|lapeyre|tereva|richardson|aubade|samse|chausson|vm\s|vm matériaux|bigmat\.fr|"
    r"gedimat\.fr|tout\s?faire\.fr|plateforme du b[âa]timent|hilti|manutan|yesss|cge distribution|"
    r"foussier|descours|prolians|dmbp|bricoman|ikea|but\b|lidl|carrefour|wurth",
    re.I)


def categorie(tags: dict) -> str:
    if tags.get("craft") == "scaffolder":
        return "echafaudage"
    if tags.get("shop") == "tool_hire":
        return "location"
    t = tags.get("trade", "")
    if t in ("sanitary", "plumbing", "electrical", "tiles"):
        return "second_oeuvre"
    return "negoce"


def overpass() -> list:
    import time
    elements = []
    for f in FILTRES:
        q = f"[out:json][timeout:170];nwr{f}{FRANCE};out tags;"
        for essai in range(6):
            url = OVERPASS[essai % len(OVERPASS)]
            try:
                data = urllib.parse.urlencode({"data": q}).encode()
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "RadarEntreprises/1.0"})
                with urllib.request.urlopen(req, timeout=200) as r:
                    got = json.load(r)["elements"]
                print(f"{f} : {len(got)} éléments ({url.split('/')[2]})")
                elements += got
                break
            except Exception as ex:
                print(f"{f} : échec sur {url.split('/')[2]} ({ex}), nouvel essai")
                time.sleep(10)
    if not elements:
        raise SystemExit("Overpass indisponible")
    return elements


def main(maxi: int = 120):
    deja = {r["email"].split("@")[1] for r in csv.DictReader(open(ROOT / "prospects.csv", encoding="utf-8"))}
    seeds, domaines = [], set()
    for el in overpass():
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website") or ""
        nom = t.get("name") or t.get("brand") or ""
        if not site or not nom:
            continue
        if not site.startswith("http"):
            site = "https://" + site
        dom = urllib.parse.urlparse(site).netloc.lower().removeprefix("www.")
        if not dom or dom in domaines or dom in deja:
            continue
        if EXCLUS.search(" ".join([nom, t.get("brand", ""), t.get("operator", ""), dom])):
            continue
        cp = t.get("addr:postcode", "")
        domaines.add(dom)
        seeds.append({"categorie": categorie(t), "site": f"https://{dom}/", "nom_osm": nom,
                      "cp_osm": cp})
    print(f"{len(seeds)} établissements avec site, hors enseignes nationales")
    # Équilibre : on lit d'abord les négoces et loueurs, cibles les plus directes.
    # Alternance des catégories pour une liste équilibrée (négoces, loueurs, échafaudeurs…).
    paquets = {}
    for s in seeds:
        paquets.setdefault(s["categorie"], []).append(s)
    seeds = [s for grp in __import__("itertools").zip_longest(*paquets.values()) for s in grp if s]
    with cf.ThreadPoolExecutor(16) as ex:
        res = list(ex.map(scan, seeds[:maxi * 4]))
    out, vus = [], set()
    for r in res:
        email = r.get("email", "")
        if not email or email in vus:
            continue
        dom_mail = email.split("@")[1]
        if not re.search(r"\.(fr|com|eu|net|org|bzh|re|pro|biz|info|immo|paris|alsace|corsica)$", dom_mail):
            continue  # entreprise étrangère ou adresse douteuse
        vus.add(email)
        cp = r.get("cp_osm") or r.get("code_postal") or ""
        dept = cp[:3] if cp.startswith("97") else cp[:2]
        if not re.fullmatch(r"\d{5}", cp) or not dept:
            continue
        out.append({"categorie": r["categorie"], "nom": r["nom_osm"][:80], "site": r["site"],
                    "email": email, "code_postal": cp, "departement": dept, "siren": r.get("siren", "")})
        if len(out) >= maxi:
            break
    with open(ROOT / "prospects_marches.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["categorie", "nom", "site", "email", "code_postal",
                                          "departement", "siren"])
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    print(f"{len(out)} prospects retenus :", dict(Counter(r["categorie"] for r in out)))


if __name__ == "__main__":
    main(int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 120)
