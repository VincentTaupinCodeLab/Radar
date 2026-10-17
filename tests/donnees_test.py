"""Base de test commune à test_pages.py et test_liste.py : quelques annonces BODACC et marchés
DECP fabriqués, avec les cas à écarter (personnes physiques, opposition, titulaires non identifiés)."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import marches  # noqa: E402
import pages  # noqa: E402
import pipeline  # noqa: E402

TODAY = dt.date(2026, 10, 14)  # un mercredi
SIREN_OPPOSITION = "999888777"


def annonce(i, dept, secteur, jours, pp=0, nom=None, typ="creation", siren=None, ville="BORDEAUX"):
    d = (TODAY - dt.timedelta(days=jours)).isoformat()
    return {"id": f"A{i}", "date_parution": d, "type": typ, "siren": siren or f"{100000000 + i}",
            "personne_physique": pp, "denomination": nom or f"SOCIETE TEST {i}", "enseigne": "",
            "forme_juridique": "Entrepreneur individuel" if pp else "Société par actions simplifiée",
            "activite": {"restauration": "Restaurant traditionnel, bar", "btp": "Travaux de maçonnerie générale",
                         "holding_sci": "Prise de participations"}.get(secteur, "Conseil"),
            "secteur": secteur, "origine_fonds": "", "adresse": "1 rue du Test", "cp": f"{dept}000",
            "ville": ville, "departement": dept, "date_debut_activite": "",
            "url": f"https://www.bodacc.fr/pages/annonces-commerciales-detail/?q.id=id:A{i}"}


def build(db: Path):
    con = sqlite3.connect(db)
    con.executescript(pipeline.SCHEMA)
    rows = []
    # Gironde : 5 restaurants cette semaine (page département + page secteur), 1 holding
    for i in range(5):
        rows.append(annonce(i, "33", "restauration", i, ville="Mérignac" if i % 2 else "BORDEAUX"))
    rows.append(annonce(10, "33", "holding_sci", 2))
    # Personne physique (entrepreneur individuel) : ne doit jamais apparaître
    rows.append(annonce(11, "33", "restauration", 1, pp=1, nom="JEAN-PIERRE DUPONTEL"))
    # Opposition : société retirée à sa demande
    rows.append(annonce(12, "33", "restauration", 1, nom="SOCIETE OPPOSEE", siren=SIREN_OPPOSITION))
    # Charente-Maritime : 2 BTP seulement (pas de page secteur), dont un il y a 20 jours (pas cette semaine)
    rows.append(annonce(20, "17", "btp", 3, ville="LA ROCHELLE"))
    rows.append(annonce(21, "17", "btp", 20, ville="ROYAN"))
    # Landes : une seule annonce, vieille de 20 jours : ni page de la semaine ni page secteur
    rows.append(annonce(30, "40", "restauration", 20))
    cols = list(rows[0])
    con.executemany(f"INSERT INTO annonces ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows])

    con.executescript(pages.SCHEMA_MARCHES)
    marches._cache(con)
    ach_a, ach_b = "21330063500017", "21330999900011"
    t_ok, t_ei, t_inconnu = "55210055400013", "80000000100018", "70000000200019"
    t_opp = SIREN_OPPOSITION + "00011"

    def m(i, ach, tit, jours, dept="33", montant=50000.0):
        d = (TODAY - dt.timedelta(days=jours)).isoformat()
        return (f"M{i}", ach, tit, f"TRAVAUX DE VOIRIE LOT {i}", montant, d, d, "Travaux BTP", dept)

    con.executemany("INSERT INTO decp_marches VALUES (?,?,?,?,?,?,?,?,?)", [
        m(1, ach_a, t_ok, 3), m(2, ach_a, t_ei, 10), m(3, ach_a, t_inconnu, 40), m(4, ach_a, t_ok, 200),
        m(5, ach_a, t_opp, 5),
        m(6, ach_b, t_ok, 6), m(7, ach_b, t_ok, 9),            # acheteur B : 2 marchés, pas de page
        m(8, ach_a, t_ok, 500),                                # trop ancien (plus de 12 mois)
    ])
    today = TODAY.isoformat()
    con.executemany("INSERT INTO entreprises (siret,nom,ville,cp,naf,effectif,trouve,maj,nature) VALUES (?,?,?,?,?,?,?,?,?)", [
        (ach_a, "COMMUNE DE MERIGNAC", "Mérignac", "33700", "84.11Z", "", 1, today, "7210"),
        (ach_b, "SYNDICAT DES EAUX TEST", "Bordeaux", "33000", "84.11Z", "", 1, today, "7354"),
        (t_ok, "COLAS FRANCE", "Mérignac", "33700", "42.11Z", "", 1, today, "5710"),
        (t_ei, "PAUL MARTINEAU", "Pessac", "33600", "43.99C", "", 1, today, "1000"),
        (t_opp, "ENTREPRISE OPPOSEE", "Pessac", "33600", "43.99C", "", 1, today, "5499"),
    ])
    con.commit()
    return con


INTERDITS = ["DUPONTEL", "PAUL MARTINEAU", "SOCIETE OPPOSEE", "ENTREPRISE OPPOSEE"]
