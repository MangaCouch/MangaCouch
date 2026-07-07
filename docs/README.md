# MangaCouch — documentation

| File | What it is | Read it when… |
|------|------------|---------------|
| [`design-spec.md`](design-spec.md) | ⭐ The **self-contained v1 build spec**: scope, requirements, architecture, dependency choices, phased plan, and the e-hentai protocol essentials (Appendix A). | You want to understand what the system is supposed to be. |
| [`decisions.md`](decisions.md) | The **decision log** (Q1–Q11 + two feedback rounds): what was decided and *why* — stack, search, acquisition model, storage, auth, packaging. | You're about to revisit or question an architectural choice. |
| [`feedback.md`](feedback.md) | The maintainer's **raw feedback** that resolved the open questions (referenced by the decision log). | You need the original wording behind a decision. |
| [`requirements.md`](requirements.md) | The **full feature wishlist** (traceability superset) with P0/P1/P2 priorities — including items deliberately deferred or dropped. | You're planning what to build next. |
| [`ehentai-protocol.md`](ehentai-protocol.md) | Deep **e-hentai/exhentai protocol notes** (auth cookies, archiver flow, H@H, image limits, GP economics) beyond what Appendix A carries. | You're extending the acquisition layer. |

Historical research that was **not** carried over (prior-art survey, the 11-file LANraragi
baseline analysis) can be re-derived from the upstream LANraragi source itself; the day-to-day
build/test commands live in the repo root [`CLAUDE.md`](../CLAUDE.md).
