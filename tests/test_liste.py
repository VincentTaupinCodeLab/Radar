"""Liste hebdomadaire gratuite (liste.py), sans réseau : Netlify et Brevo simulés.

Vérifie le double opt-in (seuls les confirmés reçoivent la liste), le jeton de confirmation,
l'envoi du lundi (10 entrées au plus, personnes morales uniquement, désinscription en un clic),
l'absence de doublon, la proposition d'essai après 3 envois, la désinscription (effacement chez
Netlify), l'expiration des demandes non confirmées et le stockage séparé, sans adresse email.

Lancement : python tests/test_liste.py
"""
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import donnees_test as D  # noqa: E402
from donnees_test import ROOT, TODAY, pages  # noqa: E402
import liste  # noqa: E402

OK = []


def check(cond, msg):
    assert cond, msg
    OK.append(msg)


tmp = Path(tempfile.mkdtemp())
opp = tmp / "opposition.txt"
opp.write_text(f"{D.SIREN_OPPOSITION}\n")
pages.OPPOSITION = opp
con = D.build(tmp / "radar.db")
subs_avant = (ROOT / "subscribers.json").read_text()


def inscription(sid, email, dept="33", produit="entreprises", consent="oui", jour=TODAY):
    return {"id": sid, "created_at": f"{jour.isoformat()}T10:00:00Z", "form_name": liste.FORM_INSCRIPTION,
            "data": {"email": email, "departement": dept, "produit": produit, "consentement": consent,
                     "page": f"/departements/x-{dept}/"}}


SUBS = {liste.FORM_INSCRIPTION: [
    inscription("a1", "Alice@Exemple.fr"),
    inscription("a2", "alice@exemple.fr"),                       # même liste : doublon
    inscription("c1", "carole@exemple.fr", consent=""),          # sans consentement : ignorée
    inscription("d1", "denis@exemple.fr", produit="marches"),
    inscription("e1", "pasunemail"),                             # adresse invalide
], liste.FORM_CONFIRMATION: [], liste.FORM_DESINSCRIPTION: []}
state = {}
sent, deleted = [], []


def envoyer(to, sujet, corps, headers):
    sent.append((to, sujet, corps, headers))


def jour(d, hebdo=False):
    sent.clear()
    return liste.traiter(SUBS, state, d, envoyer, deleted.append, con, hebdo)


# 1. Inscription : un email de confirmation, rien d'autre
jour(TODAY)
check(sorted(t for t, *_ in sent) == ["alice@exemple.fr", "denis@exemple.fr"], "Confirmation envoyée aux seules inscriptions valides")
corps = next(c for t, s, c, h in sent if t == "alice@exemple.fr")
check(liste.lien_confirmation("a1").replace("&", "&amp;") in corps and "Confirmez" in sent[0][1], "Email de confirmation avec lien")
check(all(r["source"] == "gratuit" for r in state["abonnes"].values()), "Inscrits stockés avec la source « gratuit »")
check("@" not in json.dumps(state), "Aucune adresse email dans l'état versionné (dépôt public)")
check(state["abonnes"][liste.cle("a2")]["statut"] == "doublon" and "a2" in deleted, "Doublon écarté et effacé")
jour(TODAY + dt.timedelta(days=1))
check(not sent, "Email de confirmation envoyé une seule fois")

# 2. Pas confirmé : pas de liste le lundi
lundi = TODAY + dt.timedelta(days=(7 - TODAY.weekday()) % 7)
jour(lundi, hebdo=True)
check(not sent, "Non confirmé : aucune liste")

# 3. Jeton faux puis bon
SUBS[liste.FORM_CONFIRMATION].append({"id": "k0", "data": {"inscription": "a1", "jeton": "0" * 24}})
jour(lundi, hebdo=True)
check(not sent and state["abonnes"][liste.cle("a1")]["statut"] == "en_attente", "Jeton invalide : inscription non confirmée")
SUBS[liste.FORM_CONFIRMATION].append({"id": "k1", "data": {"inscription": "a1", "jeton": liste.jeton("a1")}})
SUBS[liste.FORM_CONFIRMATION].append({"id": "k2", "data": {"inscription": "d1", "jeton": liste.jeton("d1")}})
jour(lundi)
check(not sent and state["abonnes"][liste.cle("a1")]["statut"] == "confirme", "Confirmation enregistrée (hors lundi : rien envoyé)")

# 4. Le lundi : la liste réduite
jour(lundi, hebdo=True)
a = [x for x in sent if x[0] == "alice@exemple.fr"]
check(len(a) == 1 and "Gironde" in a[0][1], "Lundi : liste envoyée au confirmé, pour son département")
html_a = a[0][2]
check(html_a.count("<tr>") <= liste.TAILLE and "SOCIETE TEST" in html_a, "Liste réduite : 10 entrées au plus")
check(not any(x in html_a for x in D.INTERDITS), "Liste : ni personne physique ni opposition")
check(liste.lien_desinscription("a1") in html_a and "un clic" in html_a, "Lien de désinscription en un clic")
check("List-Unsubscribe" in a[0][3] and liste.lien_desinscription("a1") in a[0][3]["List-Unsubscribe"], "En-tête List-Unsubscribe")
check("alerte complète 7 jours" not in html_a, "Pas d'offre d'essai avant 3 envois")
d = [x for x in sent if x[0] == "denis@exemple.fr"]
check(len(d) == 1 and "marchés" in d[0][1] and "COLAS FRANCE" in d[0][2] and "PAUL MARTINEAU" not in d[0][2],
      "Liste marchés : titulaires personnes morales")
jour(lundi, hebdo=True)
check(not sent, "Même semaine : pas de second envoi")

# 5. Après 3 envois, l'essai gratuit est proposé (données de test figées à la date de la base)
contenu_reel = liste.contenu
liste.contenu = lambda c, produit, dept, today: contenu_reel(c, produit, dept, lundi)
for k in (1, 2, 3):
    jour(lundi + dt.timedelta(days=7 * k), hebdo=True)
    a = [x for x in sent if x[0] == "alice@exemple.fr"][0][2]
    check(("alerte complète 7 jours" in a) == (k >= 3), f"Envoi n°{k + 1} : offre d'essai {'présente' if k >= 3 else 'absente'}")
check(state["abonnes"][liste.cle("a1")]["envois"] == 4, "Nombre d'envois suivi")

# 6. Désinscription en un clic
deleted.clear()
SUBS[liste.FORM_DESINSCRIPTION].append({"id": "u1", "data": {"inscription": "d1"}})
jour(lundi + dt.timedelta(days=28), hebdo=True)
check(not any(x[0] == "denis@exemple.fr" for x in sent), "Désinscrit : plus aucun envoi")
check(state["abonnes"][liste.cle("d1")]["statut"] == "desinscrit" and {"d1", "u1"} <= set(deleted),
      "Désinscription : adresse effacée chez Netlify")
check(any(x[0] == "alice@exemple.fr" for x in sent), "Les autres inscrits continuent de recevoir la liste")

# 7. Demande jamais confirmée : effacée après 30 jours
SUBS[liste.FORM_INSCRIPTION].append(inscription("v1", "vieux@exemple.fr", jour=TODAY - dt.timedelta(days=40)))
state["abonnes"][liste.cle("v1")] = {"source": "gratuit", "produit": "entreprises", "departement": "33",
                                     "inscrit_le": (TODAY - dt.timedelta(days=40)).isoformat(),
                                     "statut": "en_attente", "envois": 0, "confirmation_envoyee": "x"}
jour(TODAY)
check(state["abonnes"][liste.cle("v1")]["statut"] == "expire" and "v1" in deleted, "Non confirmée sous 30 jours : effacée")

# 8. Séparation des fichiers
check((ROOT / "subscribers.json").read_text() == subs_avant, "subscribers.json (essais) inchangé")
check(liste.STATE.name == "liste_gratuite.json", "Inscrits gratuits dans leur propre fichier")

print(f"\n{len(OK)} vérifications réussies.")
shutil.rmtree(tmp)
