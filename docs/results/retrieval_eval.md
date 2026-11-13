# Retrieval eval (45 memories, ollama:nomic-embed-text)

## Recall (fast path)

| reranker | found | precision | negatives abstained | p50 | p95 |
|---|---|---|---|---|---|
| none | 31/36 (86%) | 59% | 3/7 | 11 ms | 21 ms |
| minilm | 30/36 (83%) | 61% | 4/7 | 48 ms | 68 ms |
| qwen | 33/36 (92%) | 79% | 6/7 | 449 ms | 500 ms |

found = expected memories returned (abstain cutoff off, so this is ranking quality). precision = share of returned items that were expected. negatives = questions with no stored answer that abstained at the default cutoff (verifier LLM off).

<details><summary>none: 8 misses</summary>

- Can I cook a satay with groundnut sauce for them? → missed allergy; top: The user drinks an oat-milk flat white every morning.
- What should I avoid putting in their food? → missed allergy, diet; top: The user drinks an oat-milk flat white every morning.
- What does the user do to stay fit? → missed gym; top: The user is vegetarian.
- Who do I contact about problems with the flat? → missed landlord; top: The user's flat is at Kastanienallee 12, Berlin.
- What is the user's favourite football team? → should abstain; returned: The user's favourite band is Radiohead.
- Who is the user's dentist? → should abstain; returned: The user's landlord is Herr Schulz, who prefers email.
- What is the user's mother's name? → should abstain; returned: The user's name is Ananya Rao.
- Which cloud provider hosts Project Hermes? → should abstain; returned: Project Hermes is an internal chat bot for HR questions.

</details>

<details><summary>minilm: 8 misses</summary>

- Can I cook a satay with groundnut sauce for them? → missed allergy; top: The user drinks an oat-milk flat white every morning.
- What should I avoid putting in their food? → missed allergy, diet; top: The user drinks an oat-milk flat white every morning.
- What does the user do to stay fit? → missed gym; top: The user is vegetarian.
- Who do I contact about problems with the flat? → missed landlord; top: The user's flat is at Kastanienallee 12, Berlin.
- Which language is the user picking up? → missed learning; top: The user's main programming language is Go.
- What is the user's favourite football team? → should abstain; returned: The user's favourite band is Radiohead.
- What is the user's mother's name? → should abstain; returned: The user's name is Ananya Rao.
- Which cloud provider hosts Project Hermes? → should abstain; returned: Project Hermes is an internal chat bot for HR questions.

</details>

<details><summary>qwen: 4 misses</summary>

- Can I cook a satay with groundnut sauce for them? → missed allergy; top: The user drinks an oat-milk flat white every morning.
- What does the user do to stay fit? → missed gym; top: The user drinks an oat-milk flat white every morning.
- Who do I contact about problems with the flat? → missed landlord; top: The user's flat is at Kastanienallee 12, Berlin.
- Which cloud provider hosts Project Hermes? → should abstain; returned: Project Hermes is an internal chat bot for HR questions.

</details>

## Forget candidates

| method | found | would forget (correct) | would forget (wrong) | judge | p50 |
|---|---|---|---|---|---|
| old: key + recall | 3/9 | 3/9 | 0/16 | - | 12 ms |
| new: qwen + LLM judge | 9/9 | 8/9 | 0/16 | llm | 11151 ms |

found = memories the request covers that reached the candidate list. wrong = look-alike memories (listed as keep) that confirm would delete. Old method: confirm deletes every candidate.

<details><summary>old: key + recall: 6 notes</summary>

- forget where I live → would NOT forget city_new (never found)
- forget where I live → would NOT forget address (never found)
- delete everything about my health → would NOT forget medication (never found)
- delete everything about my health → would NOT forget doctor (never found)
- delete everything about my health → would NOT forget allergy (never found)
- forget how much I earn → would NOT forget salary (never found)

</details>

<details><summary>new: qwen + LLM judge: 2 notes</summary>

- forget where I live → would also forget 1 unlisted memories
- delete everything about my health → would NOT forget doctor

</details>

