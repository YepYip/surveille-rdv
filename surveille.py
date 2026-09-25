"""Vérifie les créneaux "Premier RDV" d'un praticien et envoie une alerte Pushover.

Le dépôt est public : la page surveillée est fournie par le secret URL_RDV, et rien de ce que
le site affiche (nom du cabinet, praticiens, adresse…) ne doit être écrit dans les logs.
Les captures d'écran de diagnostic sont envoyées uniquement par Pushover.
"""
import os, re, sys, json, time, datetime, pathlib, requests
from collections import Counter
from playwright.sync_api import sync_playwright

URL = os.environ["URL_RDV"]
ETAT = pathlib.Path(".etat/empreinte.txt")  # passage précédent (JSON) : date du prochain RDV + heures vues
PANNE = pathlib.Path(".etat/panne.txt")  # présent tant que la panne a déjà été signalée
HEURE = re.compile(r"\b(\d{1,2})\s?[h:]\s?(\d{2})\b")
# Plages d'ouverture du cabinet (« 09:00 - 12:00 », « 14h à 19h00 »…) : retirées avant la lecture des créneaux.
# « Prochain rdv disponible à partir du JJ/MM » : affiché quand la semaine en cours est complète.
PROCHAIN = re.compile(r"(prochain[^\n]{0,80}?)\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", re.I)
# Message affiché quand le planning est plein ; sa disparition signale de nouveaux créneaux.
COMPLET = re.compile(r"aucune\s+disponibilit", re.I)
PLAGE = re.compile(r"\b\d{1,2}\s?[h:]\s?\d{2}\s*(?:-|–|à|a)\s*\d{1,2}\s?[h:]\s?\d{2}\b")


def envoyer(titre, message, prio="high", capture=None):
    """Notification Pushover, avec éventuellement une capture d'écran (JPEG) en pièce jointe."""
    data = {"token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
            "title": titre, "message": message, "priority": 1 if prio == "high" else 0,
            "url": URL, "url_title": "Réserver"}
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
        return etat.get("prochain"), Counter(etat.get("heures", {})), etat.get("complet")
    except (OSError, ValueError, AttributeError):
        return None, Counter(), None  # premier passage, ou ancien format


def lire_texte(page):
    """Texte de la page, iframes comprises (le module de réservation peut y être)."""
    textes = []
    for frame in page.frames:
        try:
            textes.append(frame.inner_text("body", timeout=2000))
        except Exception:
            pass
    return "\n".join(textes)


capture = None  # dernière capture d'écran, jointe aux notifications de test et de panne
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
        if test:
            capture = capturer(page)
        prochain = prochaine_date(texte)
        complet = bool(COMPLET.search(texte))
        vues = heures(texte)
        if prochain:
            # Le créneau est plus loin dans le calendrier : on clique sur le message pour y aller
            # et lire l'heure. Si le clic ne mène nulle part, on garde au moins la date.
            try:
                page.get_by_text(re.compile("prochain", re.I)).first.click(timeout=5000)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)
                texte = lire_texte(page)
                vues = heures(texte)
                etape("semaine du prochain RDV chargée")
                if test:
                    capture = capturer(page)
            except Exception as e:
                print("Impossible d'ouvrir la semaine du prochain RDV :", resume_erreur(e))
        browser.close()

    # Date du premier créneau : celle du message, ou aujourd'hui si des créneaux sont visibles
    # dans la semaine en cours (le message n'est alors pas affiché).
    if prochain is None and vues:
        prochain = datetime.date.today()
    ancien_prochain, anciennes_heures, ancien_complet = lire_etat()
    ancien_prochain = datetime.date.fromisoformat(ancien_prochain) if ancien_prochain else None
    nouvelles_heures = sorted((vues - anciennes_heures).elements())
    alerte = prochain is not None and (
        ancien_prochain is None or prochain < ancien_prochain  # un créneau plus proche s'est libéré
        or (prochain == ancien_prochain and nouvelles_heures)  # un autre créneau, même semaine
    )
    # Filet de sécurité : « Aucune disponibilité » a disparu, même si aucune date ni heure n'a été reconnue
    # (créneaux affichés dans un format inattendu).
    message_disparu = ancien_complet is True and not complet
    alerte = alerte or message_disparu
    etape("fin de la lecture")
    print("Prochain RDV :", prochain or "aucun", "| passage précédent :", ancien_prochain or "aucun")
    print("Heures vues :", dict(sorted(vues.items())) or "aucune")
    print("« Aucune disponibilité » affiché :", "oui" if complet else "non",
          "| passage précédent :", {True: "oui", False: "non"}.get(ancien_complet, "inconnu"))
    print("Alerte :", "oui" if alerte else "non")
    detail = f"Prochain RDV : {prochain:%d/%m}" if prochain else "Aucun RDV disponible"
    if vues:
        detail += " — horaires : " + ", ".join(sorted(vues)[:10])
    if message_disparu and not prochain:
        detail = "Le message « Aucune disponibilité » a disparu : de nouveaux créneaux sont peut-être ouverts."
    if test:
        envoyer("Test surveillance dentiste", f"Ça marche ! {detail}", prio="default", capture=capture)
    if alerte:
        envoyer("Créneau dentiste dispo !", detail)
    ETAT.parent.mkdir(exist_ok=True)
    ETAT.write_text(json.dumps({"prochain": prochain.isoformat() if prochain else None, "heures": vues,
                                 "complet": complet}))
    if PANNE.exists():
        envoyer("Surveillance dentiste rétablie", "La vérification fonctionne de nouveau.", prio="default")
        PANNE.unlink()
except Exception as e:
    # Pas de trace complète : elle pourrait contenir du texte de la page (logs publics).
    print("Échec :", resume_erreur(e))
    # Une seule alerte par panne, pas une à chaque passage.
    if not PANNE.exists():
        try:
            envoyer("Surveillance dentiste en panne", resume_erreur(e), prio="default", capture=capture)
            PANNE.parent.mkdir(exist_ok=True)
            PANNE.write_text(resume_erreur(e))
        except Exception as e2:
            print("Notification de panne impossible :", resume_erreur(e2))
    sys.exit(1)
