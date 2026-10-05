"""Radar entreprises — pipeline v1.

Récupère les annonces BODACC (créations + ventes/cessions de fonds),
les normalise, les classe par secteur, les stocke dans SQLite (dédoublonnées)
et produit des échantillons CSV + HTML par secteur.

Usage :
    python pipeline.py --since 2026-09-28 --region 75
    python pipeline.py --days 7 --dept 16 17 33
"""
import argparse
import csv
import datetime as dt
import html
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

BODACC = ("https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/"
          "catalog/datasets/annonces-commerciales/exports/json")
ROOT = Path(__file__).parent
DB = ROOT / "radar.db"
OUT = ROOT / "out"

# --- Classification sectorielle par mots-clés (sur le libellé d'activité) ---
# Ordre important : le premier secteur qui matche gagne. Les catégories
# « pièges » (holdings/SCI, livreurs de plateformes, vente en ligne) passent
# d'abord pour ne pas polluer les secteurs à forte valeur (restauration, BTP).
SECTORS = {
    "holding_sci": [
        r"prise de participations?", r"holding", r"acquisition.{0,40}propriete",
        r"propriete.{0,40}administration", r"\bsci\b", r"gestion de son patrimoine",
        r"location nue", r"exploitation par bail", r"biens immobiliers", r"marchand de biens",
        r"construction.{0,30}(immeubles?|biens)", r"parts? de societes? immobili",
    ],
    "transport_livraison": [
        r"livraison.{0,40}(velo|plate-?formes?|scooter|compte de)", r"transport",
        r"vtc", r"taxi", r"demenag", r"coursier", r"messagerie",
        r"repas a domicile.{0,20}velo",
    ],
    "ecommerce": [
        r"vente en ligne", r"e-?commerce", r"vente a distance", r"sur internet",
        r"dropshipping", r"marketplace", r"plate-?formes? numerique",
    ],
    "restauration": [
        r"restaura(?!tion d(e|')\s?(meubles?|tableaux|objets|vehicules?|oeuvres|bateaux|batiments?|anciens?))", r"brasserie", r"\bbar\b", r"\bbars\b", r"cafe", r"debit de boissons",
        r"pizz", r"snack", r"traiteur", r"creperie", r"food ?truck", r"burger",
        r"kebab", r"sandwich", r"salon de the", r"vente a emporter", r"plats? a emporter",
        r"licence iv", r"bistro", r"glacier", r"sushi", r"chef(fe)? (prive|a domicile)", r"cuisin(e|ier) a domicile",
    ],
    "commerce_alimentaire": [
        r"boulang", r"patisser", r"boucher", r"charcut", r"epicerie", r"primeur",
        r"fromag", r"poissonn", r"caviste", r"vins? et spiritueux", r"superette",
        r"alimentation generale", r"chocolat", r"fruits et legumes", r"torrefa",
    ],
    "beaute_bien_etre": [
        r"coiff", r"esthetique", r"institut de beaute", r"onglerie", r"prothesie ongulaire",
        r"barbier", r"massage", r"toilettage", r"spa\b", r"tatou", r"maquillage", r"soins? du corps",
        r"bien[- ]etre", r"extension de cils",
    ],
    "btp": [
        r"macon", r"plomb", r"electric", r"menuis", r"charpent", r"couvreur|couverture",
        r"travaux de peinture", r"peintre en bat", r"peinture (interieure|exterieure|en batiment|de batiment)", r"platr", r"carrel", r"renovation", r"batiment", r"terrassement",
        r"chauffag", r"isolation", r"travaux", r"second oeuvre", r"gros oeuvre", r"facade",
        r"pose (de|et)", r"multiservices?", r"bricolage", r"ramonage", r"poele",
    ],
    "commerce_detail": [
        r"pret[- ]a[- ]porter", r"vetement", r"chaussure", r"bijou", r"fleur",
        r"vente au detail", r"commerce de detail", r"boutique", r"articles? de",
        r"decoration", r"cadeaux", r"librairie", r"tabac", r"presse",
    ],
    "auto_moto": [
        r"garage", r"mecanique", r"carrosserie", r"vehicules?", r"automobile",
        r"pneu", r"lavage auto", r"controle technique",
    ],
    "immobilier": [
        r"immobili", r"marchand de biens", r"location de logements?", r"lmnp",
        r"gestion locative", r"location meublee",
    ],
    "conseil_services_b2b": [
        r"conseil", r"consult", r"formation", r"informatique", r"logiciel",
        r"communication", r"marketing", r"apporteur d'affaires", r"secretariat",
        r"nettoyage",
    ],
}
SECTOR_RX = {k: re.compile("|".join(v)) for k, v in SECTORS.items()}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


def _match(text: str) -> str | None:
    for sector, rx in SECTOR_RX.items():
        if rx.search(text):
            return sector
    return None


def classify(activite: str) -> str:
    """L'activité principale est presque toujours énoncée en premier :
    on classe d'abord le premier segment, puis le texte complet."""
    a = norm(activite)
    head = re.split(r"\s[-–]\s|;|\.\s", a, maxsplit=1)[0]
    return _match(head) or _match(a) or "autre"


def _loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return None


def _first(x):
    """Les champs BODACC sont soit un objet, soit une liste d'objets."""
    if isinstance(x, list):
        return x[0] if x else {}
    return x or {}


def fetch(where: str, retries: int = 3) -> list:
    url = BODACC + "?" + urllib.parse.urlencode({"where": where})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # réseau / 429
            if attempt == retries - 1:
                raise
            print(f"  retry ({e})", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return []


def normalize(rec: dict) -> dict:
    pers = _first((_loads(rec.get("listepersonnes")) or {}).get("personne"))
    etab = _first((_loads(rec.get("listeetablissements")) or {}).get("etablissement"))
    acte = _loads(rec.get("acte")) or {}
    adr = etab.get("adresse") or pers.get("adresse") or {}

    pp = pers.get("typePersonne") == "pp"
    if pp:
        denomination = pers.get("nomCommercial") or pers.get("enseigne") or ""
    else:
        denomination = pers.get("denomination") or ""
    enseigne = etab.get("enseigne") or pers.get("nomCommercial") or ""
    activite = etab.get("activite") or pers.get("activite") or ""
    siren = (rec.get("registre") or [""])[0].replace(" ", "")
    adresse = " ".join(str(adr.get(k, "")) for k in
                       ("numeroVoie", "typeVoie", "nomVoie")).strip()

    return {
        "id": rec["id"],
        "date_parution": rec.get("dateparution"),
        "type": rec.get("familleavis"),            # creation | vente
        "siren": siren,
        "personne_physique": int(pp),
        "denomination": denomination,
        "enseigne": enseigne,
        "forme_juridique": pers.get("formeJuridique", ""),
        "activite": activite,
        "secteur": classify(activite + " " + enseigne + " " + denomination),
        "origine_fonds": etab.get("origineFonds", ""),
        "adresse": adresse,
        "cp": adr.get("codePostal") or rec.get("cp") or "",
        "ville": adr.get("ville") or rec.get("ville") or "",
        "departement": rec.get("numerodepartement"),
        "date_debut_activite": (acte.get("dateCommencementActivite") or ""),
        "url": rec.get("url_complete"),
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS annonces (
  id TEXT PRIMARY KEY, date_parution TEXT, type TEXT, siren TEXT,
  personne_physique INTEGER, denomination TEXT, enseigne TEXT,
  forme_juridique TEXT, activite TEXT, secteur TEXT, origine_fonds TEXT,
  adresse TEXT, cp TEXT, ville TEXT, departement TEXT,
  date_debut_activite TEXT, url TEXT,
  inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_sect ON annonces(secteur, departement, date_parution);
"""


def store(rows: list) -> int:
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    cols = list(rows[0].keys()) if rows else []
    before = con.total_changes
    con.executemany(
        f"INSERT OR IGNORE INTO annonces ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    new = con.total_changes - before
    con.close()
    return new


def build_where(since: str, region: str | None, depts: list | None) -> str:
    w = [f'dateparution>="{since}"', 'familleavis in ("creation","vente")']
    if region:
        w.append(f"region_code={int(region)}")
    if depts:
        w.append("numerodepartement in (" + ",".join(f'"{d}"' for d in depts) + ")")
    return " and ".join(w)


# --- Exports ---------------------------------------------------------------
PUBLIC_COLS = ["date_parution", "type", "secteur", "denomination", "enseigne",
               "siren", "activite", "adresse", "cp", "ville", "departement",
               "date_debut_activite", "url"]


def export(rows: list, label: str):
    OUT.mkdir(exist_ok=True)
    by_sector = {}
    for r in rows:
        by_sector.setdefault(r["secteur"], []).append(r)

    with open(OUT / f"{label}_tout.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PUBLIC_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["secteur"], r["departement"])))

    for sector, items in by_sector.items():
        if sector == "autre":
            continue
        (OUT / f"{label}_{sector}.html").write_text(
            email_html(sector, items, label), encoding="utf-8")
    return by_sector


def email_html(sector: str, items: list, label: str) -> str:
    title = sector.replace("_", " ").capitalize()
    items = sorted(items, key=lambda r: (r["departement"], r["ville"]))
    rows = []
    for r in items:
        name = r["enseigne"] or r["denomination"] or "(entrepreneur individuel)"
        tag = "Création" if r["type"] == "creation" else "Reprise / cession"
        rows.append(
            "<tr><td><strong>{}</strong><br><span class=m>{} · SIREN {}</span></td>"
            "<td>{}</td><td>{} {}<br><span class=m>{}</span></td>"
            "<td><a href='{}'>Annonce</a></td></tr>".format(
                html.escape(name), tag, r["siren"],
                html.escape(r["activite"][:160]),
                r["cp"], html.escape(r["ville"]), html.escape(r["adresse"]),
                r["url"]))
    return f"""<!doctype html><meta charset=utf-8>
<title>Radar {title} — {label}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1b1b1b}}
h1{{font-size:20px;margin:0 0 4px}} .m{{color:#666;font-size:12px}}
table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid #e5e5e5;padding:8px 6px;vertical-align:top}}
.note{{background:#f6f6f2;padding:10px 12px;border-radius:6px;font-size:12px;color:#555;margin-top:18px}}
</style>
<h1>{len(items)} nouvelles entreprises — {title}</h1>
<div class=m>Période : {label} · Source : BODACC (données publiques, Licence Ouverte)</div>
<table>{''.join(rows)}</table>
<div class=note>Données issues de publications légales officielles. Vous pouvez vous désinscrire à tout moment.
Toute personne mentionnée peut s'opposer à l'utilisation de ses données à des fins de prospection.</div>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--region")
    ap.add_argument("--dept", nargs="*")
    a = ap.parse_args()
    since = a.since or (dt.date.today() - dt.timedelta(days=a.days)).isoformat()

    where = build_where(since, a.region, a.dept)
    print("BODACC where:", where)
    raw = fetch(where)
    rows = [normalize(r) for r in raw]
    new = store(rows)
    label = f"{since}_{a.region or '-'.join(a.dept or ['FR'])}"
    by_sector = export(rows, label)

    print(f"{len(rows)} annonces récupérées, {new} nouvelles en base")
    for s, items in sorted(by_sector.items(), key=lambda x: -len(x[1])):
        print(f"  {s:22s} {len(items):5d}")


if __name__ == "__main__":
    main()
