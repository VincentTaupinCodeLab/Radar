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
import marches  # noqa: E402
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

# 2 bis. Radar Marchés attribués : champs Stripe, collecte, filtres
def sess_m(depts, types, montant, email="marches@exemple.fr"):
    f = session(depts, email=email)
    f["custom_fields"] += [{"key": "types", "type": "dropdown", "dropdown": {"value": types}},
                           {"key": "montant", "type": "dropdown", "dropdown": {"value": montant}}]
    return f


s = billing.subscriber_from_session(sess_m("16, 33", "btp", "m40000"), "marches", "active")
check(s["types"] == ["btp"] and s["montant_min"] == 40000, "Marchés : type et montant minimum lus au paiement")
s = billing.subscriber_from_session(sess_m("69", "tous", "m0"), "marches", "active")
check(s["types"] == [] and s["montant_min"] == 0, "Marchés : « tous » = aucun filtre")
check(len(billing.custom_fields("marches")) == 3, "Marchés : 3 champs au paiement (limite Stripe)")


def rec(i, cpv, dept, montant, pub, notif="2026-09-20", siret="12345678900011", modif="CDL", st="non"):
    return {"id": str(i), "acheteur_id": "21160015300014", "codecpv": cpv, "objet": f"OBJET {i} TRAVAUX DE TOITURE",
            "montant": montant, "datenotification": notif, "datepublicationdonnees": pub,
            "lieuexecution_code": dept, "lieuexecution_typecode": "Code département",
            "titulaire_id_1": siret, "idmodification": modif, "soustraitancedeclaree": st, "dureemois": 6}


RAW = [rec(1, "45261000-4", "16", 120000, "2026-10-05", st="oui"),
       rec(1, "45261000-4", "16", 120000, "2026-10-05"),                  # doublon (co-titulaire)
       rec(2, "45233140-2", "69", 50000, "2026-10-05"),                   # hors zone
       rec(3, "79620000-6", "33", 25000, "2026-10-05"),                   # services
       rec(4, "45000000-7", "33", 15000, "2026-10-05"),                   # BTP, petit montant
       rec(5, "45000000-7", "16", 90000, "2026-10-05", modif="AV1"),      # avenant
       rec(6, "45000000-7", "17", 90000, "2026-10-05", notif="2024-01-10"),  # trop ancien
       rec(7, "71000000-8", "17", 1, "2026-10-04")]                       # montant non précisé
rows = marches.collect_new("2026-10-01", records=RAW)
check(sorted(r["id"] for r in rows) == ["1", "2", "3", "4", "7"],
      "Collecte : doublons, avenants et marchés trop anciens écartés")
check(next(r for r in rows if r["id"] == "7")["montant"] is None, "Montant à 1 € traité comme non précisé")
sel = marches.for_subscriber(rows, {"departements": ["16", "33"], "types": ["btp"], "montant_min": 40000})
check([r["id"] for r in sel] == ["1"], "Filtres département + type + montant minimum")
check(marches.propre("INX MS010 - TRAVAUX DE RÉFECTION DE LA TOITURE") == "Travaux de réfection de la toiture",
      "Objet nettoyé (code interne, majuscules)")


# 3. Parcours complet dans un dossier temporaire
tmp = Path(tempfile.mkdtemp())
for name in ("STATE", "STATE_MARCHES", "STATE_MARCHES_PUB", "STATE_FIN_ESSAI"):
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
                                           "departements": "16, 17"}},
           {"form_name": "essai-marches", "data": {"email": "loueur@exemple.fr", "entreprise": "Loc'Matériel",
                                                   "departements": "16, 17, 33, 86, 79",
                                                   "types": ["btp", "entretien"], "montant_min": "40000"}}]


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
pub = (today - dt.timedelta(days=2)).isoformat()
WEEK = [rec(10, "45261000-4", "16", 120000, pub, notif=pub, st="oui"),
        rec(11, "45233140-2", "69", 50000, pub, notif=pub),
        rec(12, "77310000-6", "79", 60000, pub, notif=pub, siret="98765432100019"),
        rec(13, "79620000-6", "33", 25000, pub, notif=pub)]
fetches = []
decp.fetch = lambda where: (fetches.append(where), WEEK)[1]
lookups = []
marches.DB = db
marches.lookup = lambda siret: (lookups.append(siret), {"trouve": 1, "nom": f"ENTREPRISE {siret[:4]}",
                                                         "ville": "Angoulême", "cp": "16000", "naf": "43.91A",
                                                         "effectif": "10-19 salariés"})[1]
marches.time.sleep = lambda x: None

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
m = [(s, b) for t, s, b in sent if t == "marches@exemple.fr"]
check(len(m) == 1 and m[0][0].startswith("2 marchés attribués cette semaine — Charente et Gironde"),
      "Marchés : seulement ceux des départements de l'abonné")
check("ENTREPRISE 1234" in m[0][1] and "Angoulême" in m[0][1], "Marchés : nom et ville du titulaire dans l'email")
check("Sous-traitance déclarée" in m[0][1], "Marchés : sous-traitance déclarée signalée")
mi = sent.index(next(x for x in sent if x[0] == "marches@exemple.fr"))
check(atts[mi] and atts[mi][0].startswith("marches_attribues_") and b"titulaire_nom" in atts[mi][1],
      "Marchés : fichier CSV joint avec les titulaires")
check(len(set(lookups)) == len(lookups), "Enrichissement : chaque SIRET cherché une seule fois")
check(any(t == "nouveau@exemple.fr" and "marché" in s for t, s, _ in sent), "Essai : alerte marchés incluse")
lou = [(s, b) for t, s, b in sent if t == "loueur@exemple.fr"]
check(any("Radar Marchés attribués commence" in s for s, _ in lou), "Essai Marchés : email de bienvenue dédié")
alert = [s for s, _ in lou if "attribué" in s and "essai" not in s.lower()]
check(alert and alert[0].startswith("2 marchés"), "Essai Marchés : première alerte immédiate, filtres appliqués (BTP + entretien, ≥ 40 000 €)")
saved = json.loads(sender.SUBS.read_text())
check(not any(x.get("premiere_marches") for x in saved), "Première alerte marquée comme envoyée")

csvs = [a for a in atts if a]
check(csvs and csvs[0][0].endswith(".csv") and b"siren" in csvs[0][1], "Alerte quotidienne : fichier CSV joint (Excel)")

# 4. Rejouer le même jour : aucun doublon
n = len(sent)
sender.main()
check(len(sent) == n, "Relance du même jour : aucun email en double")

# 5. Fin d'essai Marchés : lien vers l'offre à 39 €
fin_m = sender.end_of_trial_email({"email": "x@y.fr", "produit": "marches", "secteurs": ["marches_attribues"],
                                   "departements": ["16"], "essai_fin": today.isoformat()})
check("test_mar" in fin_m and "39" in fin_m, "Fin d'essai Marchés : lien vers l'offre Marchés")
sender.urllib.request.urlopen = real_urlopen

print(f"\n{len(OK)} vérifications réussies :")
for m in OK:
    print("  ✓", m)
shutil.rmtree(tmp)
