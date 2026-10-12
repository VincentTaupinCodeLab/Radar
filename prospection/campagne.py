"""Pilotage de la campagne de prospection (envoi depuis Gmail, contact@radar-entreprises.fr).

État dans etat.json (une entrée par prospect, clé = email) :
    statut      a_envoyer | en_cours | reponse | interesse | essai | stop | erreur | termine
    etapes      {"1": "AAAA-MM-JJ", "2": ..., "3": ...}  dates d'envoi
    thread_id   fil Gmail (les relances partent dans le même fil)
    objet       objet réellement envoyé à l'étape 1
    notes       texte libre

Commandes :
    python campagne.py plan [--date AAAA-MM-JJ]       actions du jour (JSON)
    python campagne.py envoye EMAIL ETAPE THREAD_ID   enregistre un envoi
    python campagne.py statut EMAIL STATUT [NOTE]     met à jour un prospect
    python campagne.py essai EMAIL "16 17" [marches]  ouvre un essai de 7 jours
    python campagne.py bilan                          compteurs
"""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ETAT = ROOT / "etat.json"

# Montée en charge progressive d'une boîte neuve : nouveaux premiers emails par jour.
RAMPE = [10, 15, 20, 25]
MAX_ENVOIS_JOUR = 30            # tous emails confondus (premiers + relances)
RELANCE_1 = 3                   # jours ouvrés après l'email 1
RELANCE_2 = 7                   # jours ouvrés après l'email 1
ACTIFS = {"a_envoyer", "en_cours"}


def ouvres_entre(a: dt.date, b: dt.date) -> int:
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def charger():
    # Deux campagnes, une seule file d'envoi : Radar Entreprises puis Radar Marchés attribués.
    prospects, brouillons = [], {}
    for suffixe, produit in (("", "entreprises"), ("_marches", "marches")):
        f = ROOT / f"prospects{suffixe}.csv"
        if not f.exists():
            continue
        prospects += [dict(p, produit=produit) for p in csv.DictReader(open(f, encoding="utf-8"))]
        b_f = ROOT / f"brouillons{suffixe}.csv"
        if b_f.exists():
            for b in csv.DictReader(open(b_f, encoding="utf-8")):
                brouillons[(b["email"], b["etape"])] = b
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if ETAT.exists() else {}
    for p in prospects:
        etat.setdefault(p["email"], {"statut": "a_envoyer", "etapes": {}, "thread_id": "",
                                     "objet": "", "notes": ""})
        etat[p["email"]]["produit"] = p["produit"]
    return prospects, brouillons, etat


def sauver(etat):
    ETAT.write_text(json.dumps(etat, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def plan(jour: dt.date) -> list:
    prospects, brouillons, etat = charger()
    if jour.weekday() >= 5:
        return []
    actions = []
    # 1. Relances dues, dans le fil d'origine
    for email, e in etat.items():
        if e["statut"] != "en_cours" or "1" not in e["etapes"]:
            continue
        j1 = dt.date.fromisoformat(e["etapes"]["1"])
        ecart = ouvres_entre(j1, jour)
        for etape, seuil in (("2", RELANCE_1), ("3", RELANCE_2)):
            if etape in e["etapes"] or ecart < seuil:
                continue
            if etape == "3" and "2" not in e["etapes"]:
                continue
            b = brouillons.get((email, etape))
            if b:
                actions.append({"email": email, "etape": int(etape), "thread_id": e["thread_id"],
                                "objet": "Re: " + (e["objet"] or b["objet"].removeprefix("Re: ")),
                                "corps": b["corps"]})
            break
    # 2. Nouveaux premiers emails, selon la montée en charge
    jours_envoi = sorted({d for e in etat.values() for d in [e["etapes"].get("1")] if d and d < jour.isoformat()})
    quota = RAMPE[min(len(jours_envoi), len(RAMPE) - 1)]
    deja = sum(1 for e in etat.values() if e["etapes"].get("1") == jour.isoformat())
    domaines_pris = {email.split("@")[1] for email, e in etat.items() if e["statut"] != "a_envoyer"}
    place = max(0, min(quota - deja, MAX_ENVOIS_JOUR - len(actions)))
    for p in prospects:
        if place <= 0:
            break
        e = etat[p["email"]]
        dom = p["email"].split("@")[1]
        if e["statut"] != "a_envoyer" or dom in domaines_pris:
            continue
        b = brouillons.get((p["email"], "1"))
        if not b:
            continue
        actions.append({"email": p["email"], "etape": 1, "thread_id": "", "objet": b["objet"], "corps": b["corps"]})
        domaines_pris.add(dom)
        place -= 1
    return actions


def envoye(email: str, etape: str, thread_id: str, jour: dt.date):
    _, brouillons, etat = charger()
    e = etat[email]
    e["etapes"][etape] = jour.isoformat()
    if etape == "1":
        e["thread_id"] = thread_id
        e["objet"] = brouillons[(email, "1")]["objet"]
        e["statut"] = "en_cours"
    if etape == "3":
        e["statut"] = "termine"
    sauver(etat)


def statut(email: str, s: str, note: str = ""):
    _, _, etat = charger()
    if email not in etat:
        etat[email] = {"statut": s, "etapes": {}, "thread_id": "", "objet": "", "notes": ""}
    etat[email]["statut"] = s
    if note:
        etat[email]["notes"] = (etat[email]["notes"] + " | " if etat[email]["notes"] else "") + note
    sauver(etat)


def essai(email: str, depts: list, jour: dt.date, entreprise: str = "", marches: bool = False):
    """Ouvre un essai de 7 jours : le prochain passage quotidien envoie le mail de
    bienvenue et la première alerte."""
    subs_path = ROOT.parent / "subscribers.json"
    subs = json.loads(subs_path.read_text(encoding="utf-8"))
    if any(s.get("email", "").lower() == email.lower() for s in subs):
        print("déjà inscrit")
        return
    entry = {"email": email, "entreprise": entreprise,
             "secteurs": ["marches_attribues"] if marches else ["restauration", "commerce_alimentaire"],
             "departements": depts[:15 if marches else 3], "source": "essai", "origine": "prospection",
             "essai_debut": jour.isoformat(),
             "essai_fin": (jour + dt.timedelta(days=6)).isoformat(), "nouveau": True, "actif": True}
    if marches:
        entry.update({"produit": "marches", "types": [], "montant_min": 0, "premiere_marches": True})
    subs.append(entry)
    subs_path.write_text(json.dumps(subs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    statut(email, "essai", f"essai ouvert le {jour.isoformat()} ({', '.join(depts[:3])})")


def bilan():
    _, _, etat = charger()
    c = {}
    for e in etat.values():
        c[e["statut"]] = c.get(e["statut"], 0) + 1
    envoyes = sum(1 for e in etat.values() if "1" in e["etapes"])
    print(json.dumps({"prospects": len(etat), "contactes": envoyes, **c}, ensure_ascii=False))


if __name__ == "__main__":
    args = sys.argv[1:]
    today = dt.date.today()
    if "--date" in args:
        i = args.index("--date")
        today = dt.date.fromisoformat(args[i + 1])
        del args[i:i + 2]
    cmd = args[0] if args else "bilan"
    if cmd == "plan":
        print(json.dumps(plan(today), ensure_ascii=False, indent=1))
    elif cmd == "envoye":
        envoye(args[1], args[2], args[3] if len(args) > 3 else "", today)
    elif cmd == "essai":  # essai EMAIL "16 17" [marches]
        _, _, et = charger()
        est_marches = (len(args) > 3 and args[3] == "marches") or et.get(args[1], {}).get("produit") == "marches"
        essai(args[1], args[2].split(), today, marches=est_marches)
    elif cmd == "statut":
        statut(args[1], args[2], " ".join(args[3:]))
    else:
        bilan()
