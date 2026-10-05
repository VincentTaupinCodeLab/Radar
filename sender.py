"""Envoi des alertes aux abonnés.

Lit subscribers.json, sélectionne dans radar.db les annonces insérées depuis
le dernier envoi qui correspondent au secteur et aux départements de chaque
abonné, puis envoie un email via l'API Brevo (gratuite jusqu'à 300 emails/jour).

Variables d'environnement :
    BREVO_API_KEY   clé API Brevo (secret GitHub)
    SENDER_EMAIL    adresse d'expédition validée dans Brevo
    SENDER_NAME     nom affiché (défaut : « Radar Entreprises »)
    DRY_RUN=1       n'envoie rien, écrit les emails dans out/preview_*.html
"""
import datetime as dt
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
SUBS = ROOT / "subscribers.json"


def last_sent() -> str:
    """Date de parution BODACC la plus récente déjà envoyée. On s'appuie sur
    la date de parution (et non sur la date d'insertion) pour qu'une perte de
    la base en cache ne provoque jamais de renvoi en double."""
    if STATE.exists():
        return STATE.read_text().strip()
    return (dt.date.today() - dt.timedelta(days=2)).isoformat()


def select(con, since: str, sector: str, depts: list) -> list:
    con.row_factory = sqlite3.Row
    q = ("SELECT * FROM annonces WHERE date_parution > ? AND secteur = ? "
         f"AND departement IN ({','.join('?' * len(depts))}) "
         "ORDER BY departement, ville")
    return [dict(r) for r in con.execute(q, [since, sector, *depts])]


def send(to: str, subject: str, body: str):
    payload = {
        "sender": {"email": os.environ["SENDER_EMAIL"],
                   "name": os.environ.get("SENDER_NAME", "Radar Entreprises")},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": body,
    }
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


def con_latest() -> str | None:
    con = sqlite3.connect(DB)
    (v,) = con.execute("SELECT max(date_parution) FROM annonces").fetchone()
    con.close()
    return v


def main():
    dry = os.environ.get("DRY_RUN") == "1" or not os.environ.get("BREVO_API_KEY")
    since = last_sent()
    latest = con_latest()
    subs = json.loads(SUBS.read_text())
    con = sqlite3.connect(DB)
    label = dt.date.today().isoformat()
    sent = 0

    for s in subs:
        if not s.get("actif", True):
            continue
        for sector in s["secteurs"]:
            rows = select(con, since, sector, s["departements"])
            if not rows and not s.get("envoyer_si_vide", False):
                continue
            body = email_html(sector, rows, label)
            subject = (f"{len(rows)} nouvelles entreprises — "
                       f"{sector.replace('_', ' ')} ({', '.join(s['departements'])})")
            if dry:
                OUT.mkdir(exist_ok=True)
                (OUT / f"preview_{s['email'].split('@')[0]}_{sector}.html").write_text(body, encoding="utf-8")
                print(f"[dry-run] {s['email']} · {subject}")
            else:
                status = send(s["email"], subject, body)
                print(f"envoyé {s['email']} · {subject} · HTTP {status}")
            sent += 1

    if not dry and latest and latest > since:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(latest)
    print(f"{sent} email(s) {'simulé(s)' if dry else 'envoyé(s)'} depuis {since}")


if __name__ == "__main__":
    sys.exit(main())
