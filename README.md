# surveille-dentiste

Surveille les créneaux « Premier RDV » d'un praticien sur un site de prise de rendez-vous
dentaire, et envoie une notification [Pushover](https://pushover.net) dès qu'un créneau se libère.

## Fonctionnement

- `surveille.py` ouvre la page du praticien avec Playwright (Chrome), choisit « Première visite »
  puis « Premier RDV », et lit le calendrier : heures affichées, message « Prochain rdv disponible
  à partir du JJ/MM », message « Aucune disponibilité ».
- Une alerte part quand la date du prochain rendez-vous se rapproche, quand une nouvelle heure
  apparaît, ou quand le message « Aucune disponibilité » disparaît.
- L'état entre deux passages est conservé dans le cache GitHub Actions.
- Le workflow est déclenché de l'extérieur (service de cron appelant l'API `workflow_dispatch`),
  car le planificateur de GitHub saute beaucoup d'exécutions.

## Configuration

Secrets du dépôt (Settings → Secrets and variables → Actions) :

| Secret | Contenu |
|---|---|
| `URL_RDV` | URL de la page de prise de rendez-vous du praticien |
| `PUSHOVER_TOKEN` | jeton de l'application Pushover |
| `PUSHOVER_USER` | clé utilisateur Pushover |

Lancement manuel : onglet Actions → « Surveille dentiste » → Run workflow. Avec `test` coché, une
notification de test est envoyée, avec une capture d'écran de la page.
