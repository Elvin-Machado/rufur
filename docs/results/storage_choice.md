# Which storage fits which knowledge

Embedder: nomic-embed-text; top-k = 5. Cell = share of questions whose context held *all* the needed evidence (code: right answer) · average prompt tokens.

| knowledge | units | prompt | vector | fts | files | code | best | what RUFUR does |
|---|---|---|---|---|---|---|---|---|
| profile | 8 | 100% · 65 | 80% · 42 | 80% · 23 | 100% · 65 | n/a | **prompt** | profile → working set injected at session start (prompt) |
| atomic facts | 300 | 100% · 4948 | 100% · 82 | 100% · 81 | 100% · 17 | 100% · 31 | **files** | fact store → hybrid FTS + vector recall |
| tables | 200 | 100% · 2598 | 20% · 65 | 50% · 65 | 70% · 298 | 100% · 36 | **code** | typed_rows table (code/SQL), plus a fact sentence for recall |
| relations (2-hop) | 59 | 100% · 463 | 10% · 40 | 0% · 40 | 0% · 8 | 100% · 49 | **code** | graph edges (code walk), plus a fact sentence for recall |
| procedures | 72 | 100% · 1227 | 50% · 86 | 0% · 85 | 100% · 205 | n/a | **files** | markdown file + chunks (files), retrieved whole |
| history (changing values) | 90 | 33% · 945 | 25% · 52 | 33% · 52 | 33% · 36 | 100% · 45 | **code** | ledger with validity windows; recall returns current, recall_past the old |

Reading the table:
- **prompt** is always complete but its cost grows with everything you know; it only wins for the small profile that every request needs.
- **vector / fts** are cheap for single facts but miss evidence spread over many units (totals, all steps of a procedure, both hops of a relation) and return stale versions next to current ones.
- **files** win when knowledge is naturally grouped (a whole procedure) and the question names the group.
- **code** (SQL, graph walk, validity lookup) is exact and nearly free in tokens for aggregates, multi-hop and "what is true now", but needs structured storage written at capture time, and its token cost includes the schema and query the agent has to write (an LLM writing the wrong query is a failure mode this table does not measure).
