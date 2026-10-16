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
SIGNATURE = "Vincent Taupin\nRadar Entreprises"
STOP = "Pas concerné ? Répondez « stop » et je ne vous écrirai plus."

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
    # Personnes morales uniquement : aucun entrepreneur individuel cité nommément (RGPD).
    ex = con.execute(
        f"SELECT coalesce(nullif(enseigne,''), denomination) nom, ville, activite FROM annonces "
        f"WHERE {where} AND personne_physique = 0 AND coalesce(nullif(enseigne,''), denomination) != '' "
        f"ORDER BY date_parution DESC LIMIT 60", args).fetchall()
    # Exemples cités : uniquement ceux dont l'activité est clairement de la restauration
    # ou de l'alimentaire (le classement automatique a quelques erreurs).
    vus, uniques = set(), []
    for e in ex:  # un même établissement peut paraître deux fois (création + immatriculation)
        cle = (e[0].strip().lower(), (e[1] or "").strip().lower())
        if cle not in vus and FOOD.search(activite_courte(e[2])):
            vus.add(cle)
            uniques.append(e)
    ex = uniques[:6]
    return n, ex


FOOD = __import__("re").compile(
    r"restau|\\bbar\\b|caf[ée]|boulang|p[âa]tiss|boucher|charcut|traiteur|cr[êe]p|pizz|alimentation|[ée]picerie|"
    r"snack|brasserie|salon de th[ée]|coquillage|glac|fromag|primeur|poissonn|caviste|kebab|burger|sushi|fournil", __import__("re").I)


def titlecase(s: str) -> str:
    return s if not s.isupper() else s.title()


def ville_courte(v: str) -> str:
    return (v or "").replace(" Arrondissement", "").strip()


def activite_courte(a: str) -> str:
    import re
    a = (a or "").strip(" -–.;:")
    a = re.split(r"[,;.(]| et ", a)[0].strip(" -–")
    for _ in range(3):
        a = re.sub(r"^(la |le |les |l'|toute |tout )?(exploitation (d'un|de tout|du) fonds de commerce (de |d')|fonds de commerce (de |d')|activit[ée]s? (de |: )?|vente en magasin (de |d')?)", "", a, flags=re.I).strip()
    a = a[:1].lower() + a[1:]
    a = a if len(a) <= 45 else a[:45].rsplit(" ", 1)[0]
    a = re.sub(r"^(la |le |l')", "", a, flags=re.I)
    while True:  # pas de phrase coupée sur une préposition
        b = re.sub(r"\s*(\b(à|a|de|du|des|sur|ou|et|en|sous|toutes|ses|type)\b|[-/–])\s*$", "", a, flags=re.I)
        if b == a:
            return a
        a = b


def drafts(p: dict, con) -> list:
    """Séquence courte, une seule question par message, aucun lien dans le premier
    (meilleure délivrabilité), preuve concrète dans la relance."""
    dept = p.get("departement") or ""
    local = dept in DEPTS
    n, ex = stats(con, dept if local else None)
    if local and n < 5:  # trop peu d'exemples locaux : on passe au national
        local = False
        n, ex = stats(con, None)
    zone = DEPTS[dept] if local else "en France"
    zone_liste = DEPTS[dept] if local else "pour votre zone"
    meme_ville = local and len({ville_courte(v) for _, v, _ in ex}) == 1
    noms = [titlecase(nom) if meme_ville else f"{titlecase(nom)} ({ville_courte(v)})" for nom, v, _ in ex[:3]]
    exemples = ", ".join(noms)
    besoin = BESOIN.get(p["categorie"], "choisit ses fournisseurs")

    e1_subject = f"{n} ouvertures de restaurants et commerces de bouche {zone}"
    e1 = f"""Bonjour,

La semaine dernière, {n} restaurants, bars et commerces de bouche ont ouvert ou changé de propriétaire {zone}{' : ' + exemples + '…' if exemples else '.'}

Chacun {besoin} dans ses premières semaines, souvent avant que vos concurrents ne le sachent.

Je publie chaque matin la liste de ces nouveaux établissements, avec adresse et numéro SIREN, à partir du BODACC officiel.

Voulez-vous que je vous envoie celle de demain {zone_liste}, gratuitement, pour juger sur pièce ?

{SIGNATURE}

{STOP}"""

    choix = ex[3:6] if len(ex) >= 6 else ex[:3]
    detail = "\n".join(f"- {titlecase(nom)}, {activite_courte(a) or 'établissement'}, {ville_courte(v)}"
                       for nom, v, a in choix)
    combien = {1: "un établissement publié", 2: "deux établissements publiés"}.get(len(choix), "trois établissements publiés")
    r1_subject = "Re: " + e1_subject
    r1 = f"""Bonjour,

Pour que ce soit concret, voici {combien} récemment {zone} :

{detail}

{'Il figure' if len(choix) == 1 else 'Ils figurent'} dans l'alerte avec leur adresse, leur SIREN et le lien vers l'annonce officielle. Vous en recevriez environ {n} par semaine {zone}.

Je vous envoie la liste de demain ? Un simple « oui » suffit.

{SIGNATURE}

{STOP}"""

    r2_subject = "Re: " + e1_subject
    r2 = f"""Bonjour,

Je ne vais pas insister. Une dernière question : si ce n'est pas vous qui gérez la prospection des nouveaux clients, à qui puis-je m'adresser ?

Sinon, l'essai de 7 jours reste ouvert sur {SITE}, sans carte bancaire.

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
