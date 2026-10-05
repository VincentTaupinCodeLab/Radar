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
