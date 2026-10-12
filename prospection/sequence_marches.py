"""Séquence de prospection Radar Marchés attribués (email 1 + 2 relances).

Chaque email cite de vrais marchés publics de travaux attribués ces 30 derniers jours dans
le département du prospect : entreprise titulaire, objet, montant (DECP + Annuaire des
entreprises). Même mécanique que sequence.py : court, sans lien, une seule question.

Entrée : prospects_marches.csv  ·  Sortie : brouillons_marches.csv
"""
import csv
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))
import marches  # noqa: E402

SITE = "https://radar-entreprises.fr/marches/"
SIGNATURE = "Vincent Taupin\nRadar Entreprises"
STOP = "Pas concerné ? Répondez « stop » et je ne vous écrirai plus."

BESOIN = {
    "negoce": "commander ses matériaux",
    "location": "louer du matériel",
    "echafaudage": "trouver des échafaudages et des sous-traitants",
    "second_oeuvre": "s'approvisionner et sous-traiter le second œuvre",
}
CIBLE = {
    "negoce": "c'est le bon moment pour lui proposer vos matériaux",
    "location": "c'est le bon moment pour lui proposer votre parc",
    "echafaudage": "c'est le bon moment pour vous présenter",
    "second_oeuvre": "c'est le bon moment pour vous présenter",
}


def dans(dept: str) -> str:
    from sequence import DEPTS
    return DEPTS.get(dept) or f"dans votre département ({marches.DEPT_NOMS.get(dept, dept)})"


def montant_court(x: float) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f} M€".replace(".", ",").replace(",0 M€", " M€")
    return f"{round(x / 1000):,} k€".replace(",", " ")


def bon_objet(o: str) -> bool:
    """Objet compréhensible tel quel : assez long, sans référence de lot ni code interne."""
    import re
    o = marches.propre(o, 200)
    return (len(o) >= 25 and not re.match(r"(?i)lot\b|lot\s*n?°?\s*\d|accord[- ]cadre|march[ée] (à|a) bons", o)
            and not re.search(r"\d{4}\s*/\s*\d|[A-Z]{2,}\d|\blot\s*\d+\s*[-:]", o)
            and sum(c.isupper() for c in o) < len(o) * 0.3)


def nom_court(n: str) -> str:
    import re
    n = re.sub(r"\s*\([^)]*\)", "", n).strip()
    if n.isupper():
        n = marches.titre(n)
    n = n if len(n) <= 45 else n[:45].rsplit(" ", 1)[0]
    while n.split(" ")[-1].lower() in marches.PETITS and " " in n:
        n = n.rsplit(" ", 1)[0]
    return n


ABREV = [(r"\btvx\b", "travaux"), (r"\btrx\b", "travaux"), (r"\baep\b", "AEP"), (r"\bst\b", "Saint"),
         (r"\brd\b", "RD"), (r"\bmen\b", "menuiserie"), (r"\bamgt\b", "aménagement")]


def objet_court(o: str) -> str:
    import re
    for a, b in ABREV:
        o = re.sub(a, b, o, flags=re.I)
    o = marches.propre(o, 75)
    return o[:1].lower() + o[1:] if o[1:2].islower() else o


def main():
    prospects = list(csv.DictReader(open(ROOT / "prospects_marches.csv", encoding="utf-8")))
    depuis = (dt.date.today() - dt.timedelta(days=31)).isoformat()
    rows = [r for r in marches.collect_new(depuis)
            if r["type"] == "btp" and r["montant"] and r["montant"] >= 15000 and r["titulaire_siret"]]
    print(f"{len(rows)} marchés de travaux attribués depuis le {depuis}")
    par_dept = {}
    for r in rows:
        for d in r["departements"]:
            par_dept.setdefault(d, []).append(r)

    # Exemples : marchés « parlants » (50 k€ à 3 M€), titulaire identifié.
    besoin = set()
    for p in prospects:
        lst = sorted(par_dept.get(p["departement"], []), key=lambda r: -(r["montant"] or 0))
        mid = [r for r in lst if 50000 <= r["montant"] <= 3_000_000] or lst
        besoin.update(id(r) for r in [r for r in mid if bon_objet(r["objet"])][:10])
    exemples = [r for r in rows if id(r) in besoin]
    marches.enrich(exemples)

    out = []
    for p in prospects:
        d = p["departement"]
        lst = par_dept.get(d, [])
        if len(lst) < 3:
            continue  # trop peu de marchés pour un message crédible
        n, total = len(lst), sum(r["montant"] or 0 for r in lst)
        ex = [r for r in sorted(lst, key=lambda r: -(r["montant"] or 0))
              if r.get("titulaire_nom") and 50000 <= r["montant"] <= 3_000_000 and bon_objet(r["objet"])][:4]
        if not ex:
            continue
        zone = dans(d)
        cat = p["categorie"] if p["categorie"] in BESOIN else "negoce"
        a = ex[0]
        e1_subject = f"{n} marchés de travaux attribués {zone} ce mois-ci"
        e1 = f"""Bonjour,

Ces 30 derniers jours, {n} marchés publics de travaux ont été attribués {zone}, pour {montant_court(total)} au total. Par exemple, {nom_court(a['titulaire_nom'])} vient de remporter « {objet_court(a['objet'])} » ({montant_court(a['montant'])}).

Chaque entreprise retenue va {BESOIN[cat]} dans les semaines qui suivent : {CIBLE[cat]}, avant vos concurrents.

Je publie chaque semaine la liste de ces marchés attribués, avec l'entreprise titulaire, l'acheteur et le montant, à partir des données officielles de la commande publique.

Voulez-vous recevoir celle de cette semaine pour votre département, gratuitement, pour juger sur pièce ?

{SIGNATURE}

{STOP}"""
        detail = "\n".join(f"- {nom_court(r['titulaire_nom'])} : {objet_court(r['objet'])} ({montant_court(r['montant'])})"
                           for r in (ex[1:4] if len(ex) > 1 else ex))
        r1 = f"""Bonjour,

Pour que ce soit concret, voici d'autres marchés attribués récemment {zone} :

{detail}

Dans la liste hebdomadaire, chaque marché est accompagné de la ville de l'entreprise, de l'acheteur et du lien vers sa fiche officielle. Vous pouvez la filtrer par type de travaux et par montant minimum.

Je vous envoie celle de cette semaine ? Un simple « oui » suffit.

{SIGNATURE}

{STOP}"""
        r2 = f"""Bonjour,

Je ne vais pas insister. Une dernière question : si ce n'est pas vous qui gérez le développement commercial, à qui puis-je m'adresser ?

Sinon, l'essai de 7 jours reste ouvert sur {SITE}, sans carte bancaire.

Bonne continuation,

{SIGNATURE}"""
        for etape, subj, body in ((1, e1_subject, e1), (2, "Re: " + e1_subject, r1), (3, "Re: " + e1_subject, r2)):
            out.append({"email": p["email"], "nom": p["nom"], "categorie": cat, "zone": zone,
                        "etape": etape, "objet": subj, "corps": body})
    with open(ROOT / "brouillons_marches.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["email", "nom", "categorie", "zone", "etape", "objet", "corps"])
        w.writeheader()
        w.writerows(out)
    print(f"{len(prospects)} prospects, {len(out) // 3} séquences prêtes")


if __name__ == "__main__":
    main()
