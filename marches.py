"""Radar Marchés attribués — produit autonome.

Chaque semaine, les marchés publics dont les données viennent d'être publiées
(DECP), filtrés pour chaque abonné (départements, types de marchés, montant
minimum) et enrichis avec le nom et la ville de l'entreprise titulaire et le
nom de l'acheteur (API Recherche d'entreprises, ouverte et sans clé).

Fonctions principales :
    collect_new(since_pub)        marchés publiés depuis une date (pas de doublons)
    enrich(rows)                  ajoute titulaire/acheteur (cache SQLite dans radar.db)
    for_subscriber(rows, s)       filtre selon les préférences de l'abonné
    email_html(rows, s, ...)      email hebdomadaire
    to_csv(rows)                  pièce jointe ouvrable dans Excel

Usage (aperçu local, sans envoi) :
    python marches.py --days 7 --dept 16 17 33 --types btp --min 40000
"""
import argparse
import csv
import datetime as dt
import html
import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import decp

ROOT = Path(__file__).parent
DB = ROOT / "radar.db"
OUT = ROOT / "out"
SITE = "https://radar-entreprises.fr"
PAGE = f"{SITE}/marches/"
API_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"

# Types proposés aux abonnés -> familles CPV (decp.CPV_FAMILIES)
TYPES = {
    "btp": ("Travaux et matériaux du BTP",
            {"Travaux BTP", "Matériaux de construction"}),
    "ingenierie": ("Ingénierie, architecture, études",
                   {"Ingénierie / architecture"}),
    "entretien": ("Entretien, maintenance, nettoyage, espaces verts",
                  {"Maintenance / réparation", "Nettoyage / déchets / environnement",
                   "Espaces verts / agriculture"}),
    "fournitures": ("Fournitures et équipements",
                    {"Denrées alimentaires", "Véhicules / transport (fournitures)", "Matériel médical",
                     "Mobilier / équipements", "Énergie", "Matériel électrique / télécom",
                     "Instruments / laboratoire", "Machines et équipements", "Vêtements / EPI",
                     "Imprimés / livres"}),
    "services": ("Services, informatique, formation", None),  # tout le reste
}
MONTANTS = [0, 40000, 100000, 500000]

EFFECTIFS = {"00": "0 salarié", "01": "1-2 salariés", "02": "3-5 salariés", "03": "6-9 salariés",
             "11": "10-19 salariés", "12": "20-49 salariés", "21": "50-99 salariés",
             "22": "100-199 salariés", "31": "200-249 salariés", "32": "250-499 salariés",
             "41": "500-999 salariés", "42": "1 000-1 999 salariés", "51": "2 000-4 999 salariés",
             "52": "5 000-9 999 salariés", "53": "10 000 salariés et plus"}

DEPT_NOMS = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence", "05": "Hautes-Alpes",
    "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes", "09": "Ariège", "10": "Aube", "11": "Aude",
    "12": "Aveyron", "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze", "2A": "Corse-du-Sud", "2B": "Haute-Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne", "25": "Doubs", "26": "Drôme",
    "27": "Eure", "28": "Eure-et-Loir", "29": "Finistère", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "33": "Gironde", "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre", "37": "Indre-et-Loire",
    "38": "Isère", "39": "Jura", "40": "Landes", "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire",
    "44": "Loire-Atlantique", "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère",
    "49": "Maine-et-Loire", "50": "Manche", "51": "Marne", "52": "Haute-Marne", "53": "Mayenne",
    "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan", "57": "Moselle", "58": "Nièvre", "59": "Nord",
    "60": "Oise", "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin", "69": "Rhône",
    "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe", "73": "Savoie", "74": "Haute-Savoie",
    "75": "Paris", "76": "Seine-Maritime", "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres",
    "80": "Somme", "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse", "85": "Vendée",
    "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges", "89": "Yonne", "90": "Territoire de Belfort",
    "91": "Essonne", "92": "Hauts-de-Seine", "93": "Seine-Saint-Denis", "94": "Val-de-Marne",
    "95": "Val-d'Oise", "971": "Guadeloupe", "972": "Martinique", "973": "Guyane", "974": "La Réunion",
    "976": "Mayotte",
}

VIDE = (None, "", "CDL")


def type_of(famille: str) -> str:
    for key, (_, fams) in TYPES.items():
        if fams and famille in fams:
            return key
    return "services"


# --- Collecte ---------------------------------------------------------------
def from_record(r: dict) -> dict:
    siret = str(r.get("titulaire_id_1") or "").strip()
    siret = siret.zfill(14) if siret.isdigit() else siret
    cotit = [str(r.get(f"titulaire_id_{i}")) for i in (2, 3) if r.get(f"titulaire_id_{i}") not in VIDE]
    montant = r.get("montant")
    return {
        "id": str(r.get("id") or ""),
        "date_notification": (r.get("datenotification") or "")[:10],
        "date_publication": (r.get("datepublicationdonnees") or "")[:10],
        "famille": decp.cpv_family(r.get("codecpv")),
        "type": type_of(decp.cpv_family(r.get("codecpv"))),
        "cpv": r.get("codecpv") or "",
        "objet": (r.get("objet") or "").strip(),
        "montant": float(montant) if isinstance(montant, (int, float)) and montant > 1 else None,
        "duree_mois": r.get("dureemois"),
        "procedure": r.get("procedure") or "",
        "offres_recues": r.get("offresrecues") if r.get("offresrecues") not in VIDE else "",
        "sous_traitance": (r.get("soustraitancedeclaree") or "").lower() == "oui",
        "groupement": len(cotit) > 0,
        "titulaire_siret": siret,
        # Certaines sources (DGFIP) recopient le SIRET du titulaire à la place de l'acheteur.
        "acheteur_siret": "" if str(r.get("acheteur_id") or "") == siret else str(r.get("acheteur_id") or ""),
        "departements": sorted(decp.depts_of(r)),
    }


def collect_new(since_pub: str, max_age_days: int = 365, records: list | None = None) -> list:
    """Marchés dont les données ont été publiées après `since_pub` (exclu).

    On travaille sur la date de publication et non de notification : les acheteurs
    publient souvent avec plusieurs semaines de retard, et ces marchés-là seraient
    sinon perdus. Les marchés notifiés il y a plus d'un an et les avenants sont ignorés.
    """
    raw = records if records is not None else decp.fetch(f'datepublicationdonnees>"{since_pub}"')
    oldest = (dt.date.fromisoformat(since_pub) - dt.timedelta(days=max_age_days)).isoformat()
    seen, rows = set(), []
    for r in raw:
        if r.get("idmodification") not in VIDE:
            continue
        key = (r.get("id"), r.get("acheteur_id"))
        if key in seen:
            continue
        seen.add(key)
        m = from_record(r)
        if m["date_notification"] and m["date_notification"] < oldest:
            continue
        rows.append(m)
    return rows


# --- Enrichissement (titulaire, acheteur) -----------------------------------
def _cache(con):
    con.execute("""CREATE TABLE IF NOT EXISTS entreprises(
        siret TEXT PRIMARY KEY, nom TEXT, ville TEXT, cp TEXT, naf TEXT, effectif TEXT,
        trouve INTEGER, maj TEXT)""")
    return con


PETITS = {"de", "du", "des", "la", "le", "les", "et", "sur", "sous", "en", "aux", "au", "d", "l"}


def titre(nom: str) -> str:
    """COMMUNE DE GUILLOS -> Commune de Guillos ; SAINT-YRIEIX-SUR-CHARENTE -> Saint-Yrieix-sur-Charente."""
    out = re.sub(r"[A-Za-zÀ-ÿ]+", lambda m: m.group(0).lower() if m.group(0).lower() in PETITS
                 else m.group(0).capitalize(), nom.lower())
    out = re.sub(r"(?<=[ '’-])(D|L)(?=['’])", lambda m: m.group(1).lower(), out)
    return out[:1].upper() + out[1:]


def nom_propre(nom: str) -> str:
    """« CIMALTO (CIMALTO) » -> « CIMALTO »."""
    m = re.fullmatch(r"(.+?)\s*\((.+)\)", nom.strip())
    if m and m.group(1).strip().upper() == m.group(2).strip().upper():
        return m.group(1).strip()
    return nom.strip()


def lookup(siret: str) -> dict | None:
    """Fiche d'un établissement via l'API Recherche d'entreprises (None si erreur réseau)."""
    url = API_ENTREPRISES + "?" + urllib.parse.urlencode({"q": siret, "per_page": 1})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    res = (data.get("results") or [None])[0]
    if not res:
        return {"trouve": 0}
    etab = next((e for e in res.get("matching_etablissements") or [] if e.get("siret") == siret),
                None) or res.get("siege") or {}
    nom = res.get("nom_complet") or res.get("nom_raison_sociale") or ""
    if "NON-DIFFUSIBLE" in nom.upper():
        nom = ""
    return {"trouve": 1, "nom": nom_propre(nom), "ville": titre(etab.get("libelle_commune") or ""),
            "cp": etab.get("code_postal") or "",
            "naf": etab.get("activite_principale") or res.get("activite_principale") or "",
            "effectif": EFFECTIFS.get(str(res.get("tranche_effectif_salarie") or ""), "")}


def enrich(rows: list, db: Path | None = None, max_calls: int = 1500, pause: float = 0.16) -> int:
    """Complète titulaire_* et acheteur_nom. Cache permanent : un SIRET n'est cherché qu'une fois
    (ou de nouveau après 180 jours). Renvoie le nombre d'appels faits."""
    con = _cache(sqlite3.connect(db or DB))
    known = {r[0]: r for r in con.execute("SELECT siret,nom,ville,cp,naf,effectif,trouve,maj FROM entreprises")}
    stale = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    wanted = []
    for r in rows:
        for s in (r["titulaire_siret"], r["acheteur_siret"]):
            if len(s) == 14 and s.isdigit() and s not in wanted and (s not in known or known[s][7] < stale):
                wanted.append(s)
    calls = 0
    for s in wanted[:max_calls]:
        f = lookup(s)
        calls += 1
        if f is None:
            continue  # erreur réseau : on retentera la semaine prochaine
        rec = (s, f.get("nom", ""), f.get("ville", ""), f.get("cp", ""), f.get("naf", ""),
               f.get("effectif", ""), f["trouve"], dt.date.today().isoformat())
        con.execute("INSERT OR REPLACE INTO entreprises VALUES (?,?,?,?,?,?,?,?)", rec)
        known[s] = rec
        time.sleep(pause)
    con.commit()
    con.close()
    for r in rows:
        t = known.get(r["titulaire_siret"])
        a = known.get(r["acheteur_siret"])
        r["titulaire_nom"] = nom_propre((t[1] if t else "") or "")
        r["titulaire_ville"] = titre((t[2] if t else "") or "")
        r["titulaire_cp"] = (t[3] if t else "") or ""
        r["titulaire_naf"] = (t[4] if t else "") or ""
        r["titulaire_effectif"] = (t[5] if t else "") or ""
        r["acheteur_nom"] = titre(nom_propre((a[1] if a else "") or ""))
    return calls


# --- Filtre par abonné -------------------------------------------------------
def prefs(s: dict) -> tuple:
    types = [t for t in s.get("types") or [] if t in TYPES]
    return set(s.get("departements") or []), set(types), int(s.get("montant_min") or 0)


def for_subscriber(rows: list, s: dict) -> list:
    depts, types, mini = prefs(s)
    out = []
    for r in rows:
        if not set(r["departements"]) & depts:
            continue
        if types and r["type"] not in types:
            continue
        if mini and (r["montant"] or 0) < mini:
            continue
        out.append(r)
    return out


def needed(rows: list, subs: list) -> list:
    """Marchés qui intéressent au moins un abonné (pour limiter l'enrichissement)."""
    keep = {id(r) for s in subs for r in for_subscriber(rows, s)}
    return [r for r in rows if id(r) in keep]


# --- Mise en forme ----------------------------------------------------------
def euros(x: float | None) -> str:
    if not x:
        return "Montant non précisé"
    return f"{x:,.0f} €".replace(",", " ")


def propre(objet: str, n: int = 220) -> str:
    """Objet lisible : sans code interne en tête, sans majuscules continues, coupé proprement."""
    o = objet.replace("\\n", " ").replace("¿", "'")
    o = re.sub(r"^(INX\s+)?[A-Z0-9_/.-]{4,}\s*[-–:]?\s+(?=[A-Za-zÀ-ÿ])", "", o.strip())
    o = re.sub(r"\s+", " ", o).strip(' ."«»')
    lettres = [c for c in o if c.isalpha()]
    if lettres and sum(c.isupper() for c in lettres) / len(lettres) > .6:
        o = o.lower()
    o = o[:1].upper() + o[1:]
    if len(o) > n:
        o = o[:n].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return o


def titulaire_label(r: dict) -> str:
    return r.get("titulaire_nom") or (f"SIRET {r['titulaire_siret']}" if r["titulaire_siret"] else "Titulaire non publié")


def fiche(siret: str) -> str:
    return f"https://annuaire-entreprises.data.gouv.fr/etablissement/{siret}"


def zone_label(depts: list) -> str:
    noms = [DEPT_NOMS.get(d, d) for d in depts]
    return noms[0] if len(noms) == 1 else ", ".join(noms[:-1]) + " et " + noms[-1]


def subject(rows: list, s: dict) -> str:
    depts, types, _ = prefs(s)
    n = len(rows)
    total = sum(r["montant"] or 0 for r in rows)
    t = f" · {euros(total)}" if total else ""
    z = zone_label(sorted(depts)) if len(depts) <= 2 else f"{len(depts)} départements"
    return f"{n} marché{'s' if n > 1 else ''} attribué{'s' if n > 1 else ''} cette semaine — {z}{t}"


INK, MUTED, RULE, FLUO, PAPER = "#1D2125", "#5E656C", "#E3E1DA", "#FFE34A", "#F6F4EE"


def _card(r: dict, dep_principal: str) -> str:
    lieu = r.get("titulaire_ville") or ""
    if lieu and r.get("titulaire_cp"):
        lieu += f" ({r['titulaire_cp'][:2]})"
    sub = " · ".join(x for x in (lieu, r.get("titulaire_effectif")) if x)
    badges = []
    if r["sous_traitance"]:
        badges.append("Sous-traitance déclarée")
    if r["groupement"]:
        badges.append("Groupement d'entreprises")
    badge_html = "".join(
        f"<span style='display:inline-block;background:{FLUO};color:{INK};font-size:12px;font-weight:700;"
        f"padding:2px 8px;border-radius:3px;margin:6px 6px 0 0'>{b}</span>" for b in badges)
    details = [f"Notifié le {dt.date.fromisoformat(r['date_notification']):%d/%m/%Y}" if r["date_notification"] else ""]
    if r["duree_mois"]:
        details.append(f"{r['duree_mois']} mois")
    if r["offres_recues"] and str(r["offres_recues"]).isdigit():
        n = int(r["offres_recues"])
        details.append(f"{n} offre{'s' if n > 1 else ''} reçue{'s' if n > 1 else ''}")
    acheteur = html.escape(r.get("acheteur_nom") or "")
    lien = (f"<a href='{fiche(r['titulaire_siret'])}' style='color:{INK};font-weight:700'>Fiche de l'entreprise</a>"
            if r["titulaire_siret"].isdigit() else "")
    return f"""<tr><td style="padding:16px 0;border-top:1px solid {RULE}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td style="vertical-align:top;padding-right:12px">
<div style="font-size:16px;font-weight:700;color:{INK};line-height:1.3">{html.escape(titulaire_label(r))}</div>
<div style="font-size:13px;color:{MUTED};margin-top:2px">{html.escape(sub)}</div>
</td>
<td style="vertical-align:top;text-align:right;white-space:nowrap;font-size:16px;font-weight:800;color:{INK}">{euros(r['montant'])}</td>
</tr></table>
<div style="font-size:14px;color:{INK};line-height:1.45;margin-top:8px">{html.escape(propre(r['objet']))}</div>
<div style="font-size:13px;color:{MUTED};margin-top:6px">{'Acheteur : ' + acheteur + ' · ' if acheteur else ''}{' · '.join(d for d in details if d)}</div>
{badge_html}
<div style="font-size:13px;margin-top:8px">{lien}</div>
</td></tr>"""


def email_html(rows: list, s: dict, footer: str = "", max_cards: int = 60) -> str:
    depts, types, mini = prefs(s)
    rows = sorted(rows, key=lambda r: -(r["montant"] or 0))
    by_dep: dict = {}
    for r in rows:
        d = next((x for x in sorted(depts) if x in r["departements"]), r["departements"][0] if r["departements"] else "")
        by_dep.setdefault(d, []).append(r)
    total = sum(r["montant"] or 0 for r in rows)
    st = sum(1 for r in rows if r["sous_traitance"])
    filtre = ", ".join(TYPES[t][0].lower() for t in sorted(types)) if types else "tous types de marchés"
    if mini:
        filtre += f", à partir de {euros(mini)}"
    parts, shown = [], 0
    for d in sorted(by_dep, key=lambda k: -len(by_dep[k])):
        items = by_dep[d]
        cards = []
        for r in items:
            if shown >= max_cards:
                break
            cards.append(_card(r, d))
            shown += 1
        if not cards:
            continue
        parts.append(
            f"<tr><td style='padding:26px 0 4px;font-size:13px;font-weight:800;letter-spacing:.06em;"
            f"text-transform:uppercase;color:{INK};border-bottom:2px solid {INK}'>"
            f"{html.escape(DEPT_NOMS.get(d, d))} <span style='color:{MUTED};font-weight:400'>· {len(items)}</span></td></tr>"
            + "".join(cards))
    reste = len(rows) - shown
    plus = (f"<p style='font-size:14px;color:{MUTED};margin:18px 0 0'>Et {reste} autres marchés dans le fichier joint.</p>"
            if reste > 0 else "")
    resume = (f"<b>{len(rows)}</b> marché{'s' if len(rows) > 1 else ''}"
              + (f" pour <b>{euros(total)}</b>" if total else "")
              + (f", dont <b>{st}</b> avec sous-traitance déclarée" if st else "") + ".")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar Marchés attribués</title></head>
<body style="margin:0;background:{PAPER};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAPER}"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#fff;border:1px solid {RULE};border-radius:8px">
<tr><td style="padding:24px 24px 8px">
<div style="font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:{MUTED}">Radar Marchés attribués</div>
<h1 style="font-size:22px;line-height:1.25;margin:8px 0 0;color:{INK}">Les marchés publics remportés cette semaine dans {html.escape(zone_label(sorted(depts)))}</h1>
<p style="font-size:15px;color:{INK};margin:12px 0 0;line-height:1.5">{resume}</p>
<p style="font-size:13px;color:{MUTED};margin:4px 0 0">Votre sélection : {html.escape(filtre)}. Le fichier Excel complet est joint.</p>
</td></tr>
<tr><td style="padding:0 24px 8px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{''.join(parts)}</table>{plus}</td></tr>
<tr><td style="padding:16px 24px 24px;font-size:12px;line-height:1.5;color:{MUTED};border-top:1px solid {RULE}">
{footer}Source : données essentielles de la commande publique (DECP, data.economie.gouv.fr) et Annuaire des entreprises,
sous Licence Ouverte 2.0. Montants HT tels que déclarés par l'acheteur. Toute personne mentionnée peut s'opposer à
l'utilisation de ses données à des fins de prospection : contact@radar-entreprises.fr.<br>
<a href="{PAGE}" style="color:{MUTED}">Radar Marchés attribués</a> · une offre Radar Entreprises
</td></tr></table></td></tr></table></body></html>"""


CSV_COLS = ["date_notification", "date_publication", "departements", "famille", "objet", "montant",
            "duree_mois", "procedure", "offres_recues", "sous_traitance", "titulaire_nom",
            "titulaire_siret", "titulaire_ville", "titulaire_cp", "titulaire_naf", "titulaire_effectif",
            "titulaire_fiche", "acheteur_nom", "acheteur_siret", "cpv"]


def to_csv(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore", delimiter=";")
    w.writeheader()
    for r in sorted(rows, key=lambda r: -(r["montant"] or 0)):
        w.writerow(dict(r, departements=" ".join(r["departements"]),
                        montant=f"{r['montant']:.2f}".replace(".", ",") if r["montant"] else "",
                        sous_traitance="oui" if r["sous_traitance"] else "non",
                        titulaire_fiche=fiche(r["titulaire_siret"]) if r["titulaire_siret"].isdigit() else ""))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dept", nargs="+", default=["16", "17", "33"])
    ap.add_argument("--types", nargs="*", default=[])
    ap.add_argument("--min", type=int, default=0)
    ap.add_argument("--no-enrich", action="store_true")
    a = ap.parse_args()
    since = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    s = {"departements": a.dept, "types": a.types, "montant_min": a.min}
    rows = collect_new(since)
    mine = for_subscriber(rows, s)
    if not a.no_enrich:
        print(f"{enrich(mine)} recherches d'entreprises", file=sys.stderr)
    OUT.mkdir(exist_ok=True)
    (OUT / "apercu_marches.html").write_text(email_html(mine, s), encoding="utf-8")
    (OUT / "apercu_marches.csv").write_bytes(to_csv(mine))
    print(f"{len(rows)} marchés publiés depuis le {since}, {len(mine)} pour cette sélection")
    print(subject(mine, s))


if __name__ == "__main__":
    main()
