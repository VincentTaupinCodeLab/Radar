"""Envoi des alertes aux abonnés.

Qui reçoit quoi :
- Abonnés payants : lus chaque matin dans Stripe (billing.py). Une résiliation les
  retire automatiquement.
- Essais gratuits : subscribers.json (source « essai », 7 jours). Les inscriptions du
  formulaire du site y sont importées automatiquement si NETLIFY_TOKEN est défini.
  Deux jours avant la fin, un rappel ; le dernier jour (ou au premier envoi qui suit), la fin
  d'essai ; quelques jours après, une relance avec ce que l'essai aurait apporté depuis.
- Entrées de test : subscribers.json sans source (toujours actives).

Alertes :
- BODACC (secteurs) : chaque matin, uniquement les nouvelles parutions.
- Marchés attribués (marches.py) : une fois par semaine (le mardi), tous les marchés publiés
  depuis l'envoi précédent, filtrés et enrichis par abonné, avec le CSV joint. Un nouvel
  essai reçoit sa première alerte immédiatement (7 derniers jours).

Variables d'environnement :
    BREVO_API_KEY, SENDER_EMAIL, SENDER_NAME   envoi des emails
    STRIPE_SECRET_KEY                          abonnés payants (facultatif)
    NETLIFY_TOKEN, NETLIFY_SITE_ID             import des inscriptions à l'essai (facultatif)
    DRY_RUN=1                                  n'envoie rien, écrit des aperçus dans out/
    TEST_TO=adresse                            envoie un seul email d'exemple
    FORCE_MARCHES=1                            envoie l'alerte marchés même hors mardi
"""
import base64
import csv
import datetime as dt
import html
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pipeline import DB, OUT, email_html

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "last_parution.txt"
STATE_MARCHES = ROOT / "state" / "last_marches.txt"
STATE_MARCHES_PUB = ROOT / "state" / "last_marches_pub.txt"
STATE_FIN_ESSAI = ROOT / "state" / "fin_essai_envoyes.json"
SUBS = ROOT / "subscribers.json"
LINKS = ROOT / "site" / "stripe.json"
SITE = "https://radar-entreprises.fr"
ESSAI_JOURS = 7
TODAY = dt.date.fromisoformat(os.environ.get("TODAY", dt.date.today().isoformat()))


# --- Envoi ------------------------------------------------------------------
def send(to: str, subject: str, body: str, attachment: tuple | None = None):
    payload = {
        "sender": {"email": os.environ["SENDER_EMAIL"],
                   "name": os.environ.get("SENDER_NAME", "Radar Entreprises")},
        "to": [{"email": to}],
        "replyTo": {"email": "contact@radar-entreprises.fr"},
        "subject": subject,
        "htmlContent": body,
    }
    if attachment:
        name, content = attachment
        payload["attachment"] = [{"name": name, "content": base64.b64encode(content).decode()}]
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode(),
        headers={"api-key": os.environ["BREVO_API_KEY"],
                 "content-type": "application/json",
                 "accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        key = os.environ.get("BREVO_API_KEY", "")
        print(f"Brevo HTTP {e.code} : {detail}", file=sys.stderr)
        print(f"Clé reçue : préfixe '{key[:8]}', longueur {len(key)} "
              "(une clé API commence par 'xkeysib-', une clé SMTP par 'xsmtpsib-')",
              file=sys.stderr)
        raise


class Outbox:
    def __init__(self, dry: bool):
        self.dry, self.count = dry, 0

    def __call__(self, to: str, subject: str, body: str, tag: str, attachment=None):
        if self.dry:
            OUT.mkdir(exist_ok=True)
            (OUT / f"preview_{to.split('@')[0]}_{tag}.html").write_text(body, encoding="utf-8")
            print(f"[dry-run] {to} · {subject}")
        else:
            status = send(to, subject, body, attachment) if attachment else send(to, subject, body)
            print(f"envoyé {to} · {subject} · HTTP {status}")
        self.count += 1


# --- Abonnés ----------------------------------------------------------------
def links() -> dict:
    return json.loads(LINKS.read_text()) if LINKS.exists() else {"plans": {}, "portail": ""}


def import_trial_signups(subs: list) -> bool:
    """Ajoute les nouvelles inscriptions du formulaire Netlify « essai »."""
    token, site = os.environ.get("NETLIFY_TOKEN"), os.environ.get("NETLIFY_SITE_ID")
    if not (token and site):
        return False
    from billing import parse_departements, SECTEURS
    req = urllib.request.Request(
        f"https://api.netlify.com/api/v1/sites/{site}/submissions?per_page=100",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        submissions = json.load(r)
    known = {s["email"].lower() for s in subs}
    added = False
    for sub in submissions:
        if sub.get("form_name") not in ("essai", "essai-marches"):
            continue
        d = sub.get("data") or {}
        email = (d.get("email") or "").strip().lower()
        if not email or email in known:
            continue
        if sub.get("form_name") == "essai-marches":
            entry = marches_trial(d, email)
            if entry:
                subs.append(entry)
                known.add(email)
                added = True
            continue
        raw = d.get("secteurs") or []
        raw = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",")]
        secteurs = [x for x in raw if x in SECTEURS or x == "marches_attribues"] or ["restauration"]
        depts = parse_departements(d.get("departements"), 3)
        if not depts:
            continue
        subs.append({"email": email, "entreprise": d.get("entreprise", ""), "secteurs": secteurs,
                     "departements": depts, "source": "essai", "essai_debut": TODAY.isoformat(),
                     "essai_fin": (TODAY + dt.timedelta(days=ESSAI_JOURS - 1)).isoformat(),
                     "nouveau": True, "actif": True})
        if "marches_attribues" in secteurs:
            subs[-1]["premiere_marches"] = True
        known.add(email)
        added = True
    return added


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def marches_trial(d: dict, email: str) -> dict | None:
    """Inscription à l'essai depuis la page Radar Marchés attribués."""
    from billing import parse_departements, PLANS
    import marches
    depts = parse_departements(d.get("departements"), PLANS["marches"]["max_departements"])
    if not depts:
        return None
    types = [t for t in _as_list(d.get("types")) if t in marches.TYPES]
    mini = str(d.get("montant_min") or "0")
    return {"email": email, "entreprise": d.get("entreprise", ""), "produit": "marches",
            "secteurs": ["marches_attribues"], "departements": depts,
            "types": [] if len(types) == len(marches.TYPES) else types,
            "montant_min": int(mini) if mini.isdigit() else 0,
            "source": "essai", "essai_debut": TODAY.isoformat(),
            "essai_fin": (TODAY + dt.timedelta(days=ESSAI_JOURS - 1)).isoformat(),
            "nouveau": True, "premiere_marches": True, "actif": True}


def load_subscribers() -> tuple[list, list]:
    """Renvoie (liste d'envoi du jour, liste complète du fichier mise à jour)."""
    manual = json.loads(SUBS.read_text())
    try:
        imported = import_trial_signups(manual)
    except Exception as e:  # un souci Netlify ne doit jamais bloquer les alertes
        print(f"Import des essais impossible : {e}", file=sys.stderr)
        imported = False
    if imported:
        SUBS.write_text(json.dumps(manual, indent=2, ensure_ascii=False) + "\n")
    paying = []
    if os.environ.get("STRIPE_SECRET_KEY"):
        import billing
        paying = billing.paying_subscribers()
    paying_emails = {p["email"].lower() for p in paying}
    active = list(paying)
    for s in manual:
        if not s.get("actif", True) or s["email"].lower() in paying_emails:
            continue
        if s.get("source") == "essai" and TODAY > dt.date.fromisoformat(s["essai_fin"]):
            continue  # essai terminé, pas d'abonnement
        active.append(s)
    return active, manual


def footer_for(s: dict) -> str:
    if s.get("source") == "stripe":
        portail = links().get("portail")
        gerer = f'<a href="{portail}">Gérer ou résilier mon abonnement</a>. ' if portail else ""
        return gerer
    if s.get("source") == "essai":
        return (f"Essai gratuit jusqu'au {dt.date.fromisoformat(s['essai_fin']):%d/%m}. "
                "Pour ne plus recevoir ces emails, répondez « stop ». ")
    return ""


# --- Emails liés à l'essai ---------------------------------------------------
def _page(title: str, paragraphs: list) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f"<!doctype html><meta charset=utf-8><div style='font:15px/1.55 system-ui,sans-serif;"
            f"max-width:600px;margin:24px auto;padding:0 16px;color:#14213D'><h1 style='font-size:20px'>"
            f"{html.escape(title)}</h1>{body}<p style='color:#5B6576;font-size:13px'>Radar Entreprises · "
            f"<a href='{SITE}'>radar-entreprises.fr</a> · contact@radar-entreprises.fr</p></div>")


def suggested_plan(s: dict) -> str:
    if s["secteurs"] == ["marches_attribues"]:
        return "marches"
    bodacc = [x for x in s["secteurs"] if x != "marches_attribues"]
    return "pro" if len(bodacc) > 1 or len(s["departements"]) > 3 else "essentiel"


def is_marches(s: dict) -> bool:
    return s.get("produit") == "marches" or s["secteurs"] == ["marches_attribues"]


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
NOMS_OFFRES = {"essentiel": "Essentiel", "pro": "Pro", "marches": "Marchés attribués"}
PRIX = {"essentiel": 29, "pro": 49, "marches": 39}
DETAIL_OFFRES = {"essentiel": "1 secteur, 3 départements", "pro": "3 secteurs, 15 départements",
                 "marches": "jusqu'à 15 départements, filtres par type et montant"}


def jour_fr(d: dt.date) -> str:
    return f"{JOURS[d.weekday()]} {d.day}/{d.month:02d}"


def pay_link(plan: str, email: str) -> str | None:
    """Lien de paiement Stripe, email du client pré-rempli (même adresse que l'essai)."""
    url = links()["plans"].get(plan, {}).get("url")
    if not url:
        return None
    import urllib.parse
    return f"{url}?prefilled_email={urllib.parse.quote(email)}"


def _bouton(url: str, label: str, plein: bool = True) -> str:
    style = ("background:#14213D;color:#fff" if plein else "background:#fff;color:#14213D;border:2px solid #14213D")
    return (f"<a href='{html.escape(url)}' style='display:inline-block;{style};padding:12px 18px;"
            f"border-radius:4px;text-decoration:none;font-weight:600;margin:4px 8px 4px 0'>{html.escape(label)}</a>")


def offres_html(s: dict) -> str:
    """Boutons d'abonnement : l'offre adaptée à l'essai d'abord, l'autre à côté pour choisir."""
    if is_marches(s):
        plans = ["marches"]
    else:
        conseil = suggested_plan(s)
        plans = [conseil] + [x for x in ("essentiel", "pro") if x != conseil]
    out = []
    for i, plan in enumerate(plans):
        url = pay_link(plan, s["email"])
        if url:
            out.append(_bouton(url, f"{NOMS_OFFRES[plan]} · {PRIX[plan]} € / mois", plein=(i == 0)))
    if not out:
        return "Répondez simplement à cet email pour continuer."
    detail = " ; ".join(f"{NOMS_OFFRES[x]} : {DETAIL_OFFRES[x]}" for x in plans)
    return "".join(out) + f"<br><span style='color:#5B6576;font-size:13px'>{detail}.</span>"


def nouvelles_entreprises(s: dict, depuis: str, jusqua: str | None = None) -> int | None:
    """Nombre d'entreprises publiées au BODACC dans les secteurs et départements de l'abonné."""
    secteurs = [x for x in s.get("secteurs", []) if x != "marches_attribues"]
    if not secteurs or not s.get("departements"):
        return None
    try:
        con = sqlite3.connect(DB)
        q = (f"SELECT count(*) FROM annonces WHERE date_parution >= ? AND date_parution <= ? "
             f"AND secteur IN ({','.join('?' * len(secteurs))}) "
             f"AND departement IN ({','.join('?' * len(s['departements']))})")
        (n,) = con.execute(q, [depuis, jusqua or TODAY.isoformat(), *secteurs, *s["departements"]]).fetchone()
        return n
    except Exception:
        return None


def welcome_email(s: dict) -> str:
    fin = dt.date.fromisoformat(s["essai_fin"])
    if is_marches(s):
        return _page("Votre essai Radar Marchés attribués commence", [
            "Bonjour,",
            f"Votre essai gratuit est activé pour les départements {', '.join(s['departements'])}. "
            "Vous allez recevoir dans quelques minutes une première alerte avec les marchés publiés ces 7 "
            f"derniers jours, puis l'alerte de la semaine le mardi matin, jusqu'au {fin:%d/%m}.",
            "Chaque marché indique l'entreprise qui l'a remporté, l'acheteur et le montant. Le fichier "
            "Excel joint reprend tout, avec le SIRET et la fiche de chaque entreprise.",
            "Pensez à ajouter contact@radar-entreprises.fr à vos contacts. Pour changer de départements, "
            "de type de marchés ou de montant minimum, répondez simplement à cet email.",
            "À la fin de l'essai, rien n'est prélevé : vous choisissez de continuer (39 € par mois, sans engagement) ou non.",
        ])
    return _page("Votre essai Radar Entreprises commence", [
        "Bonjour,",
        f"Votre essai gratuit est activé pour les départements {', '.join(s['departements'])}. "
        f"Votre première alerte arrive dans quelques minutes, puis chaque matin vers 7 h (du mardi au samedi) jusqu'au {fin:%d/%m}.",
        "Le bon réflexe : contacter les nouveaux établissements dans la semaine de leur publication, avant qu'ils "
        "aient choisi leurs fournisseurs. Le fichier joint à chaque alerte s'ouvre directement dans Excel.",
        "Pensez à ajouter contact@radar-entreprises.fr à vos contacts pour que nos alertes ne finissent "
        "pas dans les courriers indésirables.",
        "À la fin de l'essai, rien n'est prélevé : vous choisissez de continuer (à partir de 29 € par mois, "
        "sans engagement) ou non. Une question, un département à ajouter ? Répondez simplement à cet email.",
    ])


def bilan_essai(s: dict) -> str:
    n = nouvelles_entreprises(s, s.get("essai_debut") or s["essai_fin"])
    if not n:
        return ""
    return (f"Depuis le début de votre essai, {n} nouvelles entreprises ont été publiées dans vos départements "
            f"({', '.join(s['departements'])}) : autant d'établissements qui choisissent leurs fournisseurs en ce moment.")


def reminder_email(s: dict) -> str:
    """Deux jours avant la fin : laisser le temps de décider, sans interruption des alertes."""
    fin = dt.date.fromisoformat(s["essai_fin"])
    if is_marches(s):
        return _page(f"Votre essai Radar Marchés attribués se termine {jour_fr(fin)}", [
            "Bonjour,",
            f"Votre essai se termine {jour_fr(fin)}. Pour continuer à recevoir chaque mardi les marchés publics "
            "remportés dans vos départements, avec le titulaire, l'acheteur et le montant :",
            offres_html(s),
            "Sans engagement, résiliable à tout moment. Si la liste ne correspond pas tout à fait à ce que vous "
            "cherchez (départements, type de travaux, montant minimum), répondez-moi : je l'ajuste.",
        ])
    return _page(f"Votre essai se termine {jour_fr(fin)}", [
        "Bonjour,",
        f"Votre essai gratuit Radar Entreprises se termine {jour_fr(fin)}.",
        bilan_essai(s),
        "Pour continuer à les recevoir chaque matin, sans interruption :",
        offres_html(s),
        "Sans engagement, résiliable en un clic. Une question ou un réglage à changer ? Répondez simplement à cet email.",
    ])


def end_of_trial_email(s: dict) -> str:
    fin = dt.date.fromisoformat(s["essai_fin"])
    dernier = TODAY <= fin
    if is_marches(s):
        return _page("Votre essai Radar Marchés attribués se termine aujourd'hui" if dernier
                     else "Votre essai Radar Marchés attribués est terminé", [
            "Bonjour,",
            ("C'est le dernier jour de votre essai. " if dernier else "Votre essai est terminé. ")
            + "Pour continuer à recevoir chaque mardi les marchés publics remportés dans vos départements, "
            "avec le titulaire et l'acheteur :",
            offres_html(s),
            "Sans engagement, résiliable à tout moment depuis le lien présent dans chaque alerte.",
            "Si vous ne souhaitez pas continuer, vous n'avez rien à faire : les envois s'arrêtent d'eux-mêmes.",
        ])
    return _page("Votre essai se termine aujourd'hui" if dernier else "Votre essai Radar Entreprises est terminé", [
        "Bonjour,",
        ("C'est le dernier jour de votre essai gratuit Radar Entreprises. " if dernier
         else "Votre essai gratuit Radar Entreprises est terminé. ")
        + "Pour continuer à recevoir chaque matin les nouvelles entreprises de votre zone, il suffit de vous abonner :",
        offres_html(s),
        bilan_essai(s),
        f"Sans engagement, résiliable à tout moment. Toutes les offres : <a href='{SITE}/#tarifs'>{SITE}/#tarifs</a>.",
        "Si vous ne souhaitez pas continuer, vous n'avez rien à faire : les envois s'arrêtent d'eux-mêmes.",
    ])


def winback_email(s: dict) -> str | None:
    """Quelques jours après la fin : ce qu'il a manqué depuis, et une question ouverte."""
    fin = dt.date.fromisoformat(s["essai_fin"])
    if is_marches(s):
        return _page("Les marchés attribués continuent", [
            "Bonjour,",
            "Votre essai Radar Marchés attribués est terminé depuis quelques jours. Les marchés publics continuent "
            "d'être attribués chaque semaine dans vos départements, et chaque titulaire choisit ses fournisseurs et "
            "sous-traitants dans la foulée.",
            offres_html(s),
            "Si quelque chose vous a manqué pendant l'essai, ou si ce n'était pas le bon moment, répondez-moi en "
            "une ligne : je lis tout et ça m'aide à améliorer le service.",
        ])
    n = nouvelles_entreprises(s, (fin + dt.timedelta(days=1)).isoformat())
    if not n:
        return None
    return _page(f"{n} nouvelles entreprises depuis la fin de votre essai", [
        "Bonjour,",
        f"Depuis la fin de votre essai, {n} nouvelles entreprises ont été publiées dans vos départements "
        f"({', '.join(s['departements'])}). Elles ne vous ont pas été envoyées.",
        "Pour reprendre les alertes dès demain matin :",
        offres_html(s),
        "Si quelque chose vous a manqué pendant l'essai, ou si ce n'était pas le bon moment, répondez-moi en "
        "une ligne : je lis tout et ça m'aide à améliorer le service.",
    ])


def trial_lifecycle(manual: list, outbox: Outbox, payants: set | None = None) -> bool:
    """Bienvenue, rappel deux jours avant la fin, fin d'essai, relance quelques jours après.
    Les alertes partent du mardi au samedi : chaque étape a une fenêtre de plusieurs jours pour
    ne jamais être sautée, et chaque email n'est envoyé qu'une fois (state/fin_essai_envoyes.json)."""
    done = set(json.loads(STATE_FIN_ESSAI.read_text())) if STATE_FIN_ESSAI.exists() else set()
    payants = payants or set()
    changed = False
    for s in manual:
        if s.get("source") != "essai" or not s.get("actif", True):
            continue
        nom = "Radar Marchés attribués" if is_marches(s) else "Radar Entreprises"
        if s.pop("nouveau", False):
            outbox(s["email"], f"Votre essai {nom} commence", welcome_email(s), "bienvenue")
            changed = True
            continue
        if s["email"].lower() in payants:
            continue  # déjà abonné : plus de rappel
        fin = dt.date.fromisoformat(s["essai_fin"])
        k_rappel, k_fin, k_relance = f"rappel|{s['email']}|{fin}", f"{s['email']}|{fin}", f"relance|{s['email']}|{fin}"
        if fin - dt.timedelta(days=2) <= TODAY < fin and k_rappel not in done and k_fin not in done:
            outbox(s["email"], f"Votre essai {nom} se termine {jour_fr(fin)}", reminder_email(s), "rappel_essai")
            done.add(k_rappel)
        elif fin <= TODAY <= fin + dt.timedelta(days=3) and k_fin not in done:
            sujet = (f"Votre essai {nom} se termine aujourd'hui" if TODAY == fin
                     else f"Votre essai {nom} est terminé")
            outbox(s["email"], sujet, end_of_trial_email(s), "fin_essai")
            done.add(k_fin)
        elif fin + dt.timedelta(days=4) <= TODAY <= fin + dt.timedelta(days=12) and k_fin in done and k_relance not in done:
            corps = winback_email(s)
            if corps:
                n = nouvelles_entreprises(s, (fin + dt.timedelta(days=1)).isoformat()) if not is_marches(s) else None
                sujet = (f"{n} nouvelles entreprises depuis la fin de votre essai" if n
                         else "Les marchés attribués continuent")
                outbox(s["email"], sujet, corps, "relance_essai")
            done.add(k_relance)
    if not outbox.dry:
        STATE_FIN_ESSAI.parent.mkdir(exist_ok=True)
        STATE_FIN_ESSAI.write_text(json.dumps(sorted(done), indent=1))
        if changed:
            SUBS.write_text(json.dumps(manual, indent=2, ensure_ascii=False) + "\n")
    return changed


# --- Alertes ----------------------------------------------------------------
def last_sent() -> str:
    """Date de parution BODACC la plus récente déjà envoyée (dédoublonnage robuste
    même si la base en cache est perdue)."""
    if STATE.exists():
        return STATE.read_text().strip()
    return (TODAY - dt.timedelta(days=2)).isoformat()


def select(con, since: str, sector: str, depts: list) -> list:
    con.row_factory = sqlite3.Row
    q = ("SELECT * FROM annonces WHERE date_parution > ? AND secteur = ? "
         f"AND departement IN ({','.join('?' * len(depts))}) ORDER BY departement, ville")
    return [dict(r) for r in con.execute(q, [since, sector, *depts])]


CSV_COLS = ["date_parution", "type", "denomination", "enseigne", "siren", "activite",
            "adresse", "cp", "ville", "departement", "date_debut_activite", "url"]


def to_csv(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore", delimiter=";")
    w.writeheader()
    w.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM : ouverture directe dans Excel


def bodacc_alerts(subs: list, outbox: Outbox):
    since = last_sent()
    con = sqlite3.connect(DB)
    (latest,) = con.execute("SELECT max(date_parution) FROM annonces").fetchone()
    for s in subs:
        for sector in s["secteurs"]:
            if sector == "marches_attribues":
                continue
            rows = select(con, since, sector, s["departements"])
            if not rows and not s.get("envoyer_si_vide", False):
                continue
            subject = (f"{len(rows)} nouvelles entreprises — "
                       f"{sector.replace('_', ' ')} ({', '.join(s['departements'])})")
            att = (f"radar_{sector}_{TODAY.isoformat()}.csv", to_csv(rows)) if rows else None
            outbox(s["email"], subject, email_html(sector, rows, TODAY.isoformat(), footer_for(s)), sector, att)
    if not outbox.dry and latest and latest > since:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(latest)


def _send_marches(s: dict, rows: list, outbox: Outbox, tag: str):
    import marches
    mine = marches.for_subscriber(rows, s)
    if not mine and not s.get("envoyer_si_vide", False):
        return False
    foot = footer_for(s)
    body = marches.email_html(mine, s, f"{foot}<br>" if foot else "")
    att = (f"marches_attribues_{TODAY.isoformat()}.csv", marches.to_csv(mine)) if mine else None
    outbox(s["email"], marches.subject(mine, s), body, tag, att)
    return True


def marches_alerts(subs: list, manual: list | None = None, outbox: Outbox | None = None):
    """Alerte hebdomadaire (mardi) + première alerte immédiate pour les nouveaux essais."""
    if outbox is None:  # ancienne signature marches_alerts(subs, outbox)
        manual, outbox = [], manual
    import marches
    targets = [s for s in subs if "marches_attribues" in s["secteurs"]]
    if not targets:
        return
    already = STATE_MARCHES.read_text().strip() if STATE_MARCHES.exists() else ""
    weekly = (TODAY.weekday() == 1 or os.environ.get("FORCE_MARCHES") == "1") and already != TODAY.isoformat()
    first = [s for s in targets if s.get("premiere_marches")]
    if not weekly and not first:
        return

    week_ago = (TODAY - dt.timedelta(days=8)).isoformat()
    last_pub = STATE_MARCHES_PUB.read_text().strip() if STATE_MARCHES_PUB.exists() else week_ago
    since = min(last_pub, week_ago) if first else last_pub
    rows = marches.collect_new(since)
    recents = [r for r in rows if r["date_publication"] > week_ago]
    weekly_rows = [r for r in rows if r["date_publication"] > last_pub]

    audience = (targets if weekly else []) + [s for s in first if not weekly]
    calls = marches.enrich(marches.needed(recents if not weekly else rows, audience))
    print(f"Marchés : {len(rows)} publiés depuis le {since}, {calls} recherches d'entreprises")

    for s in first:
        _send_marches(s, recents, outbox, "marches_premiere")
    if weekly:
        firsts = {id(s) for s in first}
        for s in targets:
            if id(s) not in firsts:
                _send_marches(s, weekly_rows, outbox, "marches")
    if not outbox.dry:
        STATE_MARCHES.parent.mkdir(exist_ok=True)
        if weekly:
            STATE_MARCHES.write_text(TODAY.isoformat())
            pubs = [r["date_publication"] for r in rows if r["date_publication"]]
            if pubs:
                STATE_MARCHES_PUB.write_text(max(pubs + [last_pub]))
        if first:
            emails = {s["email"].lower() for s in first}
            for m in manual or []:
                if m["email"].lower() in emails:
                    m.pop("premiere_marches", None)
            SUBS.write_text(json.dumps(manual, indent=2, ensure_ascii=False) + "\n")


def test_send(to: str):
    """Envoi de contrôle : dernière journée, secteur restauration, sans toucher à l'état."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    (day,) = con.execute("SELECT max(date_parution) FROM annonces").fetchone()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM annonces WHERE secteur='restauration' AND date_parution=? "
        "ORDER BY departement, ville LIMIT 40", (day,))]
    status = send(to, f"[Test] {len(rows)} nouvelles entreprises — restauration ({day})",
                  email_html("restauration", rows, day))
    print(f"test envoyé à {to} depuis {os.environ['SENDER_EMAIL']} · HTTP {status}")


def main():
    if os.environ.get("TEST_TO"):
        return test_send(os.environ["TEST_TO"])
    outbox = Outbox(os.environ.get("DRY_RUN") == "1" or not os.environ.get("BREVO_API_KEY"))
    subs, manual = load_subscribers()
    trial_lifecycle(manual, outbox, {x["email"].lower() for x in subs if x.get("source") == "stripe"})
    bodacc_alerts(subs, outbox)
    marches_alerts(subs, manual, outbox)
    print(f"{len(subs)} abonné(s) actif(s), {outbox.count} email(s) "
          f"{'simulé(s)' if outbox.dry else 'envoyé(s)'}")


if __name__ == "__main__":
    sys.exit(main())
