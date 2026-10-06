"""Radar marchés attribués — DECP (données essentielles de la commande publique).

Récupère les marchés publics notifiés récemment sur une zone, les classe par
famille CPV et produit CSV + HTML. Les DECP n'identifient le titulaire que par
SIRET : on fournit un lien vers l'Annuaire des entreprises (nom, dirigeants).
L'enrichissement nom/NAF via l'API SIRENE se fera dans le runner de production.

Usage :
    python decp.py --since 2026-09-01 --region 75
"""
import argparse
import csv
import datetime as dt
import html
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = ("https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
       "decp-2022-marches-valides/exports/json")
OUT = Path(__file__).parent / "out"

REGIONS = {  # code région INSEE -> départements
    "11": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "24": ["18", "28", "36", "37", "41", "45"],
    "27": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "28": ["14", "27", "50", "61", "76"],
    "32": ["02", "59", "60", "62", "80"],
    "44": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "52": ["44", "49", "53", "72", "85"],
    "53": ["22", "29", "35", "56"],
    "75": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "76": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "84": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "93": ["04", "05", "06", "13", "83", "84"],
    "94": ["2A", "2B"],
    "01": ["971"], "02": ["972"], "03": ["973"], "04": ["974"], "06": ["976"],
}

CPV_FAMILIES = [  # préfixe CPV -> famille lisible
    ("45", "Travaux BTP"),
    ("71", "Ingénierie / architecture"),
    ("77", "Espaces verts / agriculture"),
    ("90", "Nettoyage / déchets / environnement"),
    ("55", "Restauration / hôtellerie"),
    ("15", "Denrées alimentaires"),
    ("79", "Services aux entreprises"),
    ("72", "Informatique"),
    ("50", "Maintenance / réparation"),
    ("34", "Véhicules / transport (fournitures)"),
    ("60", "Transport (services)"),
    ("80", "Formation / enseignement"),
    ("85", "Santé / social"),
    ("33", "Matériel médical"),
    ("39", "Mobilier / équipements"),
    ("44", "Matériaux de construction"),
    ("09", "Énergie"),
    ("65", "Énergie"),
    ("51", "Maintenance / réparation"),
    ("30", "Informatique"),
    ("48", "Informatique"),
    ("31", "Matériel électrique / télécom"),
    ("32", "Matériel électrique / télécom"),
    ("38", "Instruments / laboratoire"),
    ("42", "Machines et équipements"),
    ("43", "Machines et équipements"),
    ("16", "Machines et équipements"),
    ("18", "Vêtements / EPI"),
    ("22", "Imprimés / livres"),
    ("03", "Denrées alimentaires"),
    ("66", "Assurance / banque"),
    ("92", "Culture / communication"),
    ("63", "Transport (services)"),
    ("64", "Services aux entreprises"),
    ("98", "Services aux entreprises"),
]


def cpv_family(cpv: str) -> str:
    for prefix, label in CPV_FAMILIES:
        if (cpv or "").startswith(prefix):
            return label
    return "Autres"


def fetch(where: str) -> list:
    url = API + "?" + urllib.parse.urlencode({"where": where})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  retry ({e})", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return []


def in_zone(rec: dict, region: str, depts: list) -> bool:
    if region == "all":
        return True
    code = str(rec.get("lieuexecution_code") or "")
    typ = (rec.get("lieuexecution_typecode") or "").lower()
    if "région" in typ or "region" in typ:
        return code == region
    if "postal" in typ or "commune" in typ:
        return code[:2] in depts
    return code[:2] in depts  # code département ou autre


def depts_of(rec: dict) -> set:
    """Départements concernés par un marché, d'après son lieu d'exécution."""
    code = str(rec.get("lieuexecution_code") or "").strip()
    typ = (rec.get("lieuexecution_typecode") or "").lower()
    if not code:
        return set()
    if "région" in typ or "region" in typ:
        return set(REGIONS.get(code.zfill(2), []))
    if code[:2] == "97":
        return {code[:3]}
    if code[:2] == "20" and ("postal" in typ or "commune" in typ):
        return {"2A", "2B"}
    return {code[:2]}


def collect(since: str) -> list:
    """Marchés notifiés depuis `since`, dédoublonnés, avec leurs départements."""
    seen, rows = set(), []
    for r in fetch(f'datenotification>="{since}"'):
        key = (r.get("id"), r.get("acheteur_id"))
        if key in seen:  # les modifications dupliquent les lignes
            continue
        seen.add(key)
        siret = str(r.get("titulaire_id_1") or "").zfill(14)
        rows.append({
            "date_notification": r.get("datenotification"),
            "famille": cpv_family(r.get("codecpv")),
            "objet": (r.get("objet") or "").strip(),
            "montant": r.get("montant"),
            "titulaire_fiche": f"https://annuaire-entreprises.data.gouv.fr/etablissement/{siret}",
            "departements": sorted(depts_of(r)),
        })
    return rows


def email_marches(rows: list, depts: list, since: str) -> str:
    """Email hebdomadaire « marchés attribués » pour un abonné."""
    fams = {}
    for r in sorted(rows, key=lambda x: -(x["montant"] or 0)):
        fams.setdefault(r["famille"], []).append(r)
    return render(fams, since).replace(
        "<h1>Marchés publics attribués depuis le", f"<h1>Marchés attribués ({', '.join(depts)}) depuis le")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--region", default="75")
    a = ap.parse_args()
    since = a.since or (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    depts = REGIONS.get(a.region, [])

    raw = fetch(f'datenotification>="{since}"')
    seen, rows = set(), []
    for r in raw:
        if not in_zone(r, a.region, depts):
            continue
        key = (r.get("id"), r.get("acheteur_id"))
        if key in seen:  # les modifications dupliquent les lignes
            continue
        seen.add(key)
        siret = str(r.get("titulaire_id_1") or "").zfill(14)
        rows.append({
            "date_notification": r.get("datenotification"),
            "famille": cpv_family(r.get("codecpv")),
            "objet": (r.get("objet") or "").strip(),
            "montant": r.get("montant"),
            "duree_mois": r.get("dureemois"),
            "procedure": r.get("procedure"),
            "lieu": f'{r.get("lieuexecution_code")} ({r.get("lieuexecution_typecode")})',
            "titulaire_siret": siret,
            "titulaire_fiche": f"https://annuaire-entreprises.data.gouv.fr/etablissement/{siret}",
            "acheteur_siret": r.get("acheteur_id"),
            "cpv": r.get("codecpv"),
        })

    rows.sort(key=lambda x: (x["famille"], -(x["montant"] or 0)))
    OUT.mkdir(exist_ok=True)
    label = f"decp_{since}_{a.region}"
    with open(OUT / f"{label}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["vide"])
        w.writeheader()
        w.writerows(rows)

    fams = {}
    for r in rows:
        fams.setdefault(r["famille"], []).append(r)
    (OUT / f"{label}.html").write_text(render(fams, since), encoding="utf-8")

    print(f"{len(raw)} marchés nationaux depuis {since}, {len(rows)} en zone {a.region}")
    for fam, items in sorted(fams.items(), key=lambda x: -len(x[1])):
        total = sum(i["montant"] or 0 for i in items)
        print(f"  {fam:38s} {len(items):4d}  {total/1e6:7.1f} M€")


def render(fams: dict, since: str) -> str:
    parts = []
    for fam, items in sorted(fams.items(), key=lambda x: -len(x[1])):
        trs = "".join(
            "<tr><td>{}</td><td>{}</td><td class=r>{:,.0f} €</td>"
            "<td><a href='{}'>Titulaire</a></td></tr>".format(
                i["date_notification"], html.escape(i["objet"][:180]),
                i["montant"] or 0, i["titulaire_fiche"]).replace(",", " ")
            for i in items[:40])
        parts.append(f"<h2>{html.escape(fam)} <span class=m>({len(items)})</span></h2><table>{trs}</table>")
    return f"""<!doctype html><meta charset=utf-8><title>Marchés attribués</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px}}
h2{{font-size:16px;margin:22px 0 6px}} .m{{color:#777;font-weight:400}}
table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid #e6e6e6;padding:6px;vertical-align:top}}
.r{{text-align:right;white-space:nowrap}}</style>
<h1>Marchés publics attribués depuis le {since}</h1>
<p class=m>Source : DECP, data.economie.gouv.fr (Licence Ouverte 2.0)</p>
{''.join(parts)}"""


if __name__ == "__main__":
    main()
