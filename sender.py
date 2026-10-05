"""Envoi des alertes aux abonnés.

Qui reçoit quoi :
- Abonnés payants : lus chaque matin dans Stripe (billing.py). Une résiliation les
  retire automatiquement.
- Essais gratuits : subscribers.json (source « essai », 7 jours). Les inscriptions du
  formulaire du site y sont importées automatiquement si NETLIFY_TOKEN est défini.
  Le dernier jour, un email de fin d'essai propose l'abonnement ; ensuite, plus d'envoi.
- Entrées de test : subscribers.json sans source (toujours actives).

Alertes :
- BODACC (secteurs) : chaque matin, uniquement les nouvelles parutions.
- Marchés attribués : une fois par semaine (le mardi), 7 derniers jours.

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
        if sub.get("form_name") != "essai":
            continue
        d = sub.get("data") or {}
        email = (d.get("email") or "").strip().lower()
        if not email or email in known:
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
        known.add(email)
        added = True
    return added


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


def welcome_email(s: dict) -> str:
    fin = dt.date.fromisoformat(s["essai_fin"])
    return _page("Votre essai Radar Entreprises commence", [
        "Bonjour,",
        f"Votre essai gratuit est activé pour les départements {', '.join(s['departements'])}. "
        f"Votre première alerte arrive dans quelques minutes, puis chaque matin vers 7 h (du mardi au samedi) jusqu'au {fin:%d/%m}.",
        "Pensez à ajouter contact@radar-entreprises.fr à vos contacts pour que nos alertes ne finissent "
        "pas dans les courriers indésirables.",
        "Une question, un département à ajouter ? Répondez simplement à cet email.",
    ])


def end_of_trial_email(s: dict) -> str:
    plan = suggested_plan(s)
    l = links()["plans"].get(plan, {})
    noms = {"essentiel": "Essentiel (29 € / mois)", "pro": "Pro (49 € / mois)",
            "marches": "Marchés attribués (39 € / mois)"}
    cta = (f"<a href='{l['url']}' style='display:inline-block;background:#14213D;color:#fff;"
           f"padding:12px 18px;border-radius:4px;text-decoration:none'>Continuer avec l'offre {noms[plan]}</a>"
           if l.get("url") else f"Répondez à cet email pour continuer avec l'offre {noms[plan]}.")
    return _page("Votre essai se termine aujourd'hui", [
        "Bonjour,",
        "C'est le dernier jour de votre essai gratuit Radar Entreprises. Pour continuer à recevoir "
        "chaque matin les nouvelles entreprises de votre zone, il suffit de vous abonner :",
        cta,
        f"Sans engagement, résiliable à tout moment. Toutes les offres : <a href='{SITE}/#tarifs'>{SITE}/#tarifs</a>.",
        "Si vous ne souhaitez pas continuer, vous n'avez rien à faire : les envois s'arrêtent d'eux-mêmes.",
    ])


def trial_lifecycle(manual: list, outbox: Outbox) -> bool:
    """Bienvenue pour les nouveaux essais, fin d'essai le dernier jour (une seule fois)."""
    done = set(json.loads(STATE_FIN_ESSAI.read_text())) if STATE_FIN_ESSAI.exists() else set()
    changed = False
    for s in manual:
        if s.get("source") != "essai" or not s.get("actif", True):
            continue
        if s.pop("nouveau", False):
            outbox(s["email"], "Votre essai Radar Entreprises commence", welcome_email(s), "bienvenue")
            changed = True
        key = f"{s['email']}|{s['essai_fin']}"
        if TODAY.isoformat() == s["essai_fin"] and key not in done:
            outbox(s["email"], "Votre essai Radar Entreprises se termine aujourd'hui",
                   end_of_trial_email(s), "fin_essai")
            done.add(key)
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


def marches_alerts(subs: list, outbox: Outbox):
    targets = [s for s in subs if "marches_attribues" in s["secteurs"]]
    already = STATE_MARCHES.read_text().strip() if STATE_MARCHES.exists() else ""
    weekly_day = TODAY.weekday() == 1 or os.environ.get("FORCE_MARCHES") == "1"
    if not targets or not weekly_day or already == TODAY.isoformat():
        return
    import decp
    since = (TODAY - dt.timedelta(days=7)).isoformat()
    rows = decp.collect(since)
    for s in targets:
        mine = [r for r in rows if set(r["departements"]) & set(s["departements"])]
        if not mine:
            continue
        body = decp.email_marches(mine, s["departements"], since)
        foot = footer_for(s)
        if foot:
            body += f"<p style='font:12px system-ui;color:#555'>{foot}</p>"
        outbox(s["email"], f"{len(mine)} marchés publics attribués ({', '.join(s['departements'])})",
               body, "marches")
    if not outbox.dry:
        STATE_MARCHES.write_text(TODAY.isoformat())


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
    trial_lifecycle(manual, outbox)
    bodacc_alerts(subs, outbox)
    marches_alerts(subs, outbox)
    print(f"{len(subs)} abonné(s) actif(s), {outbox.count} email(s) "
          f"{'simulé(s)' if outbox.dry else 'envoyé(s)'}")


if __name__ == "__main__":
    sys.exit(main())
