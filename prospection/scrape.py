"""Collecte des coordonnées publiques des prospects.

Pour chaque site : page d'accueil + pages contact / mentions légales.
On extrait : nom (balise title), emails publiés, code postal, SIREN, téléphone.
Seuls les emails publiés par l'entreprise sur son propre site sont retenus.
"""
import concurrent.futures as cf
import csv
import html
import re
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; RadarEntreprises/1.0; +https://radar-entreprises.fr)"
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CP_RX = re.compile(r"\b((?:0[1-9]|[1-8]\d|9[0-5]|2[AB])\d{3})\s+[A-ZÉÈa-zé][\w\-' ]{2,30}")
SIREN_RX = re.compile(r"(?:SIRE[NT]|RCS)[^0-9]{0,30}(\d{3}\s?\d{3}\s?\d{3})")
LINK_RX = re.compile(r'href="([^"]+)"[^>]*>([^<]{0,60})<', re.I)
BAD_EMAIL = re.compile(r"(\.png|\.jpg|\.jpeg|\.gif|\.webp|\.svg|example\.|sentry|wixpress|"
                       r"domain\.|email\.com|votre|your|@2x|godaddy|cloudflare)", re.I)
BAD_DOMAIN = re.compile(r"(ovh\.|prestashop|webapic|wix|shopify|o2switch|ionos|hostinger|"
                        r"netlify|vercel|wordpress|gandi|cnil\.fr|google\.com|facebook)", re.I)
BAD_LOCAL = re.compile(r"(cnil|dpo|rgpd|gdpr|privacy|donnees|abuse|noreply|no-reply|webmaster|"
                       r"recrutement|rh|jobs|candidature|compta|factur)", re.I)
FREE = {"gmail.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "outlook.fr",
        "hotmail.fr", "yahoo.fr", "outlook.com", "hotmail.com"}
KEYWORDS = ("contact", "mentions", "legal", "a-propos", "qui-sommes", "about")


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(1_500_000).decode("utf-8", errors="ignore")


def decode_cf(t: str) -> list:
    """Emails protégés par Cloudflare (data-cfemail)."""
    out = []
    for enc in re.findall(r'data-cfemail="([0-9a-f]+)"', t):
        k = int(enc[:2], 16)
        out.append("".join(chr(int(enc[i:i + 2], 16) ^ k) for i in range(2, len(enc), 2)))
    return out


def scan(row: dict) -> dict:
    site = row["site"]
    host = urllib.parse.urlparse(site).netloc.replace("www.", "")
    pages, texts = [site], []
    try:
        home = get(site)
    except Exception as e:
        return {**row, "statut": f"inaccessible ({type(e).__name__})"}
    texts.append(home)
    for href, label in LINK_RX.findall(home):
        h = href.lower() + " " + label.lower()
        if any(k in h for k in KEYWORDS):
            u = urllib.parse.urljoin(site, html.unescape(href))
            if urllib.parse.urlparse(u).netloc.replace("www.", "") == host and u not in pages:
                pages.append(u)
    for u in pages[1:6]:
        try:
            texts.append(get(u))
        except Exception:
            pass
    blob = " ".join(texts)
    plain = html.unescape(re.sub(r"<[^>]+>", " ", blob))

    emails = set(EMAIL_RX.findall(html.unescape(blob))) | set(decode_cf(blob))
    emails = {e.lower().strip(".") for e in emails if not BAD_EMAIL.search(e)}
    # Priorité aux emails du domaine de l'entreprise, puis génériques (contact@, info@...)
    root = host.rsplit(".", 2)[-2] if host.count(".") >= 1 else host
    emails = {e for e in emails if not BAD_DOMAIN.search(e.split("@")[1]) and not BAD_LOCAL.search(e.split("@")[0])}
    own = [e for e in emails if root in e.split("@")[1]]
    free = [e for e in emails if e.split("@")[1] in FREE]
    # Email du domaine de l'entreprise en priorité ; sinon une adresse gmail/orange publiée ;
    # jamais l'adresse d'un prestataire (hébergeur, agence web) trouvée dans les mentions.
    generic_first = sorted(own or free, key=lambda e: (not re.match(
        r"(contact|info|commercial|bonjour|hello|accueil|vente|ventes|pro)@", e), len(e)))

    title = re.search(r"<title[^>]*>(.*?)</title>", home, re.S | re.I)
    cps = CP_RX.findall(plain)
    siren = SIREN_RX.search(plain)
    return {
        **row,
        "nom": html.unescape(title.group(1)).strip()[:90] if title else host,
        "email": generic_first[0] if generic_first else "",
        "autres_emails": ";".join(generic_first[1:4]),
        "code_postal": cps[0] if cps else "",
        "departement": (cps[0][:2] if cps else ""),
        "siren": re.sub(r"\s", "", siren.group(1)) if siren else "",
        "pages_lues": len(texts),
        "statut": "ok" if generic_first else "pas d'email publié",
    }


def main():
    rows = list(csv.DictReader(open("seeds.csv", encoding="utf-8")))
    with cf.ThreadPoolExecutor(12) as ex:
        res = list(ex.map(scan, rows))
    cols = ["categorie", "nom", "site", "email", "autres_emails", "code_postal",
            "departement", "siren", "pages_lues", "statut"]
    with open("prospects_brut.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(res)
    ok = sum(r["statut"] == "ok" for r in res)
    print(f"{len(res)} sites, {ok} avec email")
    for r in res:
        print(f"{r['categorie']:12s} {r['statut'][:22]:22s} {r.get('email',''):38s} {r.get('code_postal','')}")


if __name__ == "__main__":
    main()
