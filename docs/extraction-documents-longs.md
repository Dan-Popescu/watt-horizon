# Avis, annexes et documents longs

Les formulaires et décisions conservent leur priorité. Les avis et pièces complémentaires peuvent désormais combler une déclaration de puissance/capacité entièrement manquante : le passage doit identifier explicitement le projet et son objet, présenter des unités normalisables et porter une preuve à la page. Les valeurs contradictoires, phases, scénarios, équipements existants et couples MW/MWh sans citation commune restent inconnus. Cette règle volontairement prudente ne constitue pas un interpréteur général de tableaux ou d'études techniques.

La sélection optionnelle de passages (`RADAR_LLM_PASSAGES=1`) examine tout le texte, répartit le budget entre champs et positions du document, et donne aussi une chance aux dernières annexes malgré les répétitions du préambule. `plan_passages` expose les plages retenues, le nombre de caractères omis et les indices couverts. Une sélection ne certifie jamais que les passages omis ne contiennent rien d'utile. La lecture initiale reste soumise aux limites PDF/OCR configurées.

Le mode par défaut conserve l'entrée initiale de 20 000 caractères. L'activation de la sélection doit être comparée sur la campagne vérifiée avant généralisation. La version de requête et les empreintes de cache changent avec la politique de sélection. Les citations traversant artificiellement deux documents concaténés sont rejetées lors de l'attribution de la preuve.

Les tests utilisent des exemples construits pour les positions tardives, unités, contradictions et phases. Ils ne remplacent pas les annotations de véritables avis, annexes, scans et tableaux. Un second moteur OCR, une lecture structurée des tableaux et une interprétation par phase restent à comparer sur ce corpus avant adoption.
