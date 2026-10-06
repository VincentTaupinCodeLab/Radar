"""Met à jour les chiffres réels affichés sur la page d'accueil (site/index.html).

Chaque semaine :
- volumes de créations et reprises de la semaine écoulée, France entière, par secteur ;
- exemple d'alerte « restauration » (16, 17, 33) tiré d'un vrai jour de parution ;
- marchés de travaux attribués sur 30 jours et trois exemples en Nouvelle-Aquitaine.

Le script ne touche qu'aux zones balisées <!-- auto:xxx --> et aux <span data-auto="xxx">.
Si une source ne répond pas ou renvoie trop peu de données, la zone correspondante
garde son contenu actuel. Usage : python site_stats.py [--today AAAA-MM-JJ]
"""
import argparse
import collections
import datetime as dt
import html
import re
import sys
from pathlib import Path

import decp
import marches as M
import pipeline as P

PAGE = Path(__file__).parent / "site" / "index.html"
PAGE_MARCHES = Path(__file__).parent / "site" / "marches" / "index.html"
HERO_DEPTS = ["16", "17", "33"]
MARCHES_DEPTS = decp.REGIONS.get("75", [])  # Nouvelle-Aquitaine
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
DEPT_NOMS = {"16": "Charente", "17": "Charente-Maritime", "19": "Corrèze", "23": "Creuse",
             "24": "Dordogne", "33": "Gironde", "40": "Landes", "47": "Lot-et-Garonne",
             "64": "Pyrénées-Atlantiques", "79": "Deux-Sèvres", "86": "Vienne", "87": "Haute-Vienne"}

# Secteurs affichés, avec le type de client à qui ils servent.
SECTEURS = [
    ("transport_livraison", "Transport et livraison", "Véhicules, carburant, assurance flotte"),
    ("conseil_services_b2b", "Conseil et services aux entreprises", "Comptables, banques, logiciels"),
    ("commerce_detail", "Commerce de détail", "Agencement, encaissement, assurance"),
    ("restauration", "Restaurants, bars et traiteurs", "Boissons, café, produits frais, caisses"),
    ("btp", "BTP et artisans", "Négoces de matériaux, assurance décennale"),
    ("auto_moto", "Auto et moto", "Pièces, équipement d'atelier"),
    ("immobilier", "Immobilier", "Logiciels, assurance, diagnostics"),
    ("beaute_bien_etre", "Beauté et bien-être", "Cosmétiques, mobilier de salon"),
    ("commerce_alimentaire", "Commerce alimentaire", "Grossistes, froid, emballages"),
]


def nb(n: float) -> str:
    """12 345 avec espace insécable, à la française."""
    return f"{int(round(n)):,}".replace(",", "&nbsp;")


def jour(d: dt.date, annee: bool = True) -> str:
    j = "1er" if d.day == 1 else str(d.day)
    return f"{j} {MOIS[d.month - 1]}" + (f" {d.year}" if annee else "")


def euros(x: float) -> str:
    if x >= 1e9:
        return f"{x / 1e9:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " milliard" + ("s" if x >= 2e9 else "") + " d'euros"
    return f"{nb(x / 1e6)} millions d'euros"


GENERIQUE = re.compile(
    r"^(l'|la )?(exploitation|acquisition|reprise)( directe)? (de |d')?(un |une |tous |le |les )?(fonds|fond)"
    r"( de commerce| artisanal| commercial)?\s*(de |d'|du |des )?|^(toutes )?activit[ée]s? de |^vente [aà] consommer.*?(de |d')", re.I)
LIAISONS = {"de", "d'", "du", "des", "à", "a", "et", "ou", "en", "la", "le", "les", "sur", "au", "aux", "par", "pour", "dans", "avec", "«", "»"}


def court(activite: str, n: int = 52) -> str:
    """Partie lisible du libellé d'activité (sans formule juridique), avec majuscule."""
    a = html.unescape(activite).strip()
    a = re.split(r"\s+[-–(]\s*|;|\.\s", a)[0].strip(" ,:")
    a = GENERIQUE.sub("", a).strip(" ,:") or a
    a = re.sub(r"\bet de (?=[a-zàâéèêîôûç]{2})", "et ", a)
    if len(a) > n:
        cut = a[:n]
        a = cut[:cut.rfind(",")] if "," in cut[n // 3:] else cut[:cut.rfind(" ")]
        mots = a.split()
        while mots and mots[-1].lower().strip(",") in LIAISONS:
            mots.pop()
        a = " ".join(mots).strip(" ,:")
    return html.escape(a[:1].upper() + a[1:], quote=False)


def couper(a: str, n: int) -> str:
    """Coupe à n caractères au dernier mot entier, sans finir sur « de », « et »…"""
    if len(a) <= n:
        return a
    mots = a[:n].rsplit(" ", 1)[0].split()
    while mots and mots[-1].lower().strip(",") in LIAISONS:
        mots.pop()
    return " ".join(mots).strip(" ,:")


BOILERPLATE = re.compile(
    r"^(INX\s+)?(le présent (marché|accord-cadre)( de travaux)?( à bons de commande)? a pour objet |la présente consultation concerne |"
    r"marché de travaux pour |accord-cadre .*? pour )?(la réalisation de l.opération de travaux relative à )?", re.I)


def lisible(objet: str) -> str | None:
    """Objet de marché présentable, ou None s'il est trop codé ou en majuscules."""
    o = html.unescape(objet).replace("\\n", " ").strip()
    o = re.sub(r"^[A-Z0-9]{2,8}\s*[-–:]\s*", "", o)
    o = BOILERPLATE.sub("", o)
    o = re.split(r"\s+[-–—]\s+|\s+-(?=\w)|\s*\(|\n", o)[0].strip(' ".«»')
    o = re.sub(r"^(la |le |les |l['’])(?=\w)", "", o, flags=re.I)
    o = couper(re.sub(r"\s+", " ", o), 72)
    lettres = [c for c in o if c.isalpha()]
    if len(o) < 20 or not lettres or sum(c.islower() for c in lettres) / len(lettres) < .75:
        return None
    if o.count("/") > 1 or re.search(r"\b[A-Z]{3,}\b.*\b[A-Z]{3,}\b", o):
        return None
    return html.escape(o[:1].upper() + o[1:], quote=False)


def semaine_ecoulee(today: dt.date) -> tuple:
    lundi = today - dt.timedelta(days=today.weekday() + 7)
    return lundi, lundi + dt.timedelta(days=5)


# --- Calculs -----------------------------------------------------------------
def bodacc(lundi: dt.date, samedi: dt.date) -> list:
    w = (f'dateparution>="{lundi}" and dateparution<="{samedi}" '
         'and familleavis in ("creation","vente")')
    return [P.normalize(r) for r in P.fetch(w)]


def volumes_html(rows: list, lundi, samedi) -> str:
    c = collections.Counter(r["secteur"] for r in rows)
    lignes = sorted(SECTEURS, key=lambda s: -c[s[0]])
    top = max(c[s[0]] for s in SECTEURS) or 1
    lis = "\n".join(
        f'      <li style="--w:{max(c[k] / top * 100, 1.5):.1f}%"><span class="who"><b>{titre}</b>'
        f'<span>{pour}</span></span><span class="track"><span class="fill"></span></span>'
        f'<span class="n">{nb(c[k])}</span></li>' for k, titre, pour in lignes)
    return (f'<ol class="bars" aria-label="Créations et reprises d\'entreprises par secteur, semaine du {jour(lundi)}">\n'
            f'{lis}\n    </ol>\n'
            f'    <p class="vol-note muted">Créations et reprises publiées au BODACC du {jour(lundi, False)} '
            f'au {jour(samedi)}, France entière. Vous ne recevez que vos secteurs et vos départements.</p>')


def hero_html(rows: list) -> str | None:
    resto = [r for r in rows if r["secteur"] == "restauration" and r["departement"] in HERO_DEPTS]
    par_jour = collections.defaultdict(list)
    for r in resto:
        par_jour[r["date_parution"]].append(r)
    # Le jour le plus fourni ; on n'affiche que des sociétés (pas de personnes physiques).
    for d, du_jour in sorted(par_jour.items(), key=lambda x: -len(x[1])):
        montrables = [r for r in du_jour if r["denomination"].strip() and not r["personne_physique"]
                      and r["ville"] and len(r["denomination"]) <= 28]
        if len(montrables) >= 5:
            break
    else:
        return None
    montrables.sort(key=lambda r: (r["type"] != "creation", r["departement"]))
    choix, villes = [], set()
    for r in montrables:  # d'abord des villes différentes
        if r["ville"] not in villes and len(choix) < 5:
            choix.append(r)
            villes.add(r["ville"])
    choix += [r for r in montrables if r not in choix][:5 - len(choix)]
    entries = "\n        ".join(
        f'<div class="entry"><span class="name"><span class="hl">{html.escape(html.unescape(r["denomination"]).strip(), quote=False)}</span></span>'
        f'<span class="act">{court(r["activite"])}</span><span class="place">{html.escape(r["ville"], quote=False)} '
        f'<span class="muted">{r["departement"]}</span><span class="kind">'
        f'{"création" if r["type"] == "creation" else "reprise"}</span></span></div>' for r in choix)
    n, d = len(du_jour), dt.date.fromisoformat(d)
    reste = n - len(choix)
    plus = (f"Et {reste} autre{'s' if reste > 1 else ''}, chacune avec son adresse, son numéro SIREN et le lien vers "
            "l'annonce officielle. Fichier CSV joint." if reste else
            "Chacune avec son adresse, son numéro SIREN et le lien vers l'annonce officielle. Fichier CSV joint.")
    return (f'<div class="mail-card">\n        <div class="mail-top">\n'
            f'          <div class="from"><span>Radar Entreprises</span><time>7:12</time></div>\n'
            f'          <div class="subject">{n} nouvelles entreprises en restauration ({", ".join(HERO_DEPTS)})</div>\n'
            f'        </div>\n        {entries}\n        <div class="mail-more">{plus}</div>\n      </div>\n'
            f'      <figcaption>Annonces réelles publiées au BODACC le {jour(d)}, telles que Radar les envoie '
            f'le lendemain matin.</figcaption>')


def marches(today: dt.date) -> tuple:
    rows = decp.collect((today - dt.timedelta(days=30)).isoformat())
    btp = [r for r in rows if r["famille"] == "Travaux BTP" and (r["montant"] or 0) > 0]
    total = (len(btp), sum(r["montant"] for r in btp))
    # Trois exemples lisibles en Nouvelle-Aquitaine, dans trois départements différents.
    ok = [dict(r, objet=lisible(r["objet"])) for r in btp
          if 15000 <= r["montant"] <= 5e6 and set(r["departements"]) & set(MARCHES_DEPTS)]
    ok = [r for r in ok if r["objet"]]
    ok.sort(key=lambda r: -r["montant"])
    ex, deps = [], set()
    for r in ok:
        dep = next(d for d in r["departements"] if d in MARCHES_DEPTS)
        if dep not in deps and dep in DEPT_NOMS:
            ex.append((r, dep))
            deps.add(dep)
    if len(ex) < 3:
        return total, None
    ex = [ex[0], ex[len(ex) // 2], ex[-1]]  # un gros, un moyen, un petit
    lst = "\n          ".join(
        f'<div class="mk"><span>{r["objet"]}</span>'
        f'<span class="amt">{nb(r["montant"])} €</span><span class="meta">{DEPT_NOMS[dep]}, notifié le '
        f'{jour(dt.date.fromisoformat(r["date_notification"][:10]), False)}</span></div>' for r, dep in ex)
    return total, f'<div class="mk-list" aria-label="Exemples récents de marchés attribués">\n          {lst}\n        </div>'


# --- Écriture ----------------------------------------------------------------
def marches_page(today: dt.date, page: str) -> tuple:
    """Page /marches/ : chiffres de la semaine écoulée (publication) et exemples enrichis."""
    lundi, _ = semaine_ecoulee(today)
    dimanche = lundi + dt.timedelta(days=6)
    rows = [r for r in M.collect_new((lundi - dt.timedelta(days=1)).isoformat())
            if r["date_publication"] <= dimanche.isoformat()]
    if len(rows) < 300:
        return page, None
    somme = sum(r["montant"] or 0 for r in rows)
    page = valeur(page, "m-semaine", f"Entre le {jour(lundi, False)} et le {jour(dimanche)}")
    page = valeur(page, "m-n", f"{nb(len(rows))} marchés")
    page = valeur(page, "m-somme", euros(somme))
    page = valeur(page, "m-st", nb(sum(r["sous_traitance"] for r in rows)))
    # Exemples : travaux en Nouvelle-Aquitaine, objet lisible, trois départements différents
    cand = [r for r in rows if r["type"] == "btp" and r["montant"] and 100000 <= r["montant"] <= 5e6
            and set(r["departements"]) & set(MARCHES_DEPTS) and lisible(r["objet"])]
    cand.sort(key=lambda r: -r["montant"])
    M.enrich(cand[:40])
    ex, deps = [], set()
    for r in cand[:40]:
        dep = next(d for d in r["departements"] if d in MARCHES_DEPTS)
        if r.get("titulaire_nom") and r.get("acheteur_nom") and dep not in deps:
            ex.append((r, dep))
            deps.add(dep)
    if len(ex) < 3:
        return page, f"{len(rows)} marchés (exemples inchangés)"
    ex = [ex[0], ex[len(ex) // 2], ex[-1]]
    e = lambda x: html.escape(x, quote=False)
    def ville(r, dep):
        v = r.get("titulaire_ville") or DEPT_NOMS.get(dep, dep)
        return f"{v} ({(r.get('titulaire_cp') or dep)[:2]})"
    def meta(r):
        bits = [f"Acheteur : {e(M.propre(r['acheteur_nom'], 60))}"]
        if r["duree_mois"]:
            bits.append(f"{r['duree_mois']} mois")
        if str(r["offres_recues"]).isdigit():
            n = int(r["offres_recues"])
            bits.append(f"{n} offre{'s' if n > 1 else ''} reçue{'s' if n > 1 else ''}")
        return " · ".join(bits)
    h, hdep = ex[0]
    panneau = f"""<div class="panel">
        <div class="owner"><small>Maître d'ouvrage</small><b>{e(h['acheteur_nom'])}</b></div>
        <div class="what">{e(M.propre(h['objet'], 110))}</div>
        <dl>
          <dt>Entreprise titulaire</dt><dd><span class="win hl">{e(h['titulaire_nom'])}</span><small>{e(h.get('titulaire_ville') or '')}{', ' + DEPT_NOMS.get(hdep, hdep) if h.get('titulaire_ville') else DEPT_NOMS.get(hdep, hdep)}</small></dd>
          <dt>Montant</dt><dd>{nb(h['montant'])}&nbsp;€ HT</dd>
          <dt>Durée</dt><dd>{h['duree_mois'] or '–'} mois</dd>
          <dt>Offres reçues</dt><dd>{h['offres_recues'] if str(h['offres_recues']).isdigit() else 'non publié'}</dd>
        </dl>
        <div class="date">Marché réel, notifié le {jour(dt.date.fromisoformat(h['date_notification']))}</div>
      </div>"""
    noms = sorted({DEPT_NOMS[d] for _, d in ex})
    cartes = "".join(f"""
        <div class="m">
          <div class="row"><span class="who-won">{e(r['titulaire_nom'])}</span><span class="amt">{nb(r['montant'])}&nbsp;€</span></div>
          <div class="place">{e(ville(r, d))}</div>
          <div class="obj">{e(M.propre(r['objet'], 120))}</div>
          <div class="meta">{meta(r)}</div>{'<span class="tag">Sous-traitance déclarée</span>' if r['sous_traitance'] else ''}
        </div>""" for r, d in sorted(ex, key=lambda x: -x[0]["montant"]))
    exemples = f"""<div class="mail-card">
        <div class="mail-top">
          <div class="from"><span>Radar Marchés attribués</span><time>mardi 7:12</time></div>
          <div class="subject">3 marchés attribués cette semaine — {', '.join(noms[:-1])} et {noms[-1]}</div>
        </div>{cartes}
        <div class="mail-more">Et la suite dans le fichier Excel joint.</div>
      </div>
      <figcaption>Marchés réels publiés la semaine du {jour(lundi)}, présentés comme dans l'alerte.</figcaption>"""
    page = zone(page, "m-panneau", panneau)
    page = zone(page, "m-exemples", exemples)
    return page, f"{len(rows)} marchés + exemples"


def zone(page: str, nom: str, contenu: str) -> str:
    rx = re.compile(rf"(<!-- auto:{nom} -->)(.*?)(\s*<!-- /auto:{nom} -->)", re.S)
    assert rx.search(page), f"zone {nom} absente"
    return rx.sub(lambda m: m.group(1) + "\n    " + contenu + m.group(3), page)


def valeur(page: str, nom: str, texte: str) -> str:
    rx = re.compile(rf'(<span data-auto="{nom}">)[^<]*(</span>)')
    assert rx.search(page), f"valeur {nom} absente"
    return rx.sub(lambda m: m.group(1) + texte + m.group(2), page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=dt.date.today().isoformat())
    today = dt.date.fromisoformat(ap.parse_args().today)
    page = PAGE.read_text(encoding="utf-8")
    lundi, samedi = semaine_ecoulee(today)
    changes = []

    try:
        rows = bodacc(lundi, samedi)
    except Exception as e:  # source indisponible : on garde la page telle quelle
        rows = []
        print(f"BODACC indisponible : {e}", file=sys.stderr)
    if len(rows) >= 3000:
        total = len(rows)
        resto = sum(r["secteur"] == "restauration" for r in rows)
        resto_zone = sum(r["secteur"] == "restauration" and r["departement"] in HERO_DEPTS for r in rows)
        page = zone(page, "volumes", volumes_html(rows, lundi, samedi))
        page = valeur(page, "semaine-total", f"Près de {nb(round(total, -3))}")
        page = valeur(page, "ratio-resto", str(round(total / max(resto, 1))))
        page = valeur(page, "faq-semaine", f"la semaine du {jour(lundi)}")
        page = valeur(page, "faq-resto", nb(resto))
        page = valeur(page, "faq-resto-zone", nb(resto_zone))
        changes.append(f"BODACC semaine du {lundi} : {total} annonces")
        hero = hero_html(rows)
        if hero:
            page = zone(page, "hero", hero)
            changes.append("exemple d'alerte")
    else:
        print(f"BODACC : seulement {len(rows)} annonces, volumes inchangés", file=sys.stderr)

    try:
        (n, somme), exemples = marches(today)
        if n >= 100:
            page = valeur(page, "marches-total", f"{nb(n)} marchés de travaux attribués")
            page = valeur(page, "marches-somme", euros(somme))
            changes.append(f"DECP : {n} marchés")
        if exemples:
            page = zone(page, "marches", exemples)
    except Exception as e:
        print(f"DECP indisponible : {e}", file=sys.stderr)

    PAGE.write_text(page, encoding="utf-8")

    try:
        pm, info = marches_page(today, PAGE_MARCHES.read_text(encoding="utf-8"))
        if info:
            PAGE_MARCHES.write_text(pm, encoding="utf-8")
            changes.append(f"page marchés : {info}")
    except Exception as e:
        print(f"Page marchés inchangée : {e}", file=sys.stderr)
    print("Mis à jour :", "; ".join(changes) or "rien")


if __name__ == "__main__":
    main()
