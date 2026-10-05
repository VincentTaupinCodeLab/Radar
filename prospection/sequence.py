"""Génère la séquence de prospection personnalisée (email 1 + 2 relances).

Chaque email 1 cite de vraies entreprises créées ou reprises la semaine passée
dans le département du prospect (ou en France pour les acteurs nationaux).

Entrée : prospects.csv (liste validée)  ·  Base : ../radar.db
Sortie : brouillons.csv + apercu.html (pour relecture avant tout envoi)
"""
import csv
import html
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT.parent / "radar.db"
SITE = "https://radar-entreprises.fr"
SIGNATURE = "Vincent Taupin\nRadar Entreprises · radar-entreprises.fr"
STOP = "Si ce sujet ne vous concerne pas, répondez simplement « stop » : je ne vous recontacterai pas."

# Ce que l'établissement qui ouvre achète au prospect, selon sa catégorie.
BESOIN = {
    "boissons": "choisit son fournisseur de boissons",
    "cafe": "choisit son café et sa machine",
    "caisse": "s'équipe d'une caisse et d'un terminal de paiement",
    "cuisine_pro": "équipe sa cuisine",
    "comptable": "cherche un expert-comptable",
    "assurance": "doit souscrire sa multirisque professionnelle",
    "emballage": "commande ses emballages de vente à emporter",
    "web": "a besoin d'un site et d'une présence en ligne",
}

DEPTS = {  # libellés pour l'objet du mail
    "75": "à Paris", "13": "dans les Bouches-du-Rhône", "69": "dans le Rhône", "33": "en Gironde",
    "31": "en Haute-Garonne", "44": "en Loire-Atlantique", "34": "dans l'Hérault", "59": "dans le Nord",
    "35": "en Ille-et-Vilaine", "16": "en Charente", "17": "en Charente-Maritime", "64": "dans les Pyrénées-Atlantiques",
    "63": "dans le Puy-de-Dôme", "73": "en Savoie", "85": "en Vendée", "29": "dans le Finistère",
    "56": "dans le Morbihan", "68": "dans le Haut-Rhin", "42": "dans la Loire", "26": "dans la Drôme",
    "92": "dans les Hauts-de-Seine", "94": "dans le Val-de-Marne", "91": "dans l'Essonne", "25": "dans le Doubs",
    "58": "dans la Nièvre", "28": "en Eure-et-Loir", "86": "dans la Vienne", "24": "en Dordogne",
    "40": "dans les Landes", "87": "en Haute-Vienne", "79": "dans les Deux-Sèvres", "49": "en Maine-et-Loire", "47": "en Lot-et-Garonne",
}
SECTEURS = ("restauration", "commerce_alimentaire")


def stats(con, dept: str | None):
    where = "secteur IN (?,?) AND date_parution >= date('now','-7 days')"
    args = list(SECTEURS)
    if dept:
        where += " AND departement = ?"
        args.append(dept)
    (n,) = con.execute(f"SELECT count(*) FROM annonces WHERE {where}", args).fetchone()
    ex = con.execute(
        f"SELECT coalesce(nullif(enseigne,''), denomination) nom, ville FROM annonces "
        f"WHERE {where} AND personne_physique = 0 AND coalesce(nullif(enseigne,''), denomination) != '' "
        f"ORDER BY date_parution DESC LIMIT 3", args).fetchall()
    return n, ex


def titlecase(s: str) -> str:
    return s if not s.isupper() else s.title()


def drafts(p: dict, con) -> list:
    dept = p.get("departement") or ""
    local = dept in DEPTS
    n, ex = stats(con, dept if local else None)
    if local and n < 5:  # trop peu d'exemples locaux : on passe au national
        local = False
        n, ex = stats(con, None)
    zone = DEPTS[dept] if local else "en France"
    noms = [f"{titlecase(nom)} ({ville.replace(' Arrondissement', '')})" for nom, ville in ex]
    exemples = ", ".join(noms[:-1]) + " et " + noms[-1] if len(noms) > 1 else "".join(noms)
    besoin = BESOIN.get(p["categorie"], "choisit ses fournisseurs")

    e1_subject = f"{n} restaurants et commerces de bouche créés ou repris {zone} en 7 jours"
    e1 = f"""Bonjour,

La semaine dernière, {n} restaurants, bars et commerces de bouche ont été créés ou repris {zone}{', dont ' + exemples if exemples else ''}.

Un établissement qui ouvre {besoin} dans ses premières semaines. Radar Entreprises vous envoie chaque matin la liste de ceux de votre zone : nom, activité, adresse et numéro SIREN, à partir des publications officielles du BODACC. Vous les contactez avant vos concurrents.

Je vous propose 7 jours d'essai gratuit, sans carte bancaire : répondez-moi simplement avec les départements que vous couvrez, et vous recevez la première liste demain matin.

Un aperçu de l'alerte : {SITE}

{SIGNATURE}

{STOP}"""

    r1_subject = "Re: " + e1_subject
    r1 = f"""Bonjour,

Je me permets de revenir vers vous. Chaque matin, Radar Entreprises repère en moyenne {round(n / 5)} nouveaux restaurants et commerces de bouche {zone}.

Si vous voulez juger sur pièce, je peux vous envoyer la liste de demain, sans engagement. Il suffit de me répondre « oui ».

{SIGNATURE}

{STOP}"""

    r2_subject = "Re: " + e1_subject
    r2 = f"""Bonjour,

Dernier message de ma part sur ce sujet : si la prospection des nouveaux établissements n'est pas une priorité pour vous en ce moment, je ne vous relancerai plus.

Si c'est le cas plus tard, l'essai de 7 jours reste ouvert sur {SITE}.

Bonne continuation,

{SIGNATURE}"""
    return [(1, e1_subject, e1), (2, r1_subject, r1), (3, r2_subject, r2)], zone, n


def main():
    con = sqlite3.connect(DB)
    prospects = list(csv.DictReader(open(ROOT / "prospects.csv", encoding="utf-8")))
    out, preview = [], []
    for p in prospects:
        seq, zone, n = drafts(p, con)
        for etape, subj, body in seq:
            out.append({"email": p["email"], "nom": p["nom"], "categorie": p["categorie"],
                        "zone": zone, "etape": etape, "objet": subj, "corps": body})
        preview.append((p, seq))

    with open(ROOT / "brouillons.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    blocks = []
    for p, seq in preview:
        mails = "".join(
            f"<h3>{'Email 1' if e == 1 else 'Relance ' + str(e - 1)} · {html.escape(s)}</h3>"
            f"<pre>{html.escape(b)}</pre>" for e, s, b in seq)
        blocks.append(f"<section><h2>{html.escape(p['nom'][:70])}</h2>"
                      f"<p class=m>{p['email']} · {p['categorie']} · {p['site']}</p>{mails}</section>")
    (ROOT / "apercu.html").write_text(f"""<!doctype html><meta charset=utf-8>
<title>Aperçu prospection</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px}}
section{{border-top:2px solid #14213D;padding:12px 0 24px}} h2{{font-size:18px;margin:6px 0}}
h3{{font-size:14px;margin:16px 0 4px;color:#14213D}} .m{{color:#666;font-size:13px;margin:0}}
pre{{white-space:pre-wrap;font:inherit;background:#f4f6f9;padding:12px;border-radius:6px;margin:0}}</style>
<h1>Séquence de prospection : {len(prospects)} prospects</h1>{''.join(blocks)}""", encoding="utf-8")
    print(f"{len(prospects)} prospects, {len(out)} brouillons")


if __name__ == "__main__":
    main()
