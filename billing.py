"""Facturation Stripe : offres, liens de paiement et synchronisation des abonnés.

Stripe est la source de vérité pour les clients payants : chaque matin,
`paying_subscribers()` lit les abonnements actifs et en déduit la liste d'envoi.
Un abonné qui résilie disparaît donc automatiquement des envois.

Variables d'environnement :
    STRIPE_SECRET_KEY   clé secrète (sk_test_... en test, sk_live_... en production)
    STRIPE_API_BASE     facultatif, pour tester contre stripe-mock (http://localhost:12111)

Usage :
    python billing.py setup   # crée/retrouve offres, liens de paiement et portail client
    python billing.py sync    # affiche les abonnés payants actuels
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
LINKS = ROOT / "site" / "stripe.json"   # publié avec le site : URLs publiques uniquement
SITE = "https://radar-entreprises.fr"

SECTEURS = {
    "restauration": "Restauration et bars",
    "commerce_alimentaire": "Commerce alimentaire",
    "beaute_bien_etre": "Beauté et bien-être",
    "btp": "BTP et artisans",
    "commerce_detail": "Commerce de détail",
    "auto_moto": "Auto et moto",
    "immobilier": "Immobilier",
    "transport_livraison": "Transport et livraison",
    "conseil_services_b2b": "Conseil et services B2B",
}

# Montants en centimes. Micro-entreprise en franchise de TVA : prix HT = prix payé.
PLANS = {
    "essentiel": {"nom": "Radar Entreprises — Essentiel", "prix": 2900,
                  "description": "1 secteur, jusqu'à 3 départements, alerte chaque matin.",
                  "max_secteurs": 1, "max_departements": 3},
    "pro": {"nom": "Radar Entreprises — Pro", "prix": 4900,
            "description": "Jusqu'à 3 secteurs sur toute une région, alerte chaque matin.",
            "max_secteurs": 3, "max_departements": 15},
    "marches": {"nom": "Radar Entreprises — Marchés attribués", "prix": 3900,
                "description": "Marchés publics attribués dans vos départements, chaque semaine.",
                "max_secteurs": 0, "max_departements": 15},
}

DEPT_RX = re.compile(r"\b(2A|2B|97[1-6]|0[1-9]|[1-8]\d|9[0-5])\b", re.I)


# --- Client HTTP minimal (aucune dépendance) --------------------------------
def _flatten(data, prefix=""):
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += _flatten(v, f"{prefix}[{k}]" if prefix else k)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += _flatten(v, f"{prefix}[{i}]")
    elif isinstance(data, bool):
        out.append((prefix, "true" if data else "false"))
    elif data is not None:
        out.append((prefix, str(data)))
    return out


def api(method: str, path: str, data: dict | None = None) -> dict:
    key = os.environ["STRIPE_SECRET_KEY"]
    base = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
    url = f"{base}/v1/{path}"
    body = None
    if data and method == "GET":
        url += "?" + urllib.parse.urlencode(_flatten(data))
    elif data:
        body = urllib.parse.urlencode(_flatten(data)).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode(),
        "Stripe-Version": "2024-06-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Stripe {method} {path} → HTTP {e.code} : {e.read().decode()[:400]}")


def list_all(path: str, params: dict | None = None):
    params = dict(params or {}, limit=100)
    while True:
        page = api("GET", path, params)
        yield from page["data"]
        if not page.get("has_more") or not page["data"]:
            return
        params["starting_after"] = page["data"][-1]["id"]


# --- Mise en place des offres -----------------------------------------------
def custom_fields(plan: str) -> list:
    p = PLANS[plan]
    fields = [{
        "key": "departements",
        "label": {"type": "custom", "custom": f"Départements (jusqu'à {p['max_departements']})"},
        "type": "text",
        "text": {"minimum_length": 2, "maximum_length": 120},
    }]
    if p["max_secteurs"]:
        fields.append({
            "key": "secteur",
            "label": {"type": "custom", "custom": "Secteur principal"},
            "type": "dropdown",
            "dropdown": {"options": [{"label": v, "value": k.replace("_", "")[:100]}
                                     for k, v in SECTEURS.items()]},
        })
    if p["max_secteurs"] > 1:
        fields.append({
            "key": "autres",
            "label": {"type": "custom", "custom": "Autres secteurs (facultatif)"},
            "type": "text", "optional": True,
            "text": {"maximum_length": 120},
        })
    return fields


def setup() -> dict:
    """Idempotent : retrouve ce qui existe (metadata radar_plan), crée le reste."""
    products = {(p.get("metadata") or {}).get("radar_plan"): p
                for p in list_all("products", {"active": "true"})}
    links = {(l.get("metadata") or {}).get("radar_plan"): l
             for l in list_all("payment_links", {"active": "true"})}
    out = {"plans": {}}
    for plan, p in PLANS.items():
        prod = products.get(plan)
        if not prod:
            prod = api("POST", "products", {
                "name": p["nom"], "description": p["description"],
                "metadata": {"radar_plan": plan},
                "default_price_data": {"currency": "eur", "unit_amount": p["prix"],
                                       "recurring": {"interval": "month"}},
            })
        link = links.get(plan)
        if not link:
            link = api("POST", "payment_links", {
                "line_items": [{"price": prod["default_price"], "quantity": 1}],
                "custom_fields": custom_fields(plan),
                "metadata": {"radar_plan": plan},
                "subscription_data": {"metadata": {"radar_plan": plan}},
                "allow_promotion_codes": True,
                "billing_address_collection": "required",
                "tax_id_collection": {"enabled": True},
                "after_completion": {"type": "redirect",
                                     "redirect": {"url": f"{SITE}/bienvenue.html"}},
            })
        out["plans"][plan] = {"url": link["url"], "prix": p["prix"] / 100}

    portals = [c for c in list_all("billing_portal/configurations", {"active": "true"})
               if (c.get("metadata") or {}).get("radar") == "portail"]
    portal = portals[0] if portals else api("POST", "billing_portal/configurations", {
        "business_profile": {"headline": "Gérer votre abonnement Radar Entreprises",
                             "privacy_policy_url": f"{SITE}/mentions-legales.html",
                             "terms_of_service_url": f"{SITE}/mentions-legales.html"},
        "features": {
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "tax_id"]},
        },
        "login_page": {"enabled": True},
        "metadata": {"radar": "portail"},
    })
    out["portail"] = (portal.get("login_page") or {}).get("url") or ""
    LINKS.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# --- Lecture des abonnés payants --------------------------------------------
SECTEUR_KEYS = {k.replace("_", ""): k for k in SECTEURS}
SECTEUR_WORDS = [  # pour interpréter le champ libre « autres secteurs »
    ("restau", "restauration"), ("bar", "restauration"), ("aliment", "commerce_alimentaire"),
    ("boulang", "commerce_alimentaire"), ("beaut", "beaute_bien_etre"), ("coiff", "beaute_bien_etre"),
    ("btp", "btp"), ("artisan", "btp"), ("batiment", "btp"), ("bâtiment", "btp"),
    ("detail", "commerce_detail"), ("détail", "commerce_detail"), ("commerce", "commerce_detail"),
    ("auto", "auto_moto"), ("garage", "auto_moto"), ("immo", "immobilier"),
    ("transport", "transport_livraison"), ("livraison", "transport_livraison"),
    ("conseil", "conseil_services_b2b"), ("b2b", "conseil_services_b2b"), ("service", "conseil_services_b2b"),
]


def parse_departements(text: str, limit: int) -> list:
    seen = []
    for d in DEPT_RX.findall(text or ""):
        d = d.upper().zfill(2)
        if d not in seen:
            seen.append(d)
    return seen[:limit]


def parse_secteurs(main: str | None, autres: str | None, limit: int) -> list:
    out = []
    if main and main in SECTEUR_KEYS:
        out.append(SECTEUR_KEYS[main])
    for word, key in SECTEUR_WORDS:
        if autres and word in autres.lower() and key not in out:
            out.append(key)
    return out[:limit]


def subscriber_from_session(session: dict, plan: str, status: str) -> dict | None:
    fields = {f["key"]: (f.get("text") or {}).get("value") or (f.get("dropdown") or {}).get("value")
              for f in session.get("custom_fields") or []}
    p = PLANS[plan]
    email = (session.get("customer_details") or {}).get("email")
    depts = parse_departements(fields.get("departements"), p["max_departements"])
    if not email or not depts:
        return None
    secteurs = parse_secteurs(fields.get("secteur"), fields.get("autres"), p["max_secteurs"])
    if plan == "marches":
        secteurs = ["marches_attribues"]
    return {"email": email, "plan": plan, "secteurs": secteurs, "departements": depts,
            "source": "stripe", "statut_stripe": status, "actif": True}


def paying_subscribers() -> list:
    """Abonnés dont l'abonnement est actif (ou en simple retard de paiement)."""
    subs = []
    for status in ("active", "trialing", "past_due"):
        for sub in list_all("subscriptions", {"status": status}):
            plan = (sub.get("metadata") or {}).get("radar_plan")
            if plan not in PLANS:
                continue
            sessions = list(list_all("checkout/sessions", {"subscription": sub["id"]}))
            if not sessions:
                continue
            s = subscriber_from_session(sessions[0], plan, status)
            if s:
                subs.append(s)
    return subs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "setup":
        print(json.dumps(setup(), indent=2, ensure_ascii=False))
    else:
        for s in paying_subscribers():
            print(s)
