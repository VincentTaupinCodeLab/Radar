"""Pages de référencement : les nouvelles entreprises et les marchés attribués, par département.

Appelé à la fin de daily.yml (et par site.yml) juste avant la mise en ligne. Écrit dans site/ :

    /departements/                              index national
    /departements/<dept>/                       créations et reprises des 7 derniers jours
    /departements/<dept>/<secteur>/             30 derniers jours, si au moins 3 entrées
    /marches/departements/                      index national des marchés attribués
    /marches/departements/<dept>/               10 marchés récents du département
    /marches/acheteurs/<acheteur>/              acheteur public ayant au moins 3 marchés sur 12 mois
    /sitemap.xml

Règles de publication :
- seules les personnes morales apparaissent : annonces BODACC « pp » écartées, titulaires de marchés
  affichés seulement si l'Annuaire des entreprises confirme une catégorie juridique de personne
  morale (les entrepreneurs individuels, catégorie 1xxx, et les fiches non diffusibles sont écartés) ;
- aucune page vide : une page n'est écrite que si elle a au moins une entrée à montrer
  (et au moins 3 entrées sur 30 jours pour les pages département × secteur) ;
- les SIREN listés dans state/opposition.txt (droit d'opposition) ne sont jamais affichés.

Les marchés (DECP) sont gardés 13 mois dans radar.db (table decp_marches), mis à jour chaque jour
depuis la dernière date de publication connue. Les noms des titulaires et des acheteurs viennent du
cache « entreprises » de marches.py, complété ici dans une limite de temps par passage.

Usage : python pages.py [--today AAAA-MM-JJ] [--budget-enrichissement 480] [--sans-reseau]
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import html
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import marches as M
import pipeline as P
from site_stats import court, jour

ROOT = Path(__file__).parent
SITE_DIR = ROOT / "site"
SITE = "https://radar-entreprises.fr"
OPPOSITION = ROOT / "state" / "opposition.txt"
GENERES = ["departements", "marches/departements", "marches/acheteurs"]  # dossiers recréés à chaque passage
PAGES_FIXES = ["/", "/marches/", "/mentions-legales.html", "/cgv.html"]

JOURS_SEMAINE = 7
JOURS_SECTEUR = 30
MIN_SECTEUR = 3
EXEMPLES_SECTEUR = 15
MARCHES_DEPT = 10
MIN_ACHETEUR = 3
MOIS_MARCHES = 12

DECP_EXPORT = ("https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
               "decp-2022-marches-valides/exports/json")
DECP_PAGE = "https://data.economie.gouv.fr/explore/dataset/decp-2022-marches-valides/"
BODACC_PAGE = "https://www.bodacc.fr/"
BODACC_DONNEES = "https://bodacc-datadila.opendatasoft.com/explore/dataset/annonces-commerciales/"
ANNUAIRE = "https://annuaire-entreprises.data.gouv.fr/"

NOMS = dict(M.DEPT_NOMS, **{"975": "Saint-Pierre-et-Miquelon"})
DOM = ["971", "972", "973", "974", "975", "976"]

# « en Gironde », « dans le Nord », « à Paris » : la préposition fait partie du titre.
DANS = {
    "01": "dans l'Ain", "02": "dans l'Aisne", "03": "dans l'Allier", "04": "dans les Alpes-de-Haute-Provence",
    "05": "dans les Hautes-Alpes", "06": "dans les Alpes-Maritimes", "07": "en Ardèche", "08": "dans les Ardennes",
    "09": "en Ariège", "10": "dans l'Aube", "11": "dans l'Aude", "12": "en Aveyron", "13": "dans les Bouches-du-Rhône",
    "14": "dans le Calvados", "15": "dans le Cantal", "16": "en Charente", "17": "en Charente-Maritime",
    "18": "dans le Cher", "19": "en Corrèze", "2A": "en Corse-du-Sud", "2B": "en Haute-Corse", "21": "en Côte-d'Or",
    "22": "dans les Côtes-d'Armor", "23": "dans la Creuse", "24": "en Dordogne", "25": "dans le Doubs",
    "26": "dans la Drôme", "27": "dans l'Eure", "28": "en Eure-et-Loir", "29": "dans le Finistère", "30": "dans le Gard",
    "31": "en Haute-Garonne", "32": "dans le Gers", "33": "en Gironde", "34": "dans l'Hérault",
    "35": "en Ille-et-Vilaine", "36": "dans l'Indre", "37": "en Indre-et-Loire", "38": "en Isère", "39": "dans le Jura",
    "40": "dans les Landes", "41": "en Loir-et-Cher", "42": "dans la Loire", "43": "en Haute-Loire",
    "44": "en Loire-Atlantique", "45": "dans le Loiret", "46": "dans le Lot", "47": "en Lot-et-Garonne",
    "48": "en Lozère", "49": "en Maine-et-Loire", "50": "dans la Manche", "51": "dans la Marne", "52": "en Haute-Marne",
    "53": "en Mayenne", "54": "en Meurthe-et-Moselle", "55": "dans la Meuse", "56": "dans le Morbihan",
    "57": "en Moselle", "58": "dans la Nièvre", "59": "dans le Nord", "60": "dans l'Oise", "61": "dans l'Orne",
    "62": "dans le Pas-de-Calais", "63": "dans le Puy-de-Dôme", "64": "dans les Pyrénées-Atlantiques",
    "65": "dans les Hautes-Pyrénées", "66": "dans les Pyrénées-Orientales", "67": "dans le Bas-Rhin",
    "68": "dans le Haut-Rhin", "69": "dans le Rhône", "70": "en Haute-Saône", "71": "en Saône-et-Loire",
    "72": "dans la Sarthe", "73": "en Savoie", "74": "en Haute-Savoie", "75": "à Paris", "76": "en Seine-Maritime",
    "77": "en Seine-et-Marne", "78": "dans les Yvelines", "79": "dans les Deux-Sèvres", "80": "dans la Somme",
    "81": "dans le Tarn", "82": "dans le Tarn-et-Garonne", "83": "dans le Var", "84": "dans le Vaucluse",
    "85": "en Vendée", "86": "dans la Vienne", "87": "en Haute-Vienne", "88": "dans les Vosges", "89": "dans l'Yonne",
    "90": "dans le Territoire de Belfort", "91": "dans l'Essonne", "92": "dans les Hauts-de-Seine",
    "93": "en Seine-Saint-Denis", "94": "dans le Val-de-Marne", "95": "dans le Val-d'Oise", "971": "en Guadeloupe",
    "972": "en Martinique", "973": "en Guyane", "974": "à La Réunion", "975": "à Saint-Pierre-et-Miquelon",
    "976": "à Mayotte",
}

# Départements limitrophes (contours IGN, Corse comprise ; l'outre-mer renvoie aux autres DROM).
VOISINS = {
    "01": ["38", "39", "69", "71", "73", "74"], "02": ["08", "51", "59", "60", "77", "80"],
    "03": ["18", "23", "42", "58", "63", "71"], "04": ["05", "06", "26", "83", "84"],
    "05": ["04", "26", "38", "73"], "06": ["04", "83"], "07": ["26", "30", "38", "42", "43", "48", "84"],
    "08": ["02", "51", "55"], "09": ["11", "31", "66"], "10": ["21", "51", "52", "77", "89"],
    "11": ["09", "31", "34", "66", "81"], "12": ["15", "30", "34", "46", "48", "81", "82"],
    "13": ["30", "83", "84"], "14": ["27", "50", "61"], "15": ["12", "19", "43", "46", "48", "63"],
    "16": ["17", "24", "79", "86", "87"], "17": ["16", "24", "33", "79", "85"],
    "18": ["03", "23", "36", "41", "45", "58"], "19": ["15", "23", "24", "46", "63", "87"],
    "2A": ["2B"], "2B": ["2A"], "21": ["10", "39", "52", "58", "70", "71", "89"], "22": ["29", "35", "56"],
    "23": ["03", "18", "19", "36", "63", "87"], "24": ["16", "17", "19", "33", "46", "47", "87"],
    "25": ["39", "70", "90"], "26": ["04", "05", "07", "38", "84"], "27": ["14", "28", "60", "61", "76", "78", "95"],
    "28": ["27", "41", "45", "61", "72", "78", "91"], "29": ["22", "56"],
    "30": ["07", "12", "13", "34", "48", "84"], "31": ["09", "11", "32", "65", "81", "82"],
    "32": ["31", "40", "47", "64", "65", "82"], "33": ["17", "24", "40", "47"], "34": ["11", "12", "30", "81"],
    "35": ["22", "44", "49", "50", "53", "56"], "36": ["18", "23", "37", "41", "86", "87"],
    "37": ["36", "41", "49", "72", "86"], "38": ["01", "05", "07", "26", "42", "69", "73"],
    "39": ["01", "21", "25", "70", "71"], "40": ["32", "33", "47", "64"], "41": ["18", "28", "36", "37", "45", "72"],
    "42": ["03", "07", "38", "43", "63", "69", "71"], "43": ["07", "15", "42", "48", "63"],
    "44": ["35", "49", "56", "85"], "45": ["18", "28", "41", "58", "77", "89", "91"],
    "46": ["12", "15", "19", "24", "47", "82"], "47": ["24", "32", "33", "40", "46", "82"],
    "48": ["07", "12", "15", "30", "43"], "49": ["35", "37", "44", "53", "72", "79", "85", "86"],
    "50": ["14", "35", "53", "61"], "51": ["02", "08", "10", "52", "55", "77"],
    "52": ["10", "21", "51", "55", "70", "88"], "53": ["35", "49", "50", "61", "72"],
    "54": ["55", "57", "67", "88"], "55": ["08", "51", "52", "54", "88"], "56": ["22", "29", "35", "44"],
    "57": ["54", "67"], "58": ["03", "18", "21", "45", "71", "89"], "59": ["02", "62", "80"],
    "60": ["02", "27", "76", "77", "80", "95"], "61": ["14", "27", "28", "50", "53", "72"],
    "62": ["59", "80"], "63": ["03", "15", "19", "23", "42", "43"], "64": ["32", "40", "65"],
    "65": ["31", "32", "64"], "66": ["09", "11"], "67": ["54", "57", "68", "88"], "68": ["67", "88", "90"],
    "69": ["01", "38", "42", "71"], "70": ["21", "25", "39", "52", "88", "90"],
    "71": ["01", "03", "21", "39", "42", "58", "69"], "72": ["28", "37", "41", "49", "53", "61"],
    "73": ["01", "05", "38", "74"], "74": ["01", "73"], "75": ["92", "93", "94"],
    "76": ["27", "60", "80"], "77": ["02", "10", "45", "51", "89", "91", "93", "94"],
    "78": ["27", "28", "91", "92", "95"], "79": ["16", "17", "49", "85", "86"], "80": ["02", "59", "60", "62", "76"],
    "81": ["11", "12", "31", "34", "82"], "82": ["12", "31", "32", "46", "47", "81"], "83": ["04", "06", "13", "84"],
    "84": ["04", "07", "13", "26", "30", "83"], "85": ["17", "44", "49", "79"], "86": ["16", "36", "37", "49", "79", "87"],
    "87": ["16", "19", "23", "24", "36", "86"], "88": ["52", "54", "55", "67", "68", "70", "90"],
    "89": ["10", "21", "45", "58", "77"], "90": ["25", "68", "70", "88"], "91": ["28", "45", "77", "78", "92", "94"],
    "92": ["75", "78", "91", "93", "94", "95"], "93": ["75", "77", "92", "94", "95"],
    "94": ["75", "77", "91", "92", "93"], "95": ["27", "60", "78", "92", "93"],
}


class Groupe:
    def __init__(self, slug, nom, titre, secteurs, pour=""):
        self.slug, self.nom, self.titre, self.secteurs, self.pour = slug, nom, titre, set(secteurs), pour


# Grands secteurs suivis par Radar (clés de pipeline.SECTORS). « autres » n'a pas de page propre.
GROUPES = [
    Groupe("restaurants-commerces-de-bouche", "Restaurants et commerces de bouche",
           "Nouveaux restaurants et commerces de bouche", {"restauration", "commerce_alimentaire"},
           "fournisseurs de boissons, grossistes alimentaires, équipementiers de cuisine, assureurs"),
    Groupe("btp-artisans", "BTP et artisans du bâtiment", "Nouvelles entreprises du BTP et du bâtiment", {"btp"},
           "négoces de matériaux, loueurs de matériel, assureurs en décennale"),
    Groupe("beaute-bien-etre", "Beauté et bien-être", "Nouveaux salons de beauté et de bien-être",
           {"beaute_bien_etre"}, "fournisseurs de cosmétiques, de mobilier de salon et de logiciels de réservation"),
    Groupe("commerce-de-detail", "Commerce de détail", "Nouveaux commerces de détail", {"commerce_detail"},
           "agenceurs de magasins, fournisseurs de caisses, assureurs"),
    Groupe("auto-moto", "Auto et moto", "Nouveaux garages et commerces auto-moto", {"auto_moto"},
           "distributeurs de pièces, équipementiers d'atelier, assureurs"),
    Groupe("transport-livraison", "Transport et livraison", "Nouvelles entreprises de transport et de livraison",
           {"transport_livraison"}, "loueurs et vendeurs de véhicules, assureurs de flotte, cartes carburant"),
    Groupe("immobilier", "Immobilier", "Nouvelles sociétés immobilières", {"immobilier"},
           "éditeurs de logiciels, diagnostiqueurs, assureurs"),
    Groupe("conseil-services", "Conseil et services aux entreprises",
           "Nouvelles sociétés de conseil et de services aux entreprises", {"conseil_services_b2b"},
           "experts-comptables, banques, éditeurs de logiciels"),
]
AUTRES = Groupe("autres", "Holdings, sociétés civiles et autres activités", "", {"holding_sci", "ecommerce", "autre"})
PAR_SECTEUR = {s: g for g in GROUPES + [AUTRES] for s in g.secteurs}


# --- Outils -----------------------------------------------------------------------------
def e(x) -> str:
    return html.escape(html.unescape(str(x or "")).strip(), quote=True)


def slug(texte: str, n: int = 70) -> str:
    s = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:n].rstrip("-") or "x"


def dept_slug(d: str) -> str:
    return f"{slug(NOMS[d])}-{d.lower()}"


def url_dept(d: str) -> str:
    return f"/departements/{dept_slug(d)}/"


def url_secteur(d: str, g: Groupe) -> str:
    return f"{url_dept(d)}{g.slug}/"


def url_marches(d: str) -> str:
    return f"/marches/departements/{dept_slug(d)}/"


def url_acheteur(nom: str, siret: str) -> str:
    return f"/marches/acheteurs/{slug(nom, 60)}-{siret}/"


def nombre(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def pluriel(n: int, mot: str, mots: str | None = None) -> str:
    return f"{nombre(n)} {mot if n < 2 else (mots or mot + 's')}"


def date_courte(iso: str) -> str:
    try:
        return f"{dt.date.fromisoformat(iso[:10]):%d/%m/%Y}"
    except ValueError:
        return ""


def ville_propre(v: str) -> str:
    v = html.unescape(v or "").strip()
    return M.titre(v) if v.isupper() else v


def liste_fr(mots: list) -> str:
    mots = [m for m in mots if m]
    if len(mots) < 2:
        return "".join(mots)
    return ", ".join(mots[:-1]) + " et " + mots[-1]


def oppositions() -> set:
    """SIREN (9 chiffres) des personnes ayant exercé leur droit d'opposition."""
    if not OPPOSITION.exists():
        return set()
    out = set()
    for line in OPPOSITION.read_text(encoding="utf-8").splitlines():
        digits = re.sub(r"\D", "", line.split("#", 1)[0])
        if len(digits) >= 9:
            out.add(digits[:9])
    return out


def dept_of_cp(cp: str) -> str | None:
    cp = (cp or "").strip()
    if not re.fullmatch(r"\d{5}", cp):
        return None
    if cp.startswith("97"):
        return cp[:3] if cp[:3] in NOMS else None
    if cp.startswith("20"):
        return "2A" if int(cp) < 20200 else "2B"
    return cp[:2] if cp[:2] in NOMS else None


# --- Données BODACC (radar.db, table annonces) ----------------------------------------------
def annonces(con, depuis: dt.date, jusqua: dt.date, exclus: set) -> list:
    """Créations et reprises publiées entre deux dates, personnes morales uniquement,
    une ligne par SIREN (la plus récente)."""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM annonces WHERE personne_physique = 0 AND trim(coalesce(denomination,'')) != '' "
        "AND date_parution >= ? AND date_parution <= ? ORDER BY date_parution DESC, id DESC",
        (depuis.isoformat(), jusqua.isoformat())).fetchall()
    con.row_factory = None
    seen, out = set(), []
    for r in rows:
        r = dict(r)
        siren = re.sub(r"\D", "", r["siren"] or "")
        if siren[:9] in exclus or r["departement"] not in NOMS:
            continue
        cle = siren or r["id"]
        if cle in seen:
            continue
        seen.add(cle)
        out.append(r)
    return out


def groupe_of(r: dict) -> Groupe:
    return PAR_SECTEUR.get(r["secteur"], AUTRES)


def activite_courte(r: dict) -> str:
    a = court(r["activite"] or r["enseigne"] or "", 90) if (r["activite"] or r["enseigne"]) else ""
    return a or "Activité non précisée"


# --- Données DECP (radar.db, table decp_marches) --------------------------------------------
SCHEMA_MARCHES = """
CREATE TABLE IF NOT EXISTS decp_marches (
  id TEXT, acheteur_siret TEXT, titulaire_siret TEXT, objet TEXT, montant REAL,
  date_notification TEXT, date_publication TEXT, famille TEXT, departements TEXT,
  PRIMARY KEY (id, acheteur_siret));
CREATE INDEX IF NOT EXISTS ix_decp_notif ON decp_marches(date_notification);
CREATE INDEX IF NOT EXISTS ix_decp_acheteur ON decp_marches(acheteur_siret);
"""
CHAMPS_DECP = ("id,acheteur_id,titulaire_id_1,titulaire_id_2,titulaire_id_3,objet,montant,codecpv,"
               "datenotification,datepublicationdonnees,lieuexecution_code,lieuexecution_typecode,"
               "idmodification,dureemois,procedure,offresrecues,soustraitancedeclaree")


def fetch_decp(where: str) -> list:
    url = DECP_EXPORT + "?" + urllib.parse.urlencode({"where": where, "select": CHAMPS_DECP})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return json.load(r)
        except Exception as ex:
            if attempt == 2:
                raise
            print(f"  DECP : nouvel essai ({ex})", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    return []


def store_marches(con, records: list, oldest: str) -> int:
    con.executescript(SCHEMA_MARCHES)
    rows = []
    for rec in records:
        if rec.get("idmodification") not in M.VIDE:
            continue  # avenants : la ligne d'origine suffit
        m = M.from_record(rec)
        if not m["id"] or not m["date_notification"] or m["date_notification"] < oldest:
            continue
        rows.append((m["id"], m["acheteur_siret"], m["titulaire_siret"], m["objet"], m["montant"],
                     m["date_notification"], m["date_publication"], m["famille"], " ".join(m["departements"])))
    before = con.total_changes
    con.executemany("INSERT OR REPLACE INTO decp_marches VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return con.total_changes - before


def sync_marches(con, today: dt.date, fetch=fetch_decp) -> int:
    """Complète la table decp_marches : 12 mois au premier passage (par tranches mensuelles),
    puis seulement les données publiées depuis la dernière date connue."""
    con.executescript(SCHEMA_MARCHES)
    oldest = (today - dt.timedelta(days=31 * (MOIS_MARCHES + 1))).isoformat()
    (last,) = con.execute("SELECT max(date_publication) FROM decp_marches").fetchone()
    n = 0
    if not last:
        start = today - dt.timedelta(days=366)
        while start <= today:
            end = start + dt.timedelta(days=30)
            n += store_marches(con, fetch(f'datenotification>="{start}" and datenotification<"{end}"'), oldest)
            start = end
    else:
        since = (dt.date.fromisoformat(last) - dt.timedelta(days=2)).isoformat()
        n += store_marches(con, fetch(f'datepublicationdonnees>="{since}"'), oldest)
    con.execute("DELETE FROM decp_marches WHERE date_notification < ?", (oldest,))
    con.commit()
    return n


def marches_recents(con, depuis: dt.date) -> list:
    M._cache(con)
    con.executescript(SCHEMA_MARCHES)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT m.*, t.nom AS t_nom, t.nature AS t_nature, t.ville AS t_ville, t.cp AS t_cp, t.trouve AS t_trouve, "
        "a.nom AS a_nom, a.ville AS a_ville, a.cp AS a_cp, a.nature AS a_nature FROM decp_marches m "
        "LEFT JOIN entreprises t ON t.siret = m.titulaire_siret "
        "LEFT JOIN entreprises a ON a.siret = m.acheteur_siret "
        "WHERE m.date_notification >= ? ORDER BY m.date_notification DESC, m.id",
        (depuis.isoformat(),))]
    con.row_factory = None
    for r in rows:
        r["deps"] = (r["departements"] or "").split()
    return rows


def montrable(r: dict, exclus: set) -> bool:
    """Marché affichable : titulaire identifié comme personne morale, acheteur nommé."""
    return bool(r.get("t_nom") and M.personne_morale(r.get("t_nature")) and r.get("a_nom")
                and not str(r.get("a_nature") or "").startswith("1")
                and r["titulaire_siret"][:9] not in exclus and r["acheteur_siret"][:9] not in exclus)


def acheteur_dept(r: dict) -> str | None:
    return dept_of_cp(r.get("a_cp") or "")


# --- Enrichissement (Annuaire des entreprises), en parallèle et borné dans le temps ----------
class Limiteur:
    def __init__(self, par_seconde: float):
        self.pas, self.prochain, self.lock = 1 / par_seconde, 0.0, threading.Lock()

    def attendre(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.prochain)
            self.prochain = t + self.pas
        time.sleep(max(0, t - now))


def a_chercher(con, sirets: list) -> list:
    M._cache(con)
    known = {r[0]: r for r in con.execute("SELECT siret, trouve, nature, maj FROM entreprises")}
    stale = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    out, seen = [], set()
    for s in sirets:
        if not (s and len(s) == 14 and s.isdigit()) or s in seen:
            continue
        seen.add(s)
        k = known.get(s)
        if k is None or (k[3] or "") < stale or (k[1] == 1 and not (k[2] or "")):
            out.append(s)
    return out


def enrichir(con, sirets: list, budget_s: float, lookup=None, par_seconde: float = 6.0) -> int:
    """Cherche les SIRET inconnus (ou sans catégorie juridique) dans l'ordre donné, jusqu'à
    épuisement du budget de temps. Renvoie le nombre de fiches enregistrées."""
    lookup = lookup or M.lookup
    todo = a_chercher(con, sirets)
    if not todo or budget_s <= 0:
        return 0
    fin = time.monotonic() + budget_s
    lim = Limiteur(par_seconde)

    def un(s):
        if time.monotonic() > fin:
            return s, None
        lim.attendre()
        return s, lookup(s)

    n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for s, f in pool.map(un, todo):
            if f is None:
                continue
            con.execute("INSERT OR REPLACE INTO entreprises (siret,nom,ville,cp,naf,effectif,trouve,maj,nature) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (s, f.get("nom", ""), f.get("ville", ""), f.get("cp", ""), f.get("naf", ""),
                         f.get("effectif", ""), f["trouve"], dt.date.today().isoformat(), f.get("nature", "")))
            n += 1
            if n % 200 == 0:
                con.commit()
    con.commit()
    return n


def priorites(rows: list, today: dt.date) -> list:
    """SIRET à connaître, du plus utile au moins utile : marchés récents de chaque département,
    puis acheteurs les plus actifs, puis titulaires de leurs marchés."""
    out = []
    par_dept = collections.defaultdict(list)
    for r in rows:
        if len(r["deps"]) <= 3:
            for d in r["deps"]:
                par_dept[d].append(r)
    for d, rs in par_dept.items():
        for r in rs[:30]:
            out += [r["titulaire_siret"], r["acheteur_siret"]]
    compte = collections.Counter(r["acheteur_siret"] for r in rows if r["acheteur_siret"])
    actifs = [a for a, c in compte.most_common() if c >= MIN_ACHETEUR]
    out += actifs
    par_acheteur = collections.defaultdict(list)
    for r in rows:
        par_acheteur[r["acheteur_siret"]].append(r)
    for a in actifs:
        out += [r["titulaire_siret"] for r in par_acheteur[a][:20]]
    return out


# --- Gabarit commun -----------------------------------------------------------------------
LOGO = ('<svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true"><rect x="2" y="9" width="28" height="15" '
        'rx="4" transform="rotate(-6 16 16)" fill="#FFE34A"/><path d="M9 22V10h6.2c2.6 0 4.3 1.5 4.3 3.8 0 1.8-1 3-2.7 '
        '3.5L20 22h-3.3l-2.8-4.4H12V22z M12 15.2h3c1 0 1.6-.6 1.6-1.4S16 12.4 15 12.4h-3z" fill="#1D2125"/></svg>')
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='2' y='9' "
           "width='28' height='15' rx='4' transform='rotate(-6 16 16)' fill='%23FFE34A'/%3E%3Cpath d='M9 22V10h6.2c2.6 "
           "0 4.3 1.5 4.3 3.8 0 1.8-1 3-2.7 3.5L20 22h-3.3l-2.8-4.4H12V22z M12 15.2h3c1 0 1.6-.6 1.6-1.4S16 12.4 15 "
           "12.4h-3z' fill='%231D2125'/%3E%3C/svg%3E")
FOOTER_NAV = ('<a href="mailto:contact@radar-entreprises.fr">contact@radar-entreprises.fr</a>'
              '<a href="/departements/">Nouvelles entreprises par département</a>'
              '<a href="/marches/departements/">Marchés publics par département</a>'
              '<a href="/mentions-legales.html">Mentions légales et données personnelles</a>'
              '<a href="/cgv.html">Conditions générales de vente</a>')


def gabarit(titre: str, description: str, chemin: str, fil: list, corps: str, essai: str = "/#essai") -> str:
    canon = SITE + chemin
    fil_html = " › ".join(f'<a href="{u}">{e(n)}</a>' if u else f'<span aria-current="page">{e(n)}</span>'
                          for n, u in fil)
    ld = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": SITE + (u or chemin)} for i, (n, u) in enumerate(fil)]}
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titre)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{canon}">
<meta property="og:title" content="{e(titre)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{canon}">
<meta property="og:type" content="website">
<meta name="theme-color" content="#FFE34A">
<link rel="icon" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:ital,wght@0,400..900;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
</head>
<body class="seo">
<header>
  <div class="wrap">
    <a class="logo" href="/" aria-label="Radar Entreprises, accueil">
      {LOGO}
      Radar Entreprises
    </a>
    <a class="btn light small" href="{essai}">Essai gratuit</a>
  </div>
</header>
<main>
<div class="wrap"><nav class="crumbs" aria-label="Fil d'Ariane">{fil_html}</nav></div>
{corps}
</main>
<footer>
  <div class="wrap">
    <span>Radar Entreprises. Données BODACC et DECP, Licence Ouverte 2.0.</span>
    <nav aria-label="Liens utiles">{FOOTER_NAV}</nav>
  </div>
</footer>
</body>
</html>
"""


def bloc_liste(dept: str | None, produit: str, chemin: str) -> str:
    """Formulaire Netlify « liste-gratuite » (double opt-in traité par liste.py)."""
    options = "".join(f'<option value="{d}"{" selected" if d == dept else ""}>{d} · {e(NOMS[d])}</option>'
                      for d in NOMS)
    vide = "" if dept else '<option value="" selected disabled>Choisir un département</option>'
    zone = DANS[dept] if dept else "dans votre département"
    quoi = "10 marchés publics attribués" if produit == "marches" else "10 nouvelles entreprises"
    chk = lambda p: " checked" if p == produit else ""
    return f"""<section class="lg" id="liste-gratuite">
  <div class="wrap">
    <div>
      <h2>La liste gratuite, chaque lundi.</h2>
      <p class="lg-lead">Recevez chaque lundi matin {quoi} {e(zone)}, par email. Gratuit, une fois par semaine, désinscription en un clic.</p>
      <p class="lg-fine">Besoin de tout, chaque matin, avec les fichiers Excel ? <a href="{'/marches/#essai' if produit == 'marches' else '/#essai'}">Essai gratuit de l'alerte complète pendant 7 jours</a>.</p>
    </div>
    <form name="liste-gratuite" method="POST" action="/liste/merci.html" data-netlify="true" netlify-honeypot="site_web">
      <input type="hidden" name="form-name" value="liste-gratuite">
      <input type="hidden" name="page" value="{e(chemin)}">
      <p class="lg-pot"><label>Ne pas remplir <input name="site_web" tabindex="-1" autocomplete="off"></label></p>
      <label>Email professionnel
        <input type="email" name="email" required autocomplete="email" placeholder="vous@entreprise.fr">
      </label>
      <label>Département
        <select name="departement" required>{vide}{options}</select>
      </label>
      <fieldset>
        <legend>Liste</legend>
        <label><input type="radio" name="produit" value="entreprises"{chk("entreprises")}> Nouvelles entreprises</label>
        <label><input type="radio" name="produit" value="marches"{chk("marches")}> Marchés publics attribués</label>
      </fieldset>
      <label class="lg-consent"><input type="checkbox" name="consentement" value="oui" required> J'accepte de recevoir la liste hebdomadaire gratuite de Radar Entreprises. Un email me demandera de confirmer mon adresse.</label>
      <button class="btn" type="submit">Recevoir la liste gratuite</button>
    </form>
  </div>
</section>"""


def bloc_sources(marche: bool, maj: dt.date) -> str:
    if marche:
        src = (f'Données essentielles de la commande publique (<a href="{DECP_PAGE}">DECP, data.economie.gouv.fr</a>) '
               f'et <a href="{ANNUAIRE}">Annuaire des entreprises</a> pour le nom des titulaires et des acheteurs, '
               "réutilisés sous Licence Ouverte 2.0. Montants hors taxes tels que déclarés par l'acheteur.")
    else:
        src = (f'Annonces de créations, ventes et cessions publiées au <a href="{BODACC_PAGE}">BODACC</a> '
               f'(<a href="{BODACC_DONNEES}">données ouvertes de la DILA</a>), réutilisées sous Licence Ouverte 2.0. '
               "Le classement par secteur est automatique et peut comporter des erreurs.")
    return f"""<section class="src"><div class="wrap"><div class="note">
  <p><b>Mise à jour le {jour(maj)}.</b> Sources : {src}</p>
  <p>Seules des personnes morales (sociétés, associations, organismes publics) figurent sur cette page ; les entrepreneurs individuels n'y apparaissent pas. Toute personne concernée peut s'opposer à la diffusion des informations la concernant en écrivant à <a href="mailto:contact@radar-entreprises.fr">contact@radar-entreprises.fr</a>.</p>
</div></div></section>"""


def bloc_liens(titre: str, liens: list) -> str:
    liens = [(n, u) for n, u in liens if u]
    if not liens:
        return ""
    lis = "".join(f'<li><a href="{u}">{e(n)}</a></li>' for n, u in liens)
    return f'<div class="seo-links"><h2>{e(titre)}</h2><ul class="links">{lis}</ul></div>'


def table_annonces(rows: list) -> str:
    trs = []
    for r in rows:
        kind = "Création" if r["type"] == "creation" else "Reprise"
        lieu = e(ville_propre(r["ville"])) + (f' <span class="k">{e(r["cp"])}</span>' if r["cp"] else "")
        lien = (f'<a href="{e(r["url"])}" rel="nofollow noopener">Annonce officielle</a>'
                if (r["url"] or "").startswith("https://") else "")
        trs.append(f'<tr><td><b>{e(r["denomination"])}</b><span class="k">{kind} · {date_courte(r["date_parution"])}</span></td>'
                   f'<td>{activite_courte(r)}</td><td>{lieu}</td><td>{lien}</td></tr>')
    return ('<table class="tbl"><thead><tr><th>Entreprise</th><th>Activité</th><th>Ville</th><th>Source</th></tr>'
            f'</thead><tbody>{"".join(trs)}</tbody></table>')


def table_marches(rows: list, pages_acheteurs: dict, avec_acheteur: bool = True) -> str:
    trs = []
    for r in rows:
        tv = ville_propre(r.get("t_ville") or "")
        titulaire = f'<b>{e(r["t_nom"])}</b>' + (f'<span class="k">{e(tv)}</span>' if tv else "")
        u = pages_acheteurs.get(r["acheteur_siret"])
        a_nom = M.titre(M.nom_propre(r["a_nom"]))
        ach = f'<a href="{u}">{e(a_nom)}</a>' if u else e(a_nom)
        cells = [titulaire] + ([ach] if avec_acheteur else []) + [
            e(M.propre(r["objet"] or "", 150)) + f'<span class="k">{e(r["famille"])}</span>',
            M.euros(r["montant"]).replace("Montant non précisé", "Non précisé"),
            date_courte(r["date_notification"])]
        cls = [""] + ([""] if avec_acheteur else []) + ["", ' class="r"', ' class="r"']
        trs.append("<tr>" + "".join(f"<td{c}>{x}</td>" for c, x in zip(cls, cells)) + "</tr>")
    head = ["Titulaire"] + (["Acheteur"] if avec_acheteur else []) + ["Objet", "Montant HT", "Notifié le"]
    hcls = [""] + ([""] if avec_acheteur else []) + ["", ' class="r"', ' class="r"']
    ths = "".join(f"<th{c}>{h}</th>" for c, h in zip(hcls, head))
    return f'<table class="tbl"><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def top_villes(rows: list, n: int = 3) -> str:
    c = collections.Counter(ville_propre(r["ville"]) for r in rows if r["ville"])
    return liste_fr([f"{v} ({k})" for v, k in c.most_common(n)])


def voisins(d: str) -> list:
    if d in DOM:
        return [x for x in DOM if x != d and x in NOMS]
    return VOISINS.get(d, [])


# --- Génération -----------------------------------------------------------------------------
class Site:
    def __init__(self, today: dt.date):
        self.today = today
        self.pages = {}  # chemin -> (html, lastmod)

    def add(self, chemin: str, contenu: str, lastmod: dt.date | None = None):
        assert chemin not in self.pages, chemin
        self.pages[chemin] = (contenu, (lastmod or self.today).isoformat())


def pages_entreprises(site: Site, rows30: list):
    today = site.today
    debut7 = today - dt.timedelta(days=JOURS_SEMAINE - 1)
    par_dept30 = collections.defaultdict(list)
    for r in rows30:
        par_dept30[r["departement"]].append(r)
    semaine = {d: [r for r in rs if r["date_parution"] >= debut7.isoformat()] for d, rs in par_dept30.items()}

    # Pages département × secteur (30 jours, au moins 3 entrées)
    secteurs_publies = collections.defaultdict(list)
    for d, rs in par_dept30.items():
        for g in GROUPES:
            if len([r for r in rs if groupe_of(r) is g]) >= MIN_SECTEUR:
                secteurs_publies[d].append(g)
    depts_publies = sorted(d for d in NOMS if semaine.get(d))

    for d in depts_publies:
        rs = semaine[d]
        lastmod = dt.date.fromisoformat(max(r["date_parution"] for r in rs))
        par_g = collections.defaultdict(list)
        for r in rs:
            par_g[groupe_of(r)].append(r)
        ordre = sorted(par_g, key=lambda g: (g is AUTRES, -len(par_g[g])))
        n, nc = len(rs), sum(r["type"] == "creation" for r in rs)
        tops = [g for g in ordre if g is not AUTRES][:2]
        phrase_top = ""
        if tops:
            phrase_top = (f" Le secteur le plus représenté est « {tops[0].nom.lower()} » ({pluriel(len(par_g[tops[0]]), 'entrée')})"
                          + (f", devant « {tops[1].nom.lower()} » ({nombre(len(par_g[tops[1]]))})." if len(tops) > 1 else "."))
        intro = (f"Du {jour(debut7, False)} au {jour(today)}, {pluriel(n, 'société créée ou reprise', 'sociétés créées ou reprises')} "
                 f"{DANS[d]} {'a été publiée' if n == 1 else 'ont été publiées'} au BODACC : {pluriel(nc, 'création')} et {pluriel(n - nc, 'reprise')} de fonds de commerce."
                 f"{phrase_top} Les communes les plus actives sont {top_villes(rs)}. Chaque entrée renvoie vers l'annonce "
                 "officielle ; la liste est mise à jour chaque jour ouvré.")
        blocs = []
        for g in ordre:
            items = par_g[g]
            lien = (f'<p class="more"><a href="{url_secteur(d, g)}">{e(g.titre)} {e(DANS[d])} : les 30 derniers jours</a></p>'
                    if g in secteurs_publies[d] else "")
            reste = len(items) - EXEMPLES_SECTEUR
            plus = f'<p class="more muted">Et {pluriel(reste, "autre entrée", "autres entrées")} cette semaine.</p>' if reste > 0 else ""
            blocs.append(f'<div class="grp"><h2>{e(g.nom)} <span class="n">· {nombre(len(items))}</span></h2>'
                         f'{table_annonces(items[:EXEMPLES_SECTEUR])}{plus}{lien}</div>')
        liens = [(f"Nouvelles entreprises {DANS[v]}", url_dept(v)) for v in voisins(d) if v in depts_publies]
        liens_s = [(f"{g.titre} {DANS[d]}", url_secteur(d, g)) for g in secteurs_publies[d]]
        chemin = url_dept(d)
        corps = f"""<section class="seo-head"><div class="wrap">
  <h1>Nouvelles entreprises {e(DANS[d])}</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · {pluriel(n, 'entrée')} sur 7 jours · <a href="#liste-gratuite">Recevoir la liste chaque lundi</a></p>
</div></section>
<section class="seo-body"><div class="wrap">{''.join(blocs)}
{bloc_liens("Par secteur, sur 30 jours", liens_s)}
{bloc_liens("Départements voisins", liens)}
{bloc_liens("Aller plus loin", [("Toutes les nouvelles entreprises par département", "/departements/"), (f"Marchés publics attribués {DANS[d]}", url_marches(d) if d in site.depts_marches else None), ("L'alerte quotidienne Radar Entreprises", "/")])}
</div></section>
{bloc_liste(d, "entreprises", chemin)}
{bloc_sources(False, today)}"""
        site.add(chemin, gabarit(
            f"Nouvelles entreprises {DANS[d]} ({d}) : créations et reprises de la semaine",
            f"{pluriel(n, 'société créée ou reprise', 'sociétés créées ou reprises')} {DANS[d]} du {jour(debut7, False)} au {jour(today)}, "
            f"par secteur, d'après le BODACC : nom, activité, ville et annonce officielle.",
            chemin, [("Accueil", "/"), ("Départements", "/departements/"), (NOMS[d], None)], corps), lastmod)

        # Pages secteur
        for g in secteurs_publies[d]:
            items = [r for r in par_dept30[d] if groupe_of(r) is g]
            n30, n7 = len(items), sum(r["date_parution"] >= debut7.isoformat() for r in items)
            nc = sum(r["type"] == "creation" for r in items)
            sous = collections.Counter(r["secteur"] for r in items)
            detail = ""
            if len(g.secteurs) > 1:
                lib = {"restauration": "restaurants, bars et traiteurs", "commerce_alimentaire": "commerces alimentaires"}
                detail = " On y trouve " + liste_fr([f"{nombre(k)} {lib.get(s, s)}" for s, k in sous.most_common()]) + "."
            intro = (f"Sur les 30 derniers jours, {pluriel(n30, 'entreprise', 'entreprises')} du secteur « {g.nom.lower()} » "
                     f"ont été créées ou reprises {DANS[d]} d'après le BODACC, dont {nombre(n7)} ces 7 derniers jours "
                     f"et {pluriel(nc, 'création')} au total.{detail} Les communes concernées sont surtout {top_villes(items)}. "
                     f"Ces nouveaux établissements choisissent leurs fournisseurs dans leurs premières semaines : "
                     f"une liste suivie notamment par les {g.pour}.")
            montres = items[:30]
            reste = n30 - len(montres)
            autres_s = [(f"{x.titre} {DANS[d]}", url_secteur(d, x)) for x in secteurs_publies[d] if x is not g]
            memes = [(f"{g.titre} {DANS[v]}", url_secteur(v, g)) for v in voisins(d) if g in secteurs_publies.get(v, [])]
            chemin_s = url_secteur(d, g)
            corps = f"""<section class="seo-head"><div class="wrap">
  <h1>{e(g.titre)} {e(DANS[d])}</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · {pluriel(n30, 'entrée')} sur 30 jours · <a href="#liste-gratuite">Recevoir la liste chaque lundi</a></p>
</div></section>
<section class="seo-body"><div class="wrap">
<div class="grp"><h2>Les plus récentes <span class="n">· {nombre(len(montres))} sur {nombre(n30)}</span></h2>{table_annonces(montres)}
{f'<p class="more muted">Et {pluriel(reste, "entrée plus ancienne", "entrées plus anciennes")} sur la période.</p>' if reste > 0 else ''}</div>
{bloc_liens("Autres secteurs " + DANS[d], autres_s)}
{bloc_liens("Même secteur, départements voisins", memes)}
{bloc_liens("Aller plus loin", [(f"Toutes les nouvelles entreprises {DANS[d]}", url_dept(d)), ("Les nouvelles entreprises par département", "/departements/"), ("L'alerte quotidienne Radar Entreprises", "/")])}
</div></section>
{bloc_liste(d, "entreprises", chemin_s)}
{bloc_sources(False, today)}"""
            site.add(chemin_s, gabarit(
                f"{g.titre} {DANS[d]} ({d}) : les 30 derniers jours",
                f"{pluriel(n30, 'entreprise', 'entreprises')} « {g.nom.lower()} » créées ou reprises {DANS[d]} en 30 jours, "
                f"dont {nombre(n7)} cette semaine : nom, activité, commune et annonce BODACC.",
                chemin_s, [("Accueil", "/"), ("Départements", "/departements/"), (NOMS[d], url_dept(d)), (g.nom, None)],
                corps), dt.date.fromisoformat(items[0]["date_parution"]))

    # Index national
    total = sum(len(semaine[d]) for d in depts_publies)
    c = collections.Counter(groupe_of(r).nom.lower() for d in depts_publies for r in semaine[d] if groupe_of(r) is not AUTRES)
    top = c.most_common(1)
    lis = "".join(f'<li><a href="{url_dept(d)}">{d} · {e(NOMS[d])}</a> <span class="n">{nombre(len(semaine[d]))}</span></li>'
                  for d in depts_publies)
    intro = (f"Du {jour(debut7, False)} au {jour(today)}, {pluriel(total, 'société créée ou reprise', 'sociétés créées ou reprises')} "
             f"{'a été publiée' if total == 1 else 'ont été publiées'} au BODACC en France"
             + (f", avec en tête le secteur « {top[0][0]} » ({nombre(top[0][1])})" if top else "")
             + ". Choisissez un département pour voir les nouvelles entreprises de la semaine, classées par secteur, "
             "et les pages secteur sur 30 jours. Les chiffres indiquent le nombre de sociétés publiées sur 7 jours.")
    corps = f"""<section class="seo-head"><div class="wrap">
  <h1>Nouvelles entreprises par département</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · <a href="/marches/departements/">Marchés publics attribués par département</a></p>
</div></section>
<section class="seo-body"><div class="wrap"><ul class="links counts">{lis}</ul></div></section>
{bloc_liste(None, "entreprises", "/departements/")}
{bloc_sources(False, today)}"""
    site.add("/departements/", gabarit(
        "Nouvelles entreprises par département : créations et reprises de la semaine",
        f"{pluriel(total, 'société créée ou reprise', 'sociétés créées ou reprises')} en France cette semaine d'après le BODACC. "
        "Consultez les nouvelles entreprises de votre département, par secteur.",
        "/departements/", [("Accueil", "/"), ("Départements", None)], corps))
    return depts_publies


def pages_marches(site: Site, rows12: list, exclus: set):
    today = site.today
    debut30 = (today - dt.timedelta(days=29)).isoformat()
    # Acheteurs : au moins 3 marchés en 12 mois, nom connu, au moins un marché affichable
    par_acheteur = collections.defaultdict(list)
    for r in rows12:
        if r["acheteur_siret"]:
            par_acheteur[r["acheteur_siret"]].append(r)
    acheteurs = {}
    for a, rs in par_acheteur.items():
        if len(rs) < MIN_ACHETEUR or not rs[0].get("a_nom") or str(rs[0].get("a_nature") or "").startswith("1"):
            continue
        visibles = [r for r in rs if montrable(r, exclus)]
        if not visibles:
            continue
        lieux = collections.Counter(d for r in rs for d in r["deps"] if len(r["deps"]) == 1)
        dep = acheteur_dept(rs[0]) or (lieux.most_common(1)[0][0] if lieux else None)
        if dep not in NOMS:
            continue
        acheteurs[a] = {"nom": M.titre(M.nom_propre(rs[0]["a_nom"])), "dept": dep, "rows": rs, "visibles": visibles,
                        "ville": ville_propre(rs[0].get("a_ville") or "")}
    urls_ach = {a: url_acheteur(x["nom"], a) for a, x in acheteurs.items()}
    # Homonymes (même nom, même commune, plusieurs SIRET) : le SIRET distingue titre et description.
    homonymes = collections.Counter((x["nom"], x["ville"]) for x in acheteurs.values())
    for a, x in acheteurs.items():
        x["suffixe"] = f", SIRET {a}" if homonymes[(x["nom"], x["ville"])] > 1 else ""

    par_dept = collections.defaultdict(list)
    for r in rows12:
        if 0 < len(r["deps"]) <= 3:
            for d in r["deps"]:
                if d in NOMS:
                    par_dept[d].append(r)
    depts = sorted(d for d, rs in par_dept.items() if any(montrable(r, exclus) for r in rs))
    site.depts_marches = set(depts)
    ach_par_dept = collections.defaultdict(list)
    for a, x in acheteurs.items():
        ach_par_dept[x["dept"]].append(a)

    for d in depts:
        rs = par_dept[d]
        vis = [r for r in rs if montrable(r, exclus)][:MARCHES_DEPT]
        n30 = sum(r["date_notification"] >= debut30 for r in rs)
        somme = sum(r["montant"] or 0 for r in rs)
        fam = collections.Counter(r["famille"] for r in rs).most_common(2)
        ach_c = collections.Counter(M.titre(M.nom_propre(r["a_nom"])) for r in rs if r.get("a_nom")).most_common(3)
        intro = (f"{pluriel(len(rs), 'marché public exécuté', 'marchés publics exécutés')} {DANS[d]} "
                 f"{'a été notifié' if len(rs) == 1 else 'ont été notifiés'} au cours des "
                 f"12 derniers mois d'après les données essentielles de la commande publique, dont {nombre(n30)} ces 30 derniers jours"
                 + (f", pour un montant total déclaré de {M.euros(somme)} HT" if somme else "") + "."
                 + (f" Les domaines les plus fréquents sont {liste_fr([f'{f} ({nombre(k)})' for f, k in fam])}." if fam else "")
                 + (f" Les acheteurs les plus actifs : {liste_fr([f'{n} ({k})' for n, k in ach_c])}." if ach_c else "")
                 + " Ci-dessous, les marchés les plus récents avec l'entreprise qui les a remportés : un signal utile pour "
                 "proposer ses services de sous-traitance, de fourniture ou de location aux titulaires.")
        liste_ach = sorted(ach_par_dept[d], key=lambda a: (-len(acheteurs[a]["rows"]), acheteurs[a]["nom"]))
        liens_ach = [(f"{acheteurs[a]['nom']} ({len(acheteurs[a]['rows'])} marchés)", urls_ach[a]) for a in liste_ach[:80]]
        liens_v = [(f"Marchés publics attribués {DANS[v]}", url_marches(v)) for v in voisins(d) if v in depts]
        chemin = url_marches(d)
        corps = f"""<section class="seo-head"><div class="wrap">
  <h1>Marchés publics attribués {e(DANS[d])}</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · <a href="#liste-gratuite">Recevoir 10 marchés chaque lundi</a></p>
</div></section>
<section class="seo-body"><div class="wrap">
<div class="grp"><h2>Les {nombre(len(vis))} marchés les plus récents</h2>{table_marches(vis, urls_ach)}
<p class="more muted">Marchés dont le titulaire est une personne morale identifiée dans l'Annuaire des entreprises.</p></div>
{bloc_liens("Acheteurs publics les plus actifs " + DANS[d], liens_ach)}
{bloc_liens("Départements voisins", liens_v)}
{bloc_liens("Aller plus loin", [("Marchés publics attribués par département", "/marches/departements/"), (f"Nouvelles entreprises {DANS[d]}", url_dept(d) if d in site.depts_entreprises else None), ("L'alerte hebdomadaire Radar Marchés attribués", "/marches/")])}
</div></section>
{bloc_liste(d, "marches", chemin)}
{bloc_sources(True, today)}"""
        site.add(chemin, gabarit(
            f"Marchés publics attribués {DANS[d]} ({d}) : titulaires, acheteurs, montants",
            f"{pluriel(n30, 'marché public attribué', 'marchés publics attribués')} {DANS[d]} ces 30 derniers jours : "
            "entreprise titulaire, acheteur, objet, montant et date, d'après les DECP.",
            chemin, [("Accueil", "/"), ("Marchés", "/marches/"), ("Départements", "/marches/departements/"), (NOMS[d], None)],
            corps, essai="/marches/#essai"), dt.date.fromisoformat(vis[0]["date_notification"]) if vis else today)

    for a, x in acheteurs.items():
        rs, vis, d = x["rows"], x["visibles"][:20], x["dept"]
        somme = sum(r["montant"] or 0 for r in rs)
        fam = collections.Counter(r["famille"] for r in rs).most_common(2)
        tit = collections.Counter(r["t_nom"] for r in x["visibles"]).most_common(3)
        caches = len(rs) - len(x["visibles"])
        lieu = f" ({x['ville'] or NOMS[d]}{x['suffixe']})"
        intro = (f"{x['nom']}{lieu} a notifié {pluriel(len(rs), 'marché public', 'marchés publics')} au cours des 12 derniers mois "
                 "d'après les données essentielles de la commande publique"
                 + (f", pour un montant total déclaré de {M.euros(somme)} HT" if somme else "") + "."
                 + (f" Domaines principaux : {liste_fr([f'{f} ({nombre(k)})' for f, k in fam])}." if fam else "")
                 + (f" Entreprises titulaires les plus fréquentes : {liste_fr([f'{n} ({k})' for n, k in tit])}." if tit else "")
                 + (f" {pluriel(caches, 'marché n’est', 'marchés ne sont')} pas détaillé{'s' if caches > 1 else ''} ici "
                    "(titulaire entrepreneur individuel ou non identifié)." if caches else ""))
        autres = sorted((b for b in ach_par_dept[d] if b != a), key=lambda b: -len(acheteurs[b]["rows"]))[:12]
        chemin = urls_ach[a]
        corps = f"""<section class="seo-head"><div class="wrap">
  <h1>Marchés publics attribués par {e(x['nom'])}</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · SIRET {a} · <a href="{ANNUAIRE}etablissement/{a}" rel="nofollow">Fiche de l'acheteur</a></p>
</div></section>
<section class="seo-body"><div class="wrap">
<div class="grp"><h2>Marchés récents <span class="n">· {nombre(len(vis))}</span></h2>{table_marches(vis, {}, avec_acheteur=False)}</div>
{bloc_liens("Autres acheteurs publics " + DANS[d], [(acheteurs[b]["nom"], urls_ach[b]) for b in autres])}
{bloc_liens("Aller plus loin", [(f"Marchés publics attribués {DANS[d]}", url_marches(d) if d in depts else None), ("Marchés publics attribués par département", "/marches/departements/"), ("L'alerte hebdomadaire Radar Marchés attribués", "/marches/")])}
</div></section>
{bloc_liste(d, "marches", chemin)}
{bloc_sources(True, today)}"""
        site.add(chemin, gabarit(
            f"Marchés publics attribués par {x['nom']}{lieu} : titulaires et montants",
            f"{pluriel(len(rs), 'marché', 'marchés')} notifiés en 12 mois par {x['nom']}{lieu} : entreprises titulaires, "
            "objet, montant et date de notification, d'après les DECP.",
            chemin, [("Accueil", "/"), ("Marchés", "/marches/"), ("Départements", "/marches/departements/"),
                     (NOMS[d], url_marches(d) if d in depts else "/marches/departements/"), (x["nom"], None)],
            corps, essai="/marches/#essai"), dt.date.fromisoformat(vis[0]["date_notification"]))

    # Index national
    n30 = {d: sum(r["date_notification"] >= debut30 for r in par_dept[d]) for d in depts}
    total30 = sum(r["date_notification"] >= debut30 for r in rows12)
    lis = "".join(f'<li><a href="{url_marches(d)}">{d} · {e(NOMS[d])}</a> <span class="n">{nombre(n30[d])}</span></li>'
                  for d in depts)
    intro = (f"{pluriel(total30, 'marché public a été notifié', 'marchés publics ont été notifiés')} en France ces 30 derniers jours "
             f"d'après les données essentielles de la commande publique, et {pluriel(len(rows12), 'marché')} sur 12 mois. "
             "Choisissez un département pour voir les derniers marchés attribués, leurs titulaires et les acheteurs publics "
             "les plus actifs. Les chiffres indiquent le nombre de marchés notifiés sur 30 jours.")
    corps = f"""<section class="seo-head"><div class="wrap">
  <h1>Marchés publics attribués par département</h1>
  <p class="intro">{e(intro)}</p>
  <p class="maj">Mise à jour le {jour(today)} · <a href="/departements/">Nouvelles entreprises par département</a></p>
</div></section>
<section class="seo-body"><div class="wrap"><ul class="links counts">{lis}</ul></div></section>
{bloc_liste(None, "marches", "/marches/departements/")}
{bloc_sources(True, today)}"""
    if depts:
        site.add("/marches/departements/", gabarit(
            "Marchés publics attribués par département : titulaires et acheteurs",
            f"{pluriel(total30, 'marché public attribué', 'marchés publics attribués')} en France en 30 jours. "
            "Par département : entreprises titulaires, acheteurs, objets et montants (DECP).",
            "/marches/departements/", [("Accueil", "/"), ("Marchés", "/marches/"), ("Départements", None)],
            corps, essai="/marches/#essai"))
    return depts, acheteurs


def date_git(path: Path, today: dt.date) -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", str(path)], cwd=ROOT,
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return out or today.isoformat()
    except Exception:
        return today.isoformat()


def sitemap(site: Site, site_dir: Path) -> str:
    urls = []
    for p in PAGES_FIXES:
        f = site_dir / (p.lstrip("/") + "index.html" if p.endswith("/") else p.lstrip("/"))
        if f.exists():
            urls.append((p, date_git(f, site.today)))
    urls += [(p, lm) for p, (_, lm) in sorted(site.pages.items())]
    body = "".join(f"  <url><loc>{html.escape(SITE + p)}</loc><lastmod>{lm}</lastmod></url>\n" for p, lm in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}</urlset>\n')


def ecrire(site: Site, site_dir: Path):
    for d in GENERES:
        shutil.rmtree(site_dir / d, ignore_errors=True)
    for chemin, (contenu, _) in site.pages.items():
        f = site_dir / chemin.strip("/") / "index.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(contenu, encoding="utf-8")
    (site_dir / "sitemap.xml").write_text(sitemap(site, site_dir), encoding="utf-8")


def generer(con, today: dt.date, site_dir: Path = SITE_DIR) -> Site:
    exclus = oppositions()
    site = Site(today)
    site.depts_marches, site.depts_entreprises = set(), set()
    rows30 = annonces(con, today - dt.timedelta(days=JOURS_SECTEUR - 1), today, exclus)
    rows12 = marches_recents(con, today - dt.timedelta(days=365))
    # Les deux familles de pages se citent : on calcule d'abord quels départements ont des marchés.
    debut7 = (today - dt.timedelta(days=JOURS_SEMAINE - 1)).isoformat()
    site.depts_entreprises = {r["departement"] for r in rows30 if r["date_parution"] >= debut7}
    site.depts_marches = {d for r in rows12 if 0 < len(r["deps"]) <= 3 and montrable(r, exclus) for d in r["deps"]}
    pages_marches(site, rows12, exclus)
    pages_entreprises(site, rows30)
    ecrire(site, site_dir)
    return site


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--budget-enrichissement", type=float, default=480, help="secondes d'appels à l'Annuaire")
    ap.add_argument("--sans-reseau", action="store_true")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today)
    con = sqlite3.connect(P.DB)
    con.executescript(P.SCHEMA)
    if not a.sans_reseau:
        try:
            print(f"DECP : {sync_marches(con, today)} marchés enregistrés ou mis à jour")
        except Exception as ex:  # source indisponible : pages générées avec la base existante
            print(f"::warning::DECP indisponible, pages marchés construites sur la base existante : {ex}")
        rows = marches_recents(con, today - dt.timedelta(days=365))
        n = enrichir(con, priorites(rows, today), a.budget_enrichissement)
        print(f"Annuaire des entreprises : {n} fiches ajoutées")
    site = generer(con, today)
    con.close()
    kinds = collections.Counter(p.split("/")[1] if not p.startswith("/marches/") else "/".join(p.split("/")[1:3])
                                for p in site.pages)
    print(f"{len(site.pages)} pages écrites dans site/ : " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
