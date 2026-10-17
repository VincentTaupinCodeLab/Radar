"""Liste hebdomadaire gratuite (formulaire « liste-gratuite » des pages départements).

Parcours :
1. L'internaute s'inscrit sur une page /departements/... ou /marches/... (email, département,
   produit « entreprises » ou « marches »). Netlify enregistre la soumission.
2. Double opt-in : liste.py envoie un email de confirmation avec un lien
   /liste/confirmer.html?id=<soumission>&t=<jeton>. Le bouton de cette page envoie le formulaire
   Netlify « liste-confirmation ». Seules les inscriptions confirmées (jeton valide) reçoivent la liste.
3. Chaque lundi, une version réduite : 10 entrées du département choisi, par Brevo (sender.send),
   avec un lien de désinscription en un clic (/liste/desinscription.html?id=..., formulaire
   « liste-desinscription ») et l'en-tête List-Unsubscribe. À partir du 4e envoi (après 3 listes),
   l'email propose l'essai gratuit de 7 jours de l'alerte complète.

Stockage : les adresses restent dans Netlify (sous-traitant déjà déclaré) ; le dépôt étant public,
state/liste_gratuite.json ne contient aucune adresse, seulement une empreinte de la soumission,
la source « gratuit », le produit, le département, le statut et le nombre d'envois. Ces inscrits
sont distincts des abonnés payants (Stripe) et des essais (subscribers.json). Une désinscription
supprime les soumissions de l'adresse chez Netlify ; une inscription non confirmée sous 30 jours aussi.

Variables d'environnement : NETLIFY_TOKEN, NETLIFY_SITE_ID, BREVO_API_KEY, SENDER_EMAIL,
LISTE_SECRET (facultatif, clé des jetons ; à défaut NETLIFY_TOKEN), DRY_RUN=1, FORCE_LISTE=1
(envoie la liste même hors lundi), TODAY.
"""
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pages

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "liste_gratuite.json"
SITE = pages.SITE
FORM_INSCRIPTION = "liste-gratuite"
FORM_CONFIRMATION = "liste-confirmation"
FORM_DESINSCRIPTION = "liste-desinscription"
SOURCE = "gratuit"
PRODUITS = ("entreprises", "marches")
TAILLE = 10
ENVOIS_AVANT_ESSAI = 3
EXPIRATION_JOURS = 30
EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


# --- Identifiants et jetons --------------------------------------------------------------
def cle(sub_id: str) -> str:
    """Empreinte stockée dans le dépôt public (l'identifiant lui-même sert de lien privé)."""
    return hashlib.sha256(sub_id.encode()).hexdigest()[:16]


def _secret() -> bytes:
    return (os.environ.get("LISTE_SECRET") or os.environ.get("NETLIFY_TOKEN") or "radar-liste").encode()


def jeton(sub_id: str) -> str:
    return hmac.new(_secret(), f"confirmer:{sub_id}".encode(), hashlib.sha256).hexdigest()[:24]


def lien_confirmation(sub_id: str) -> str:
    return f"{SITE}/liste/confirmer.html?" + urllib.parse.urlencode({"id": sub_id, "t": jeton(sub_id)})


def lien_desinscription(sub_id: str) -> str:
    return f"{SITE}/liste/desinscription.html?" + urllib.parse.urlencode({"id": sub_id})


def semaine(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# --- Netlify -------------------------------------------------------------------------------
class Netlify:
    API = "https://api.netlify.com/api/v1"

    def __init__(self, token: str, site: str):
        self.token, self.site = token, site

    def _req(self, method: str, path: str):
        req = urllib.request.Request(self.API + path, method=method,
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        return json.loads(body) if body else None

    def submissions(self, names: tuple) -> dict:
        forms = {f["name"]: f["id"] for f in self._req("GET", f"/sites/{self.site}/forms") or []}
        out = {}
        for name in names:
            out[name] = []
            if name not in forms:
                continue
            page = 1
            while True:
                chunk = self._req("GET", f"/forms/{forms[name]}/submissions?per_page=100&page={page}") or []
                out[name] += chunk
                if len(chunk) < 100:
                    break
                page += 1
        return out

    def delete(self, sub_id: str):
        self._req("DELETE", f"/submissions/{sub_id}")


# --- Contenu des emails ----------------------------------------------------------------------
INK, MUTED, RULE, FLUO = "#1D2125", "#5E656C", "#E3E1DA", "#FFE34A"


def _email(titre: str, corps: str, pied: str = "") -> str:
    return (f"<!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'></head><body style='margin:0;background:#F6F4EE'>"
            f"<div style=\"max-width:620px;margin:0 auto;padding:24px 16px;font:15px/1.55 -apple-system,"
            f"'Segoe UI',Roboto,Arial,sans-serif;color:{INK}\"><div style='background:#fff;border:1px solid {RULE};"
            f"border-radius:8px;padding:24px'><div style='font-size:12px;font-weight:800;letter-spacing:.08em;"
            f"text-transform:uppercase;color:{MUTED}'>Radar Entreprises · liste gratuite</div>"
            f"<h1 style='font-size:21px;line-height:1.25;margin:8px 0 12px'>{html.escape(titre)}</h1>{corps}</div>"
            f"<p style='font-size:12px;color:{MUTED};margin:14px 4px'>{pied}Radar Entreprises · "
            f"<a href='{SITE}' style='color:{MUTED}'>radar-entreprises.fr</a> · contact@radar-entreprises.fr</p>"
            f"</div></body></html>")


def _bouton(url: str, label: str) -> str:
    return (f"<p style='margin:20px 0'><a href='{html.escape(url)}' style='display:inline-block;background:{INK};"
            f"color:#fff;padding:13px 20px;border-radius:5px;text-decoration:none;font-weight:700'>{html.escape(label)}</a></p>")


def email_confirmation(sub_id: str, produit: str, dept: str) -> tuple:
    quoi = "10 marchés publics attribués" if produit == "marches" else "10 nouvelles entreprises"
    corps = (f"<p>Bonjour,</p><p>Vous avez demandé à recevoir chaque lundi la liste gratuite de Radar : {quoi} "
             f"{html.escape(pages.DANS[dept])}. Pour la recevoir, confirmez votre adresse :</p>"
             + _bouton(lien_confirmation(sub_id), "Confirmer mon inscription")
             + f"<p style='color:{MUTED};font-size:13px'>Si vous n'êtes pas à l'origine de cette demande, ignorez cet "
             "email : sans confirmation, vous ne recevrez rien et la demande sera effacée sous 30 jours.</p>")
    return "Confirmez votre inscription à la liste gratuite Radar", _email("Confirmez votre inscription", corps)


def contenu(con, produit: str, dept: str, today: dt.date) -> list:
    """Les 10 entrées de la semaine pour un département (personnes morales uniquement)."""
    exclus = pages.oppositions()
    if produit == "marches":
        rows = pages.marches_recents(con, today - dt.timedelta(days=365))
        rows = [r for r in rows if dept in r["deps"] and len(r["deps"]) <= 3 and pages.montrable(r, exclus)]
        return rows[:TAILLE]
    rows = [r for r in pages.annonces(con, today - dt.timedelta(days=pages.JOURS_SEMAINE - 1), today, exclus)
            if r["departement"] == dept]
    # sociétés des secteurs suivis d'abord, les plus récentes en tête
    suivis = sorted([r for r in rows if pages.groupe_of(r) is not pages.AUTRES], key=lambda r: r["date_parution"], reverse=True)
    autres = sorted([r for r in rows if pages.groupe_of(r) is pages.AUTRES], key=lambda r: r["date_parution"], reverse=True)
    return (suivis + autres)[:TAILLE]


def _lignes(rows: list, produit: str) -> str:
    out = []
    for r in rows:
        if produit == "marches":
            titre = html.escape(html.unescape(r["t_nom"]))
            sous = html.escape(f"{pages.M.propre(r['objet'] or '', 140)}")
            meta = html.escape(f"{pages.M.titre(pages.M.nom_propre(r['a_nom']))} · {pages.M.euros(r['montant'])} · notifié le {pages.date_courte(r['date_notification'])}")
            lien = ""
        else:
            titre = html.escape(html.unescape(r["denomination"]).strip())
            sous = pages.activite_courte(r)
            kind = "Création" if r["type"] == "creation" else "Reprise"
            meta = html.escape(f"{pages.ville_propre(r['ville'])} · {kind} · {pages.groupe_of(r).nom}")
            lien = (f" · <a href='{html.escape(r['url'])}' style='color:{INK}'>annonce</a>"
                    if (r["url"] or "").startswith("https://") else "")
        out.append(f"<tr><td style='padding:12px 0;border-top:1px solid {RULE}'><div style='font-weight:700'>{titre}</div>"
                   f"<div style='font-size:14px'>{sous}</div><div style='font-size:13px;color:{MUTED}'>{meta}{lien}</div></td></tr>")
    return f"<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>{''.join(out)}</table>"


def email_liste(sub_id: str, produit: str, dept: str, rows: list, envois_passes: int, today: dt.date) -> tuple:
    zone = pages.DANS[dept]
    if produit == "marches":
        sujet = f"{len(rows)} marchés publics attribués {zone} cette semaine"
        page, essai = SITE + pages.url_marches(dept), f"{SITE}/marches/#essai"
        complet = ("L'alerte complète Radar Marchés attribués : tous les marchés de vos départements chaque mardi, "
                   "filtrés par type et montant, avec le fichier Excel des titulaires.")
    else:
        sujet = f"{len(rows)} nouvelles entreprises {zone} cette semaine"
        page, essai = SITE + pages.url_dept(dept), f"{SITE}/#essai"
        complet = ("L'alerte complète Radar Entreprises : chaque matin, toutes les créations et reprises de vos secteurs "
                   "et départements, avec SIREN, adresse et fichier Excel.")
    offre = ""
    if envois_passes >= ENVOIS_AVANT_ESSAI:
        offre = (f"<div style='background:{FLUO};border-radius:6px;padding:16px 18px;margin:18px 0'>"
                 f"<b>Vous suivez cette liste depuis {envois_passes} semaines.</b><br>{complet} "
                 "Essai gratuit de 7 jours, sans carte bancaire."
                 + _bouton(essai, "Essayer l'alerte complète 7 jours") + "</div>")
    corps = (f"<p>Voici {len(rows)} entrées de la semaine {html.escape(zone)}, d'après les données publiques "
             f"({'DECP' if produit == 'marches' else 'BODACC'}).</p>" + offre + _lignes(rows, produit)
             + f"<p style='margin-top:18px'><a href='{page}' style='color:{INK};font-weight:700'>Voir la page du département</a></p>")
    pied = (f"Vous recevez cette liste gratuite chaque lundi car vous l'avez demandée et confirmée. "
            f"<a href='{lien_desinscription(sub_id)}' style='color:{MUTED}'>Se désinscrire en un clic</a>. "
            "Seules des personnes morales figurent dans cette liste ; toute personne citée peut s'opposer à la diffusion "
            "de ses données en écrivant à contact@radar-entreprises.fr.<br>")
    return sujet, _email(f"La liste de la semaine {zone}", corps, pied)


# --- Traitement ------------------------------------------------------------------------------
def inscription_valide(sub: dict) -> dict | None:
    d = sub.get("data") or {}
    email = (d.get("email") or sub.get("email") or "").strip().lower()
    dept = str(d.get("departement") or "").strip().upper()
    produit = str(d.get("produit") or "entreprises").strip()
    if not EMAIL_RX.match(email) or dept not in pages.NOMS or not d.get("consentement"):
        return None
    return {"id": sub["id"], "email": email, "departement": dept,
            "produit": produit if produit in PRODUITS else "entreprises",
            "date": (sub.get("created_at") or "")[:10]}


def traiter(subs: dict, state: dict, today: dt.date, envoyer, supprimer, con=None, hebdo: bool = False) -> dict:
    """subs : soumissions par formulaire ; envoyer(to, sujet, html, headers) ; supprimer(id).
    Met à jour state (dict) et renvoie un résumé."""
    ab = state.setdefault("abonnes", {})
    stats = {"confirmations": 0, "listes": 0, "desinscriptions": 0, "expirations": 0}
    inscrits = {}
    for s in subs.get(FORM_INSCRIPTION, []):
        i = inscription_valide(s)
        if i:
            inscrits[i["id"]] = i
    confirmes = {str((s.get("data") or {}).get("inscription") or ""): str((s.get("data") or {}).get("jeton") or "")
                 for s in subs.get(FORM_CONFIRMATION, [])}
    desinscriptions = {}
    for s in subs.get(FORM_DESINSCRIPTION, []):
        desinscriptions.setdefault(str((s.get("data") or {}).get("inscription") or ""), []).append(s["id"])

    # Désinscription : toutes les inscriptions de la même adresse, puis effacement chez Netlify
    emails_partis = {inscrits[i]["email"] for i in desinscriptions if i in inscrits}
    for i, info in list(inscrits.items()):
        if info["email"] in emails_partis:
            rec = ab.setdefault(cle(i), {"source": SOURCE, "produit": info["produit"],
                                         "departement": info["departement"], "inscrit_le": info["date"], "envois": 0})
            if rec.get("statut") != "desinscrit":
                rec.update(statut="desinscrit", desinscrit_le=today.isoformat())
                stats["desinscriptions"] += 1
            supprimer(i)
            del inscrits[i]
    for sids in desinscriptions.values():
        for sid in sids:
            supprimer(sid)

    vus = set()
    ordre = sorted(inscrits.values(), key=lambda x: (x["date"], x["id"]))
    for info in ordre:
        k = cle(info["id"])
        rec = ab.setdefault(k, {"source": SOURCE, "produit": info["produit"], "departement": info["departement"],
                                "inscrit_le": info["date"] or today.isoformat(), "statut": "en_attente", "envois": 0})
        if rec["statut"] in ("desinscrit", "expire", "doublon"):
            supprimer(info["id"])
            continue
        triplet = (info["email"], info["produit"], info["departement"])
        if triplet in vus and rec["statut"] == "en_attente":
            rec["statut"] = "doublon"  # déjà inscrit à la même liste
            supprimer(info["id"])
            continue
        vus.add(triplet)
        t = confirmes.get(info["id"])
        if rec["statut"] == "en_attente" and t is not None and hmac.compare_digest(t, jeton(info["id"])):
            rec.update(statut="confirme", confirme_le=today.isoformat())
        if rec["statut"] == "en_attente":
            if (today - dt.date.fromisoformat(rec["inscrit_le"])).days > EXPIRATION_JOURS:
                rec["statut"] = "expire"
                stats["expirations"] += 1
                supprimer(info["id"])
                continue
            if not rec.get("confirmation_envoyee"):
                sujet, corps = email_confirmation(info["id"], info["produit"], info["departement"])
                envoyer(info["email"], sujet, corps, None)
                rec["confirmation_envoyee"] = today.isoformat()
                stats["confirmations"] += 1
        if rec["statut"] == "confirme" and hebdo and con is not None and rec.get("derniere_semaine") != semaine(today):
            rows = contenu(con, info["produit"], info["departement"], today)
            if not rows:
                continue  # rien à montrer cette semaine : pas d'email vide
            sujet, corps = email_liste(info["id"], info["produit"], info["departement"], rows, rec["envois"], today)
            envoyer(info["email"], sujet, corps, {
                "List-Unsubscribe": f"<{lien_desinscription(info['id'])}>, "
                                    "<mailto:contact@radar-entreprises.fr?subject=desinscription>"})
            rec["envois"] += 1
            rec["derniere_semaine"] = semaine(today)
            stats["listes"] += 1
    # Inscriptions effacées chez Netlify (à la main) : plus d'envoi possible
    presents = {cle(i) for i in inscrits}
    for k, rec in ab.items():
        if rec.get("statut") in ("en_attente", "confirme") and k not in presents:
            rec["statut"] = "supprime"
    return stats


def main():
    today = dt.date.fromisoformat(os.environ.get("TODAY", dt.date.today().isoformat()))
    token, site = os.environ.get("NETLIFY_TOKEN"), os.environ.get("NETLIFY_SITE_ID")
    if not (token and site):
        print("::warning::NETLIFY_TOKEN ou NETLIFY_SITE_ID absent : liste gratuite non traitée")
        return 0
    dry = os.environ.get("DRY_RUN") == "1" or not os.environ.get("BREVO_API_KEY")
    api = Netlify(token, site)
    subs = api.submissions((FORM_INSCRIPTION, FORM_CONFIRMATION, FORM_DESINSCRIPTION))
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = dt.datetime.now(dt.timezone.utc)
    hebdo = (today.weekday() == 0 and now.hour >= 6) or os.environ.get("FORCE_LISTE") == "1"

    def envoyer(to, sujet, corps, headers):
        if dry:
            print(f"[dry-run] {to.split('@')[0][:2]}… · {sujet}")
            return
        import sender
        sender.send(to, sujet, corps, headers=headers)
        print(f"envoyé · {sujet}")

    def supprimer(sub_id):
        if dry:
            return
        try:
            api.delete(sub_id)
        except Exception as ex:
            print(f"Suppression Netlify impossible ({ex})", file=sys.stderr)

    con = sqlite3.connect(pages.P.DB) if pages.P.DB.exists() else None
    stats = traiter(subs, state, today, envoyer, supprimer, con, hebdo)
    if not dry:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    actifs = sum(r.get("statut") == "confirme" for r in state.get("abonnes", {}).values())
    print(f"Liste gratuite : {actifs} inscrit(s) confirmé(s) ; " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
