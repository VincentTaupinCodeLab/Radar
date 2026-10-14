"""Parcours de fin d'essai : rappel J-2, fin d'essai jamais sautée (week-end), relance après
l'essai, abonnés payants épargnés, email pré-rempli au paiement.

Lancement : python tests/test_essai.py (nécessite radar.db, comme test_parcours.py)
"""
import datetime as dt
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sender  # noqa: E402

OK = []


def check(cond, msg):
    assert cond, msg
    OK.append(msg)


tmp = Path(tempfile.mkdtemp())
sender.STATE_FIN_ESSAI = tmp / "fin.json"
sender.SUBS = tmp / "subs.json"
sender.LINKS = tmp / "stripe.json"
sender.LINKS.write_text(json.dumps({"plans": {"essentiel": {"url": "https://buy.stripe.com/test_ess"},
                                              "pro": {"url": "https://buy.stripe.com/test_pro"},
                                              "marches": {"url": "https://buy.stripe.com/test_mar"}}}))
db = tmp / "radar.db"
shutil.copy(ROOT / "radar.db", db)
sender.DB = db
con = sqlite3.connect(db)
(latest,) = con.execute("select max(date_parution) from annonces").fetchone()
(dept,) = con.execute("select departement from annonces where secteur='restauration' "
                      "group by departement order by count(*) desc limit 1").fetchone()
con.close()
derniere = dt.date.fromisoformat(latest)


class Box:
    dry = False

    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, tag, attachment=None):
        self.sent.append((to, subject, body, tag))


def essai(email, debut, fin, secteurs=("restauration", "commerce_alimentaire"), produit=None):
    s = {"email": email, "secteurs": list(secteurs), "departements": [dept], "source": "essai",
         "essai_debut": debut.isoformat(), "essai_fin": fin.isoformat(), "actif": True}
    if produit:
        s["produit"] = produit
    return s


def journee(jour, subs, payants=()):
    sender.TODAY = jour
    box = Box()
    sender.trial_lifecycle(subs, box, set(payants))
    return box.sent


fin = derniere + dt.timedelta(days=2)
subs = [essai("prospect@exemple.fr", fin - dt.timedelta(days=6), fin),
        essai("paye@exemple.fr", fin - dt.timedelta(days=6), fin),
        essai("btp@exemple.fr", fin - dt.timedelta(days=6), fin, ("marches_attribues",), "marches")]

# J-2 : rappel, avec le bilan chiffré de l'essai et les deux offres
s = journee(derniere, subs, payants={"paye@exemple.fr"})
r = [x for x in s if x[0] == "prospect@exemple.fr"]
check(len(r) == 1 and r[0][3] == "rappel_essai" and "se termine" in r[0][1], "J-2 : rappel de fin d'essai")
check("nouvelles entreprises ont été publiées" in r[0][2], "Rappel : bilan chiffré de l'essai")
check("test_ess" in r[0][2] and "test_pro" in r[0][2], "Rappel : offres Essentiel et Pro proposées")
check("prefilled_email=prospect%40exemple.fr" in r[0][2], "Paiement : email de l'essai pré-rempli")
check(not any(x[0] == "paye@exemple.fr" for x in s), "Déjà abonné : aucun rappel")
check(any(x[0] == "btp@exemple.fr" and "test_mar" in x[2] for x in s), "Rappel Marchés : offre à 39 €")
check(not journee(derniere + dt.timedelta(days=1), subs, payants={"paye@exemple.fr"}), "Rappel envoyé une seule fois")

# Fin d'essai un dimanche : rattrapée au premier envoi suivant (mardi)
dimanche = derniere + dt.timedelta(days=(6 - derniere.weekday()) % 7 or 7)
subs2 = [essai("dimanche@exemple.fr", dimanche - dt.timedelta(days=6), dimanche)]
mardi = dimanche + dt.timedelta(days=2)
s = journee(mardi, subs2)
check(len(s) == 1 and s[0][3] == "fin_essai" and "est terminé" in s[0][1],
      "Fin d'essai tombée le week-end : email envoyé au premier passage suivant")
check(not journee(mardi + dt.timedelta(days=1), subs2), "Fin d'essai envoyée une seule fois")

# Relance quelques jours après, avec ce qui a été publié depuis
fin3 = derniere - dt.timedelta(days=6)
subs3 = [essai("ancien@exemple.fr", fin3 - dt.timedelta(days=6), fin3)]
journee(fin3, subs3)  # fin d'essai
s = journee(derniere, subs3)
check(len(s) == 1 and s[0][3] == "relance_essai" and "depuis la fin de votre essai" in s[0][1],
      "Relance après l'essai : nombre d'entreprises publiées depuis")
check(not journee(derniere + dt.timedelta(days=1), subs3), "Relance envoyée une seule fois")

# Essai fini depuis longtemps : plus rien
subs4 = [essai("vieux@exemple.fr", derniere - dt.timedelta(days=30), derniere - dt.timedelta(days=23))]
check(not journee(derniere, subs4), "Essai fini depuis longtemps : aucun email")

print(f"\n{len(OK)} vérifications réussies :")
for m in OK:
    print("  ✓", m)
shutil.rmtree(tmp)
