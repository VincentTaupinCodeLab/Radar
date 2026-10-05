# Prospection Radar Entreprises

- `seeds.csv` : sites de fournisseurs du secteur restauration trouvés par recherche web.
- `scrape.py` : collecte les emails professionnels **publiés par l'entreprise sur son propre site**
  (accueil, contact, mentions légales). Les adresses d'hébergeurs, d'agences, de DPO/RGPD et
  de recrutement sont écartées.
- `prospects.csv` : liste retenue (52 entreprises, domaines email vérifiés).
- `sequence.py` : génère email 1 + 2 relances, personnalisés avec de vraies créations de la
  semaine dans le département du prospect → `brouillons.csv` et `apercu.html`.
- `suivi.csv` : statut de chaque prospect (à valider → envoyé → relancé → réponse / stop / essai).

## Règles
- B2B uniquement, adresses génériques d'entreprise, message lié à leur activité.
- « stop » = retrait immédiat et définitif (statut `stop`, plus aucun envoi).
- 20 à 30 envois par jour maximum, depuis la boîte de Vincent (pas via Brevo).
- Relance 1 à J+4, relance 2 à J+10, arrêt dès la première réponse.
