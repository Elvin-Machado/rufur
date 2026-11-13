# 30 sessions + restart

Data directory: `/var/folders/03/qfknmcgn3vs7r1qycmvpcztc0000gn/T/rufur-30-tfzn_m0g`

- Sessions played: **30** (two agents: `claude-code` wrote, `opencode` reads after the restart)
- Memories before kill → after restart: **33 → 34**
- Full history sent with every question would cost **514 tokens**

| check | question | result | answer context | tokens | ms |
|---|---|---|---|---|---|
| update | Where does Asha live? | PASS | The user's home city is now Bangalore (moved there last month). | 127 | 1577.45 |
| update | Which database does Ledgerline use? | PASS | Ledgerline switched its database from Postgres to CockroachDB for multi-region support. / Ledgerline will be open-sourced under the Apache 2.0 license. | 386 | 21.98 |
| contradiction | When is the Ledgerline beta launch deadline? | PASS | The Ledgerline beta launch deadline is December 5. / Journal for Friday 13 November 2026: Asha, a backend engineer at Finly, discussed her side project Ledgerli | 257 | 18.82 |
| planted | Who is Asha's manager who approves deploys? | PASS | The user's manager is Ravi. / Ravi approves every production deploy. | 153 | 2135.44 |
| procedure | How do I deploy Ledgerline? | PASS | To deploy Ledgerline, the user recommends running tests, building the Docker image, pushing it to the registry, and then running the migration job. / Journal fo | 282 | 25.14 |
| table | How much was the Globex invoice? | PASS | The invoices so far are INV-101 for Acme amounting to 1200 USD and INV-102 for Globex amounting to 800 USD. | 138 | 25.4 |
| deleted | What is Asha's personal phone number? | PASS | I don't know (abstained) | 8 | 1072.85 |
| abstain | What is Asha's blood type? | PASS | I don't know (abstained) | 8 | 839.32 |
| abstain | What is the name of Asha's cat? | FAIL | The user's dog is named Bruno. / The user's name is Asha. | 149 | 24.15 |

- Checks passed: **8/9**
- Average recall context: **168 tokens** vs 514 for the full history (**67.4% less**)
- Deletion receipt `rcpt_05498546ce1d` after restart: chain valid, 1 receipt(s), resurfaced 0
- Recall latency: p50 25.4 ms, p95 2135.44 ms
