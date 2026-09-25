# surveille-dentiste

Surveille les créneaux « Premier RDV » d'un praticien sur un site de prise de rendez-vous dentaire et envoie une alerte Pushover quand de nouveaux créneaux apparaissent.

## Règles de travail

- **Toujours committer et pousser directement sur `main`.** Ne pas créer de branche ni de pull request.
- Les messages de commit et les commentaires du code sont en français.
- **Dépôt public et anonyme.** Ne jamais écrire dans le code, la documentation, les messages de commit ni les logs d'exécution quoi que ce soit qui identifie la cible : URL, nom du cabinet, ville, adresse, téléphone, noms des praticiens. La page surveillée est uniquement dans le secret `URL_RDV`.
  - Les logs GitHub Actions d'un dépôt public sont visibles de tous : le script n'y écrit que des dates, des heures et des oui/non, jamais le texte de la page ni une trace d'erreur complète (`resume_erreur`).
  - Pas d'artefact (`upload-artifact`) : il serait public. Les captures d'écran de diagnostic partent uniquement par Pushover (notification de test ou de panne).

## Fonctionnement

- `.github/workflows/dentiste.yml` : déclenché uniquement par `workflow_dispatch` (option `test` qui envoie une notification de test avec capture d'écran ; cochée par défaut pour les lancements manuels). `concurrency` : un seul passage à la fois.
- Planification : **externe**, via cron-job.org, qui appelle `POST https://api.github.com/repos/<propriétaire>/<dépôt>/actions/workflows/dentiste.yml/dispatches` avec le corps `{"ref":"main","inputs":{"test":false}}` et un jeton GitHub fine-grained (ce dépôt seul, permission Actions : Read and write) dans l'en-tête `Authorization: Bearer …`. Réponse attendue : 204. Ne pas remettre de `schedule:` GitHub, qui sautait la plupart des exécutions.
- `surveille.py` : ouvre `URL_RDV` avec Playwright (Google Chrome préinstallé sur le runner, `channel="chrome"`), clique sur « Première visite » (anciennement « Vous n'avez jamais consulté » ; étape ignorée si absente) puis « Premier RDV », textes cherchés par regex insensible à la casse et à l'apostrophe. Délai d'attente Playwright : 10 s.
  - Pour une « Première visite », le site peut proposer le « Premier RDV » d'un autre praticien du cabinet que celui de la page : c'est ce calendrier qui est surveillé.
  - Planning complet : le site affiche « Aucune disponibilité — Le planning du praticien est à présent complet… » (le script lit alors « Prochain RDV : aucun », sans alerte).
  - Le calendrier n'affiche que la semaine en cours. Si elle est complète, le site affiche « Prochain rdv disponible à partir du JJ/MM » (regex `PROCHAIN`) : le script lit cette date, puis clique sur le message pour ouvrir la semaine concernée et lire l'heure.
  - Heures : plages d'ouverture retirées (« 09:00 - 12:00 », regex `PLAGE`), puis occurrences de chaque heure `HH:MM` comptées. Ne pas filtrer des heures par valeur : un créneau à l'heure d'ouverture a déjà été manqué ainsi.
  - Alerte si la date du prochain RDV se rapproche (un créneau s'est libéré), ou si, à date égale, une heure apparaît plus souvent qu'au passage précédent. Pas d'alerte quand la date recule (créneau pris). Filet de sécurité : alerte aussi quand « Aucune disponibilité » (regex `COMPLET`) était affiché au passage précédent et ne l'est plus, même si aucune date ni heure n'est reconnue.
- État entre deux exécutions : dossier `.etat/`, conservé via le cache Actions (`actions/cache/restore` + `save`).
  - `empreinte.txt` : JSON `{"prochain": "AAAA-MM-JJ" ou null, "heures": {heure: occurrences}, "complet": true/false}` du passage précédent.
  - `panne.txt` : présent quand une panne a déjà été signalée ; une seule alerte par panne (avec capture d'écran), puis une notification « rétablie ».
- Notifications Pushover : `ttl` de 7 jours (effacées automatiquement des appareils ensuite).
- Secrets requis : `URL_RDV`, `PUSHOVER_TOKEN`, `PUSHOVER_USER`.

## Contraintes

- Dépôt public : minutes GitHub Actions illimitées. Une exécution dure ~25 s. La fréquence est réglée sur cron-job.org.
- Le jeton GitHub utilisé par cron-job.org expire : s'il expire, les appels renvoient 401 et plus rien ne tourne.

## Vérifier

- Onglet Actions → « Surveille dentiste » → Run workflow avec `test` coché : une notification « Ça marche ! » avec capture d'écran doit arriver.
- Dans les logs de l'étape `python surveille.py`, les lignes `Message « prochain rdv disponible » trouvé`, `Prochain RDV`, `Heures vues`, `« Aucune disponibilité » affiché` et `Alerte` montrent ce qui a été lu.
