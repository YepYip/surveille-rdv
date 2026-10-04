"""Vérifie les créneaux "Premier RDV" d'un praticien et envoie une alerte Pushover.

Le dépôt est public : la page surveillée est fournie par le secret URL_RDV, et rien de ce que
le site affiche (nom du cabinet, praticiens, adresse…) ne doit être écrit dans les logs.
Les captures d'écran de diagnostic sont envoyées uniquement par Pushover.
"""
import os, re, sys, json, time, datetime, pathlib, requests
from collections import Counter
from playwright.sync_api import sync_playwright

URL = os.environ.get("URL_RDV", "").strip()
if not URL.startswith("https://"):
    sys.exit("Secret URL_RDV absent ou invalide (doit commencer par https://)")
ETAT = pathlib.Path(".etat/empreinte.txt")  # passage précédent (JSON) : date du prochain RDV + heures vues
PANNE = pathlib.Path(".etat/panne.txt")  # présent tant que la panne a déjà été signalée
HEURE = re.compile(r"\b(\d{1,2})\s?[h:]\s?(\d{2})\b")
# Plages d'ouverture du cabinet (« 09:00 - 12:00 », « 14h à 19h00 »…) : retirées avant la lecture des créneaux.
# « Prochain rdv disponible à partir du JJ/MM » : affiché quand la semaine en cours est complète.
PROCHAIN = re.compile(r"(prochain[^\n]{0,80}?)\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", re.I)
# Message affiché quand le planning est plein ; sa disparition signale de nouveaux créneaux.
COMPLET = re.compile(r"aucune\s+disponibilit", re.I)
PLAGE = re.compile(r"\b\d{1,2}\s?[h:]\s?\d{2}\s*(?:-|–|à|a)\s*\d{1,2}\s?[h:]\s?\d{2}\b")


def envoyer(titre, message, capture=None):
    """Notification Pushover, avec éventuellement une capture d'écran (JPEG) en pièce jointe."""
    data = {"token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
            "title": titre, "message": message, "priority": 0,  # priorité normale pour toutes les notifications
            "url": URL, "url_title": "Réserver",
            "ttl": 7 * 24 * 3600}  # la notification s'efface d'elle-même au bout de 7 jours
    api = "https://api.pushover.net/1/messages.json"
    if capture:
        r = requests.post(api, timeout=30, data=data,
                          files={"attachment": ("capture.jpg", capture, "image/jpeg")})
        if r.ok:
            return
        print("Capture refusée par Pushover (trop lourde ?), envoi sans pièce jointe")
    requests.post(api, timeout=30, data=data).raise_for_status()


def capturer(page):
    """Capture d'écran de la page, pour Pushover uniquement (jamais dans les logs publics)."""
    try:
        return page.screenshot(type="jpeg", quality=50, full_page=True)
    except Exception:
        return None


def resume_erreur(e):
    """Première ligne de l'erreur, sans l'URL ni le contenu de la page (logs publics)."""
    return f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".replace(URL, "<URL_RDV>")[:200]


DEBUT = time.monotonic()


def etape(nom):
    """Affiche le temps écoulé, pour repérer les étapes lentes dans les logs."""
    print(f"[{time.monotonic() - DEBUT:5.1f} s] {nom}", flush=True)


def heures(texte):
    """Compte les heures au format HH:MM (9h00, 09:00, 9 h 00…), hors plages d'ouverture.

    On compte les occurrences au lieu d'écarter des valeurs : un créneau à 09:00 reste
    détecté même si « 09:00 » apparaît aussi ailleurs sur la page.
    """
    texte = PLAGE.sub(" ", texte)
    return Counter(f"{int(h):02d}:{m}" for h, m in HEURE.findall(texte) if int(h) < 24 and int(m) < 60)


def prochaine_date(texte):
    """Date du « prochain rdv disponible », ou None si le message n'est pas affiché."""
    m = PROCHAIN.search(texte)
    if not m:
        return None
    print("Message « prochain rdv disponible » trouvé")  # pas le texte lui-même : il pourrait nommer le praticien
    jour, mois, an = int(m.group(2)), int(m.group(3)), m.group(4)
    aujourd_hui = datetime.date.today()
    if an:
        an = int(an) + (2000 if len(an) == 2 else 0)
    else:  # sans année : l'année en cours, ou la suivante si la date est déjà passée
        an = aujourd_hui.year + (1 if (mois, jour) < (aujourd_hui.month, aujourd_hui.day) else 0)
    try:
        return datetime.date(an, mois, jour)
    except ValueError:
        return None


def lire_etat():
    try:
        etat = json.loads(ETAT.read_text())
        return (etat.get("prochain"), Counter(etat.get("heures", {})), etat.get("complet"),
                int(etat.get("absences", 0)))
    except (OSError, ValueError, AttributeError, TypeError):
        return None, Counter(), None, 0  # premier passage, ou ancien format


def lire_texte(page):
    """Texte de la page, iframes comprises (le module de réservation peut y être)."""
    textes = []
    for frame in page.frames:
        try:
            textes.append(frame.inner_text("body", timeout=2000))
        except Exception:
            pass
    return "\n".join(textes)


capture = None  # dernière capture d'écran, jointe aux alertes, aux tests et aux pannes
try:
    with sync_playwright() as p:
        # Google Chrome est préinstallé sur les runners GitHub : pas de navigateur à télécharger.
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(locale="fr-FR")
        # 10 s au lieu de 30 par défaut : une panne du site est signalée plus vite.
        page.set_default_timeout(10000)
        # Images, polices et vidéos sont inutiles pour lire les horaires : on ne les charge pas.
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in {"image", "font", "media"} else route.continue_())
        etape("navigateur lancé")
        page.goto(URL, wait_until="networkidle")
        etape("page chargée")
        try:
            # Textes cherchés sans tenir compte des majuscules ni de l'apostrophe (' ou ’).
            # Nouveau patient : « Première visite » (anciennement « Vous n'avez jamais consulté »).
            try:
                page.get_by_text(re.compile(r"premi[eè]re visite|jamais consult", re.I)).first.click(timeout=5000)
                page.wait_for_load_state("networkidle")
            except Exception:
                print("Choix « Première visite » introuvable : étape ignorée")
            page.get_by_text(re.compile(r"premier\s+(rdv|rendez)", re.I)).first.click()
        except Exception:
            # Le site a changé : la capture jointe à l'alerte de panne montre ce qu'il affiche.
            capture = capturer(page)
            raise
        page.wait_for_load_state("networkidle")
        etape("créneaux chargés")
        page.wait_for_timeout(3000)
        texte = lire_texte(page)
        test = os.environ.get("TEST") == "1"
        # Le calendrier peut s'afficher avec retard : tant qu'on ne voit ni « Aucune disponibilité »,
        # ni date, ni heure, on relit la page (jusqu'à 15 s) avant de conclure. Sans cela, une page
        # lue trop tôt passait pour une disparition du message (fausse alerte).
        for _ in range(15):
            if COMPLET.search(texte) or PROCHAIN.search(texte) or heures(texte):
                break
            page.wait_for_timeout(1000)
            texte = lire_texte(page)
        # Capture à chaque passage (~0,5 s), une fois le calendrier affiché : elle accompagne
        # l'alerte si un créneau apparaît.
        capture = capturer(page)
        prochain = prochaine_date(texte)
        complet = bool(COMPLET.search(texte))
        vues = heures(texte)
        if prochain:
            # Le créneau est plus loin dans le calendrier : on va à sa semaine pour lire l'heure.
            # Le lien est le bouton « Cliquez ici pour y accéder » (le texte « Prochaine
            # disponibilité le JJ/MM » n'est pas cliquable). Sinon, on garde au moins la date.
            try:
                lien = page.get_by_text(re.compile(r"y acc[eé]der", re.I))
                if not lien.count():
                    lien = page.get_by_text(re.compile("prochain", re.I))
                lien.first.click(timeout=5000)
                page.wait_for_load_state("networkidle")
                # Les créneaux peuvent s'afficher avec retard : jusqu'à 10 s pour voir une heure.
                for _ in range(10):
                    page.wait_for_timeout(1000)
                    texte = lire_texte(page)
                    vues = heures(texte)
                    if vues:
                        break
                etape("semaine du prochain RDV chargée")
                if not vues:
                    print("Semaine du prochain RDV ouverte, mais aucune heure lue")
                capture = capturer(page)  # la semaine du créneau, plus parlante que la semaine en cours
            except Exception as e:
                print("Impossible d'ouvrir la semaine du prochain RDV :", resume_erreur(e))
        browser.close()

    # Date du premier créneau : celle du message, ou aujourd'hui si des créneaux sont visibles
    # dans la semaine en cours (le message n'est alors pas affiché).
    if prochain is None and vues:
        prochain = datetime.date.today()
    ancien_prochain, anciennes_heures, ancien_complet, anciennes_absences = lire_etat()
    ancien_prochain = datetime.date.fromisoformat(ancien_prochain) if ancien_prochain else None
    nouvelles_heures = sorted((vues - anciennes_heures).elements())
    alerte = prochain is not None and (
        ancien_prochain is None or prochain < ancien_prochain  # un créneau plus proche s'est libéré
        or (prochain == ancien_prochain and nouvelles_heures)  # un autre créneau, même semaine
    )
    # Filet de sécurité : ni « Aucune disponibilité », ni date, ni heure (créneaux affichés dans un
    # format inattendu ?). Alerte seulement au 2e passage consécutif dans ce cas, pour écarter une
    # page lue trop tôt ou un affichage raté ; une seule alerte par épisode.
    absences = anciennes_absences + 1 if (not complet and prochain is None and not vues) else 0
    message_disparu = absences == 2
    alerte = alerte or message_disparu
    etape("fin de la lecture")
    print("Prochain RDV :", prochain or "aucun", "| passage précédent :", ancien_prochain or "aucun")
    print("Heures vues :", dict(sorted(vues.items())) or "aucune")
    print("« Aucune disponibilité » affiché :", "oui" if complet else "non",
          "| passage précédent :", {True: "oui", False: "non"}.get(ancien_complet, "inconnu"),
          "| passages consécutifs sans message, date ni heure :", absences)
    print("Alerte :", "oui" if alerte else "non")
    detail = f"Prochain RDV : {prochain:%d/%m}" if prochain else "Aucun RDV disponible"
    if vues:
        detail += " — horaires : " + ", ".join(sorted(vues)[:10])
    if message_disparu and not prochain:
        detail = ("Le message « Aucune disponibilité » n'apparaît plus depuis 2 passages : "
                  "de nouveaux créneaux sont peut-être ouverts.")
    if test:
        envoyer("Test surveillance dentiste", f"Ça marche ! {detail}", capture=capture)
    if alerte:
        envoyer("Créneau dentiste dispo !", detail, capture=capture)
    ETAT.parent.mkdir(exist_ok=True)
    ETAT.write_text(json.dumps({"prochain": prochain.isoformat() if prochain else None, "heures": vues,
                                 "complet": complet, "absences": absences}))
    if PANNE.exists():
        envoyer("Surveillance dentiste rétablie", "La vérification fonctionne de nouveau.")
        PANNE.unlink()
except Exception as e:
    # Pas de trace complète : elle pourrait contenir du texte de la page (logs publics).
    print("Échec :", resume_erreur(e))
    # Une seule alerte par panne, pas une à chaque passage.
    if not PANNE.exists():
        try:
            envoyer("Surveillance dentiste en panne", resume_erreur(e), capture=capture)
            PANNE.parent.mkdir(exist_ok=True)
            PANNE.write_text(resume_erreur(e))
        except Exception as e2:
            print("Notification de panne impossible :", resume_erreur(e2))
    sys.exit(1)
