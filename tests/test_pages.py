"""Pages de référencement (pages.py), sans réseau, sur une base de test.

Vérifie : pages écrites seulement quand il y a des entrées, aucune personne physique ni société
opposée, titres et descriptions uniques, canonique, sources, droit d'opposition, formulaire de la
liste gratuite, liens internes, sitemap.xml valide et robots.txt.

Lancement : python tests/test_pages.py
"""
import datetime as dt
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import donnees_test as D  # noqa: E402
from donnees_test import ROOT, TODAY, pages  # noqa: E402

OK = []


def check(cond, msg):
    assert cond, msg
    OK.append(msg)


tmp = Path(tempfile.mkdtemp())
site_dir = tmp / "site"
shutil.copytree(ROOT / "site", site_dir, ignore=shutil.ignore_patterns("departements", "acheteurs", "sitemap.xml"))
opp = tmp / "opposition.txt"
opp.write_text(f"# test\n{D.SIREN_OPPOSITION}  # demande de test\n")
pages.OPPOSITION = opp
con = D.build(tmp / "radar.db")

site = pages.generer(con, TODAY, site_dir)
chemins = set(site.pages)
fichiers = {"/" + str(f.parent.relative_to(site_dir)).replace("\\", "/") + "/": f.read_text(encoding="utf-8")
            for f in site_dir.glob("**/index.html")
            if f.parent.relative_to(site_dir).parts[:1] == ("departements",)
            or f.parent.relative_to(site_dir).parts[:2] in (("marches", "departements"), ("marches", "acheteurs"))}

# 1. Quelles pages
check("/departements/" in chemins and "/marches/departements/" in chemins, "Index nationaux générés")
check("/departements/gironde-33/" in chemins, "Page département avec des entrées cette semaine")
check("/departements/gironde-33/restaurants-commerces-de-bouche/" in chemins,
      "Page département × secteur dès 3 entrées sur 30 jours")
check("/departements/charente-maritime-17/" in chemins and
      not any(p.startswith("/departements/charente-maritime-17/") and p != "/departements/charente-maritime-17/" for p in chemins),
      "Pas de page secteur sous 3 entrées")
check("/departements/landes-40/" not in chemins and "/departements/charente-16/" not in chemins,
      "Pas de page département sans entrée cette semaine")
check("/marches/departements/gironde-33/" in chemins, "Page marchés du département")
ach = [p for p in chemins if p.startswith("/marches/acheteurs/")]
check(len(ach) == 1 and ach[0].endswith("-21330063500017/") and "commune-de-merignac" in ach[0],
      "Page acheteur seulement à partir de 3 marchés sur 12 mois")
check(set(fichiers) == {p for p in chemins}, "Fichiers écrits = pages déclarées (anciens fichiers effacés)")

# 2. Pas de page vide, pas de personne physique
for p, h in fichiers.items():
    if p in ("/departements/", "/marches/departements/"):
        check(len(re.findall(r'<li><a href="/', h)) >= 1, f"Index non vide : {p}")
    else:
        check(re.search(r"<tbody><tr>", h) is not None, f"Au moins une entrée : {p}")
    check(not any(x in h for x in D.INTERDITS), f"Ni personne physique ni opposition : {p}")
t33 = fichiers["/departements/gironde-33/"]
check("SOCIETE TEST 0" in t33 and "Mérignac" in t33, "Sociétés de la semaine listées avec leur ville")
mg = fichiers["/marches/departements/gironde-33/"]
check("COLAS FRANCE" in mg and "Commune de Merignac" in mg and "50\u202f000 €" in mg, "Marché : titulaire, acheteur, montant")
check("lot 8" not in mg.lower(), "Marché de plus de 12 mois absent")

# 3. Contenu exigé sur chaque page
titres, descs = set(), set()
for p, h in fichiers.items():
    titre = re.search(r"<title>(.*?)</title>", h).group(1)
    desc = re.search(r'<meta name="description" content="(.*?)">', h).group(1)
    check(titre not in titres and desc not in descs, f"Titre et description uniques : {p}")
    titres.add(titre)
    descs.add(desc)
    check(f'<link rel="canonical" href="https://radar-entreprises.fr{p}">' in h, f"Canonique : {p}")
    check(re.search(r'<p class="intro">[^<]{120,}</p>', h) is not None, f"Paragraphe d'introduction rédigé : {p}")
    check("Mise à jour le" in h, f"Date de mise à jour : {p}")
    check(("bodacc.fr" in h) or ("data.economie.gouv.fr" in h), f"Sources avec liens : {p}")
    check("mailto:contact@radar-entreprises.fr" in h and "opposer" in h, f"Droit d'opposition : {p}")
    check('name="liste-gratuite"' in h and 'data-netlify="true"' in h and 'name="produit"' in h
          and 'name="departement"' in h and 'type="email"' in h, f"Formulaire liste gratuite : {p}")
    check('href="/departements/"' in h and 'href="/marches/departements/"' in h, f"Liens vers les pages nationales : {p}")
check('href="/departements/charente-maritime-17/"' in t33, "Lien vers un département voisin")
check('href="/marches/departements/gironde-33/"' in t33 and 'href="/departements/gironde-33/"' in mg,
      "Liens croisés entreprises ↔ marchés")
check('href="/departements/gironde-33/restaurants-commerces-de-bouche/"' in t33, "Lien vers la page secteur")
check(re.search(r'value="33" selected', t33) and 'value="entreprises" checked' in t33
      and 'value="marches" checked' in mg, "Formulaire pré-rempli (département, produit)")

# 4. sitemap.xml et robots.txt
sm = ET.parse(site_dir / "sitemap.xml").getroot()
ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
check(sm.tag == ns + "urlset", "Sitemap : espace de noms sitemaps.org")
locs = [u.find(ns + "loc").text for u in sm]
mods = [u.find(ns + "lastmod").text for u in sm]
check(len(locs) == len(set(locs)), "Sitemap : aucune URL en double")
check(all(l.startswith("https://radar-entreprises.fr/") for l in locs), "Sitemap : URL absolues en https")
check(all(dt.date.fromisoformat(m) <= TODAY for m in mods), "Sitemap : lastmod au format AAAA-MM-JJ")
check({"https://radar-entreprises.fr" + p for p in chemins} <= set(locs)
      and "https://radar-entreprises.fr/" in locs and "https://radar-entreprises.fr/marches/" in locs,
      "Sitemap : toutes les pages générées et les pages principales")
check(not any("/liste/" in l or "merci" in l for l in locs), "Sitemap : pas de pages techniques")
robots = (ROOT / "site" / "robots.txt").read_text()
check("Sitemap: https://radar-entreprises.fr/sitemap.xml" in robots and "Disallow: /liste/" in robots, "robots.txt")
for f in ("index.html", "marches/index.html", "mentions-legales.html", "cgv.html"):
    h = (ROOT / "site" / f).read_text(encoding="utf-8")
    check('href="/departements/"' in h and 'href="/marches/departements/"' in h, f"Liens depuis {f}")

# 5. Collecte DECP incrémentale et enrichissement
appels = []


def faux_decp(where):
    appels.append(where)
    return [{"id": f"N{len(appels)}", "acheteur_id": "21330063500017", "titulaire_id_1": "55210055400013",
             "objet": "Entretien", "montant": 1000, "codecpv": "50000000", "datenotification": TODAY.isoformat(),
             "datepublicationdonnees": TODAY.isoformat(), "lieuexecution_code": "33",
             "lieuexecution_typecode": "Code département", "idmodification": "CDL"},
            {"id": "AV", "acheteur_id": "1", "idmodification": "1", "datenotification": TODAY.isoformat()}]


import sqlite3  # noqa: E402
c2 = sqlite3.connect(tmp / "vide.db")
pages.sync_marches(c2, TODAY, fetch=faux_decp)
check(len(appels) == 13 and all("datenotification" in a for a in appels), "DECP : 12 mois chargés par tranches au premier passage")
n_avant = len(appels)
pages.sync_marches(c2, TODAY, fetch=faux_decp)
check(len(appels) == n_avant + 1 and "datepublicationdonnees" in appels[-1], "DECP : ensuite, seulement les nouvelles publications")
check(c2.execute("select count(*) from decp_marches where id='AV'").fetchone()[0] == 0, "DECP : avenants ignorés")
vus = []
n = pages.enrichir(c2, ["55210055400013", "55210055400013", "123"], 30,
                   lookup=lambda s: (vus.append(s), {"trouve": 1, "nom": "X", "nature": "5710"})[1])
check(n == 1 and vus == ["55210055400013"], "Enrichissement : SIRET valides, une seule fois")
check(pages.enrichir(c2, ["55210055400013"], 30, lookup=lambda s: 1 / 0) == 0, "Enrichissement : fiche déjà connue non recherchée")
check(c2.execute("select nature from entreprises").fetchone()[0] == "5710", "Catégorie juridique mise en cache")
check(pages.M.personne_morale("5710") and not pages.M.personne_morale("1000") and not pages.M.personne_morale(""),
      "Catégorie 1xxx (entrepreneur individuel) ou inconnue = pas affichée")

print(f"\n{len(OK)} vérifications réussies.")
shutil.rmtree(tmp)
