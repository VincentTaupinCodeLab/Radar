"""Tests du parcours abonné, sans réseau : Stripe, Netlify et Brevo sont simulés.

Lancement : python3 tests/test_parcours.py
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import billing  # noqa: E402
import decp  # noqa: E402
import sender  # noqa: E402

OK = []


def check(cond, msg):
    assert cond, msg
    OK.append(msg)


def session(dept_text, secteur=None, autres=None, email="client@exemple.fr"):
    f = [{"key": "departements", "type": "text", "text": {"value": dept_text}}]
    if secteur:
        f.append({"key": "secteur", "type": "dropdown", "dropdown": {"value": secteur}})
    if autres is not None:
        f.append({"key": "autres", "type": "text", "text": {"value": autres}})
    return {"customer_details": {"email": email}, "custom_fields": f}


# 1. Lecture des champs saisis au paiement
s = billing.subscriber_from_session(session("16, 17 et 33, 86", "restauration"), "essentiel", "active")
check(s["departements"] == ["16", "17", "33"], "Essentiel : départements limités à 3")
check(s["secteurs"] == ["restauration"], "Essentiel : secteur du menu déroulant")
s = billing.subscriber_from_session(session("Gironde 33 / 2A / 971", "btp", "restauration et beauté"), "pro", "active")
check(s["departements"] == ["33", "2A", "971"], "Pro : Corse et outre-mer reconnus")
check(s["secteurs"] == ["btp", "restauration", "beaute_bien_etre"], "Pro : secteurs supplémentaires interprétés")
s = billing.subscriber_from_session(session("69"), "marches", "active")
check(s["secteurs"] == ["marches_attribues"], "Marchés : bon type d'alerte")
check(billing.subscriber_from_session(session("aucun"), "essentiel", "active") is None,
      "Saisie sans département valide ignorée")

# 2. Départements d'un marché public
check(decp.depts_of({"lieuexecution_code": "16100", "lieuexecution_typecode": "Code postal"}) == {"16"},
      "Marché localisé par code postal")
check("33" in decp.depts_of({"lieuexecution_code": "75", "lieuexecution_typecode": "Code région"}),
      "Marché régional rattaché à ses départements")

# 3. Parcours complet dans un dossier temporaire
tmp = Path(tempfile.mkdtemp())
for name in ("STATE", "STATE_MARCHES", "STATE_FIN_ESSAI"):
    setattr(sender, name, tmp / "state" / Path(getattr(sender, name)).name)
sender.SUBS = tmp / "subscribers.json"
sender.LINKS = tmp / "stripe.json"
sender.OUT = tmp / "out"
sender.LINKS.write_text(json.dumps({"plans": {"essentiel": {"url": "https://buy.stripe.com/test_ess"},
                                              "pro": {"url": "https://buy.stripe.com/test_pro"},
                                              "marches": {"url": "https://buy.stripe.com/test_mar"}},
                                    "portail": "https://billing.stripe.com/p/login/test"}))
db = tmp / "radar.db"
shutil.copy(ROOT / "radar.db", db)
sender.DB = db
con = sqlite3.connect(db)
(latest,) = con.execute("select max(date_parution) from annonces").fetchone()
con.close()
today = dt.date.fromisoformat(latest) + dt.timedelta(days=1)

sender.SUBS.write_text(json.dumps([
    {"email": "essai.encours@exemple.fr", "secteurs": ["restauration"], "departements": ["13", "69", "75"],
     "source": "essai", "essai_debut": (today - dt.timedelta(days=3)).isoformat(),
     "essai_fin": (today + dt.timedelta(days=3)).isoformat(), "actif": True},
    {"email": "essai.dernierjour@exemple.fr", "secteurs": ["restauration", "btp"], "departements": ["33"],
     "source": "essai", "essai_debut": (today - dt.timedelta(days=6)).isoformat(),
     "essai_fin": today.isoformat(), "actif": True},
    {"email": "essai.fini@exemple.fr", "secteurs": ["restauration"], "departements": ["75"],
     "source": "essai", "essai_debut": (today - dt.timedelta(days=10)).isoformat(),
     "essai_fin": (today - dt.timedelta(days=4)).isoformat(), "actif": True},
    {"email": "client@exemple.fr", "secteurs": ["restauration"], "departements": ["75"],
     "source": "essai", "essai_debut": (today - dt.timedelta(days=9)).isoformat(),
     "essai_fin": (today - dt.timedelta(days=3)).isoformat(), "actif": True},
]))

# Netlify : une nouvelle inscription à l'essai
netlify = [{"form_name": "essai", "data": {"email": "Nouveau@Exemple.fr", "entreprise": "Brasserie X",
                                           "secteurs": ["restauration", "marches_attribues"],
                                           "departements": "16, 17"}}]


class FakeResp:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self, *a): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): pass


real_urlopen = sender.urllib.request.urlopen
sender.urllib.request.urlopen = lambda req, timeout=0: FakeResp(netlify)
os.environ.update(NETLIFY_TOKEN="x", NETLIFY_SITE_ID="y", STRIPE_SECRET_KEY="sk_test_x",
                  BREVO_API_KEY="x", SENDER_EMAIL="contact@radar-entreprises.fr")
billing.paying_subscribers = lambda: [
    billing.subscriber_from_session(session("75", "restauration"), "essentiel", "active"),
    billing.subscriber_from_session(session("16, 33", email="marches@exemple.fr"), "marches", "active"),
]
sent = []
atts = []
sender.send = lambda to, subject, body, attachment=None: (sent.append((to, subject, body)), atts.append(attachment)) and 201
decp.collect = lambda since: [
    {"date_notification": "2026-10-01", "famille": "Travaux BTP", "objet": "Réfection toiture école",
     "montant": 120000, "titulaire_fiche": "https://x", "departements": ["16"]},
    {"date_notification": "2026-10-01", "famille": "Travaux BTP", "objet": "Voirie",
     "montant": 50000, "titulaire_fiche": "https://x", "departements": ["69"]},
]

# un mardi, pour l'alerte hebdomadaire des marchés
tuesday = today + dt.timedelta(days=(1 - today.weekday()) % 7)
sender.TODAY = today
os.environ["FORCE_MARCHES"] = "1"
sender.main()

to = [t for t, _, _ in sent]
subjects = {t: [s for x, s, _ in sent if x == t] for t in set(to)}
check("nouveau@exemple.fr" in to and any("commence" in s for s in subjects["nouveau@exemple.fr"]),
      "Inscription Netlify importée et email de bienvenue envoyé")
saved = json.loads(sender.SUBS.read_text())
check(any(s["email"] == "nouveau@exemple.fr" and s["essai_fin"] == (today + dt.timedelta(days=6)).isoformat()
          and "nouveau" not in s for s in saved), "Nouvel essai enregistré pour 7 jours")
check(any("se termine" in s for s in subjects.get("essai.dernierjour@exemple.fr", [])),
      "Email de fin d'essai le dernier jour")
fin_body = [b for t, s, b in sent if t == "essai.dernierjour@exemple.fr" and "termine" in s][0]
check("test_pro" in fin_body, "Fin d'essai : lien vers l'offre adaptée (2 secteurs → Pro)")
check("essai.fini@exemple.fr" not in to, "Essai expiré sans abonnement : plus aucun envoi")
check("client@exemple.fr" in to, "Essai expiré mais abonné payant : envois maintenus")
client_body = [b for t, s, b in sent if t == "client@exemple.fr"][0]
check("billing.stripe.com" in client_body, "Abonné payant : lien Gérer ou résilier dans l'email")
check("essai.encours@exemple.fr" in to, "Essai en cours : alertes envoyées")
m = [s for t, s, _ in sent if t == "marches@exemple.fr"]
check(m == ["1 marchés publics attribués (16, 33)"], "Marchés : seulement ceux des départements de l'abonné")
check(any(t == "nouveau@exemple.fr" and "marchés" in s for t, s, _ in sent), "Essai : alerte marchés incluse")

csvs = [a for a in atts if a]
check(csvs and csvs[0][0].endswith(".csv") and b"siren" in csvs[0][1], "Alerte quotidienne : fichier CSV joint (Excel)")

# 4. Rejouer le même jour : aucun doublon
n = len(sent)
sender.main()
check(len(sent) == n, "Relance du même jour : aucun email en double")
sender.urllib.request.urlopen = real_urlopen

print(f"\n{len(OK)} vérifications réussies :")
for m in OK:
    print("  ✓", m)
shutil.rmtree(tmp)
