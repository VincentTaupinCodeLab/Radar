# Radar — pipeline de données publiques (v1)

Deux sources, un même socle, plusieurs produits vendables.

| Script | Source | Produit |
|---|---|---|
| `pipeline.py` | BODACC (créations + ventes/cessions de fonds) | Radar « nouvelles entreprises » par secteur |
| `decp.py` | DECP, data.economie.gouv.fr | Radar « marchés publics attribués » |

Aucune clé d'API nécessaire : les deux sources sont ouvertes (Licence Ouverte 2.0).

## Lancer

```bash
python3 pipeline.py --days 7 --region 75        # Nouvelle-Aquitaine
python3 pipeline.py --days 1 --dept 16 17       # Charente + Charente-Maritime
python3 decp.py --days 30 --region 75
```

Sorties dans `out/` : un CSV complet et un email HTML par secteur.
Les annonces BODACC sont dédoublonnées dans `radar.db` (SQLite).

## Volumes mesurés (Nouvelle-Aquitaine)

- BODACC, semaine du 28/09/2026 : 1 010 annonces (créations + reprises),
  dont ~63 restauration, ~67 BTP, ~28 beauté.
- DECP, 01/09 → 05/10/2026 : 463 marchés attribués, dont 220 en travaux BTP
  (≈ 102 M€).

## Limites connues

- Classification sectorielle par mots-clés : précision estimée 85–90 %
  après réglages. À remplacer par le code NAF (API SIRENE) ou une
  classification par LLM dans le runner de production.
- DECP : le titulaire n'est identifié que par SIRET → lien vers
  l'Annuaire des entreprises ; enrichissement du nom à faire en production.
- RGPD : les entrepreneurs individuels sont des personnes physiques. Mentions
  d'origine des données et droit d'opposition obligatoires avant toute
  prospection ou revente (texte provisoire dans le pied des emails, à valider).

## Prochaines étapes

1. Runner quotidien (GitHub Actions ou Supabase) + envoi des emails (Brevo/Resend).
2. Enrichissement SIRENE (nom, NAF, effectif) côté production.
3. Page de vente + paiement Stripe + essai gratuit 7 jours.
4. Publication de 2 outils sur Apify (même code).

## Paiement et abonnés (Stripe)

- `billing.py setup` crée (une seule fois, sans doublon) les 3 offres, leurs liens de paiement
  et le portail client, puis écrit `site/stripe.json`. Le site affiche alors les boutons
  « S'abonner directement » et le lien « Gérer mon abonnement ».
- Chaque matin, `sender.py` lit les abonnements actifs dans Stripe : un client qui paie reçoit
  ses alertes, un client qui résilie n'en reçoit plus. Aucune base de données à tenir.
- Essais gratuits : inscriptions du formulaire importées depuis Netlify (secret `NETLIFY_TOKEN`),
  email de bienvenue, 7 jours d'alertes, email de fin d'essai avec le lien de l'offre adaptée.
- `tests/test_parcours.py` vérifie tout le parcours sans réseau (exécuté à chaque passage).

Secrets GitHub : `BREVO_API_KEY`, `SENDER_EMAIL`, `STRIPE_SECRET_KEY` (à ajouter au lancement),
`NETLIFY_TOKEN` (facultatif, pour l'import automatique des essais).

## Pages de référencement et liste gratuite

- `pages.py` (fin de `daily.yml` et `site.yml`, juste avant la mise en ligne) écrit dans `site/`
  les pages `/departements/` (créations et reprises BODACC des 7 derniers jours par grand secteur,
  pages département × secteur sur 30 jours dès 3 entrées), `/marches/departements/` (10 marchés
  récents par département) et `/marches/acheteurs/` (acheteurs ayant au moins 3 marchés en 12 mois),
  ainsi que `sitemap.xml`. Ces fichiers ne sont pas versionnés (`.gitignore`) : tout workflow qui
  déploie `site/` doit lancer `python pages.py` avant (`stripe-setup.yml` ne le fait pas).
- Seules les personnes morales sont affichées (annonces « pp » écartées ; titulaires de marchés
  confirmés comme personnes morales par l'Annuaire des entreprises). Droit d'opposition :
  ajouter le SIREN dans `state/opposition.txt`.
- La base garde 35 jours de BODACC et 13 mois de DECP (table `decp_marches`). Pour reconstituer
  30 jours de BODACC : lancer « Radar quotidien » à la main avec `jours_bodacc = 31`.
- `liste.py` (workflow `liste-gratuite.yml`, toutes les 3 h) gère le formulaire Netlify
  `liste-gratuite` : email de confirmation (double opt-in, formulaire `liste-confirmation`),
  désinscription en un clic (`liste-desinscription`), envoi du lundi (10 entrées) par Brevo, offre
  d'essai à partir du 4e envoi. Les adresses restent dans Netlify ; `state/liste_gratuite.json`
  (source « gratuit ») n'en contient aucune, le dépôt étant public. Secret facultatif `LISTE_SECRET`
  pour signer les liens de confirmation (à défaut, `NETLIFY_TOKEN`).
- Tests : `tests/test_pages.py` et `tests/test_liste.py` (base de test, sans réseau), lancés par
  `tests.yml` à chaque pull request.
