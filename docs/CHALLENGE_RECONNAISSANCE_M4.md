# M4 Challenge source reconnaissance — internal candidate pool

Status: source reconnaissance only, checked 2026-09-24. The 18-item Challenge seed corpus / v0 remains frozen. This document and [the 58-candidate sidecar](../data/challenge_reconnaissance_m4.json) are research/curation material, not Agent-facing Challenges or instructions. No candidate has been selected for the planned +32.

## Current design guidance: keep three layers separate

The primary design principles for the main corpus are **interesting, open, grounded, and operationally usable**. Prefer natural-science questions that merit scientific attention, admit more than one defensible explanation or approach, and are constrained by observations, theory, mathematics, experiments, or literature. “Open” does not mean vague; a canonical answer or score is not required. Check public access, rights, privacy, and practical stability without turning convenience into a score. Preserve reasonable heterogeneity and some natural comparison conditions, including the frozen 18.

The fields below are **secondary research annotations**, not Challenge admission rules or targets to maximize: epistemic environment, ground-truth status, social affordances and triage, strong-Agent closure risk, evidence evolution, second-Agent marginal value, task properties, and sourcing metadata. They describe possible conditions and can later help explain different outcomes. The earlier M4 and M4.1 screens deliberately investigated social affordances; their counts and gap language remain an audit trail, not the current design objective. No Challenge needs complementary roles, a handoff, or a multi-Agent workflow.

**Observed outcomes** are measured only after real Agents participate: replies, citations, corrections, disagreement, repeated interaction, specialization, trust or authority patterns, and relationship persistence. An affordance is an annotation; behavior is an outcome. Agents may answer independently, ignore one another, interact, or do nothing. Baseline social prompting remains none: do not ask Agents to debate, collaborate, reach consensus, reply, or cite one another.

At a high level, the 58 M4 plus 4 M4.1 candidates provide enough starting material to **stop reconnaissance and proceed to final Challenge composition**. This is not approval of 62 prompts: most still require a specific record or phenomenon, source and rights checks, and original question wording. The final +32 are not selected here.

## Purpose and reading the sidecar

MAS Challenges present questions that Agents can investigate independently in a shared environment. They are not primarily benchmark items, and neither answer keys nor designed collaboration are required. Ground truth can sometimes help later research, but the central design question is whether a scientifically interesting, open inquiry can be grounded in evidence. Interaction, if any, is an Agent choice and an observed outcome.

Each `situation_summary` describes a source-supported data capability or documented case **plus a possible situation to investigate**; it is not a polished prompt or a claim that a particular record has already been chosen. `why_it_may_be_useful_for_mas` records the initial permissive hypothesis; the new `social_structure_rationale` and `triage_categories` record the earlier conservative human-triage judgment where they differ. None is a source fact or observed Agent behavior. The nine `social_affordances` fields use `plausible`, `limited`, or `uncertain`; `one_shot_answer_risk` uses `limited`, `possible`, or `substantial`. These are qualitative observations, **not scores, rankings, inclusion criteria, or evidence that collaboration is required**. An Agent may always answer independently or do nothing.

`triage_categories` may overlap: `structurally_social` means the described evidence or work product itself permits heterogeneous, corrective, or cumulative contributions; `potentially_social` requires a suitable concrete case before that claim is justified; `low_affordance_comparison` is a useful naturally low-interaction situation; `benchmark_like` mainly tests individual skill; and `operationally_awkward` flags substantial access, rights, tooling, privacy, safety, or availability friction. A theoretical second answer or generic correction is insufficient for `structurally_social`. `evidence_evolution: true` records a credible partial-evidence → interpretations → additional evidence → persistence, revision or correction pathway, not merely a forecast or changing webpage. Prediction may occur inside that pathway but is neither necessary nor the primary objective. It is a source-structure hypothesis, not an observed social process or a quota.

`operational_practicality` separates public linked-page access, account need, machine access, record-level rights clarity, tooling burden, and privacy/high-stakes risk. `public_page_or_metadata` does **not** certify that an underlying dataset is downloadable or reusable; `likely_for_metadata` does not certify an API or permit automated access. `record_level_review_needed` is intentionally not a rights grant. These provisional labels come from the already recorded source capabilities and limitations; no new source or legal audit was performed in this triage. `triage_notes` state the remaining gate for each entry.

`task_properties` and `epistemic_type` are overlapping secondary descriptors. `answerability_notes` are conditional on a future case, data snapshot, and wording; they are not classifications of final Challenges. `source_family` identifies the **primary source ecosystem** for concentration checks, consistent with [the sourcing rubric](CHALLENGE_SOURCING.md). Additional URLs may come from other families. A source-family label does not prove origin or adaptation.

`provenance_confidence: portal_verified_record_pending` means the linked source page and its relevant capability were checked, but no specific task instance, dataset record, or reuse permission has been approved. `case_verified_task_not_adapted` applies only to the NIST Champlain Towers findings: the documented case exists, but no MAS task has been adapted from it. Neither label claims that the source supplied Challenge wording. URLs were checked through public web pages/search results, not by downloading every dataset or independently validating every underlying record. Future reviewers must verify exact record-level URLs, source dates, access and license terms, and whether quotation or redistribution is permitted.

## Pool coverage

There are **58 unselected situations** from **45 primary source families**. Thirteen families appear twice and 32 appear once; none appears more than twice. These are candidate counts, not sourcing quotas.

| Domain | Candidates |
| --- | ---: |
| data analysis | 8 |
| engineering | 7 |
| astronomy, computer science, history | 6 each |
| biology, language, mathematics, physics, social science | 5 each |

Primary source families represented twice: `cdc_wonder`, `fhwa_nbi`, `gwosc`, `mast_tess`, `ncbi_geo`, `nist_codata`, `noaa_storm_events`, `oeis`, `rfc_editor`, `sqlite`, `uk_hansard`, `universal_dependencies`, and `us_census_acs`. The other 32 families appear once. This is family breadth at the **portal/case reconnaissance** stage, not proof of independent evidence for each future task.

| Secondary task property | Candidates where plausible |
| --- | ---: |
| `search_required` | 46 |
| `multiple_sources_required` | 28 |
| `executable_artifact` | 31 |
| `information_retrieval` | 12 |
| `incomplete_or_noisy_evidence` | 39 |
| `prediction` | 9 |
| `historical_cutoff` | 8 |

These labels overlap and are **not** target quotas. For example, a later forecast case needs a frozen information cutoff and verification point; a portal mentioning forecasts is not enough. Similarly, an executable artifact needs a small reproducible fixture and bounded dependencies, not merely downloadable data.

The conservative re-audit retains 58 provisionally independently answerable situations. Only **11** have `plausible` partial contributions and cumulative progress; 11 also have plausible evidence disagreement (not necessarily the same 11). The remainder are `limited` or `uncertain` on those fields. Plausible correction fell from 57 to 25, evidence disagreement from 49 to 11, partial contribution from 52 to 11, and cumulative progress from 36 to 11. These are annotation changes, not changes to source evidence or measured Agent behavior. Seven deliberately retained situations have `substantial` one-shot-answer risk: `M4-MATH-01`, `M4-PHYS-01`, `M4-CS-03`, `M4-ENG-07`, `M4-LANG-02`, `M4-LANG-03`, and `M4-HIST-03`. They provide natural comparison cases; no Agent-facing item should call itself a control.

## Conservative human triage

| Internal category | Count | Interpretation |
| --- | ---: | --- |
| `structurally_social` | 11 | Distinct evidence, checks, or durable partial work are supported by the described situation. |
| `potentially_social` | 39 | Case choice determines whether complementary contributions actually exist. |
| `low_affordance_comparison` | 8 | Mostly lookup or one-shot comparison conditions. |
| `benchmark_like` | 21 | Mainly individual retrieval, computation, diagnosis, or forecast evaluation. |
| `operationally_awkward` | 26 | Meaningful practical friction; not a scientific rejection. |

Categories overlap: 7 structurally social and 18 potentially social entries are also operationally awkward; 12 potentially social and 8 low-affordance entries are benchmark-like; one structurally social optimization case is benchmark-like; one low-affordance rights lookup is operationally awkward; 7 benchmark-like entries are operationally awkward. **17** entries have `evidence_evolution: true`, without a quota or an assumption that all are suitable for the first expansion. The 11 structural entries are `M4-MATH-05`, `M4-PHYS-04`, `M4-ASTRO-02`, `M4-ASTRO-04`, `M4-BIO-01`, `M4-BIO-04`, `M4-ENG-01`, `M4-ENG-02`, `M4-DATA-08`, `M4-HIST-01`, and `M4-HIST-04`.

The strongest recurring structure is **different evidence bearing on one claim** (strain quality/waveforms, light curve/pixels, sample metadata/expression, occurrence quality/effort, reports/gauges, archives/newspapers), plus **durable partial artifacts** (solution bounds or orbit fits). Correction and revisitation are meaningful where later observations or findings can change a specific earlier interpretation. This is still provisional: except for the documented NIST case, most entries are portals rather than selected record packets.

Many others failed the stronger test because a portal describes a capability but no contested record; a fixed lookup or code fixture has a single natural completion point; multiple methods merely cross-check the same individual answer; or a changing source lacks preserved versions and a genuinely revised interpretation. Prediction and historical cutoff remain secondary task properties, not proxies for social structure. Low-affordance cases are deliberately retained, not promoted to appear social.

Under the earlier social-affordance screen, the pool appeared thin on small rights-clear packets, persistent shared artifacts, and accessible multilingual cases. These remain useful descriptive observations, not reasons to engineer collaboration or prolong reconnaissance. Case-level access and rights still need review.

## Epistemic environment and strong-Agent closure — internal

**Bounded is not closed.** A case can have no unique final answer yet still constrain claims with timelines, measurements, public records, computation and reproducible analysis. Lack of a canonical answer key is not lack of scientific answerability. These fields describe the *possible environment if a concrete case is chosen*, not a completed Challenge or an Agent capability:

| Field | Meaning |
| --- | --- |
| `epistemic_environment: closed` | A substantially determinate result follows once the case, date and conventions are fixed. Useful low-interaction comparisons remain valid. |
| `epistemic_environment: constrained_open` | Multiple interpretations may remain defensible, but evidence and methods rule out unsupported claims. This is a major MAS-relevant form, not an inferior answerability class. |
| `epistemic_environment: evolving` | The available evidence changes over time; the interest may be persistence, revision, correction, disagreement, citation, trust or specialization, not forecast accuracy. |
| `ground_truth_status` | `available`, `partial`, `unavailable`, `not_applicable` or `future_or_evolving`. This is auxiliary descriptive metadata, never a quality score, selection gate or quota. An official conclusion need not be complete ground truth. |
| `strong_agent_closure_risk` | `low`, `medium` or `high`: could a sufficiently capable *single* Agent exhaust the useful substance in one contribution? It is an environment-structure judgment, not an Agent ability or difficulty score. A difficult fixed benchmark can have high closure risk. |

`epistemic_framing_note` gives the case-level rationale. The new labels are orthogonal to the prior social triage: a closed lookup can be a useful comparison; constrained-open or evolving cases can be structurally social; and an evolving historical forecast can still be benchmark-like. In this provisional pool, all 17 `evidence_evolution: true` entries are `evolving`, but that co-occurrence is not a general schema rule. These annotations do not prescribe Agent interaction.

| Environment | Count | Structurally social | Potentially social | Low-affordance comparison | Benchmark-like | Operationally awkward |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `closed` | 15 | 0 | 8 | 7 | 12 | 2 |
| `constrained_open` | 26 | 1 | 24 | 1 | 7 | 13 |
| `evolving` | 17 | 10 | 7 | 0 | 2 | 11 |

Triage columns overlap. Ground-truth status is `available` 15, `partial` 34, `future_or_evolving` 6, `unavailable` 1 and `not_applicable` 2. Strong-Agent closure risk is `high` 23, `medium` 26 and `low` 9. The pool is **not numerically dominated** by closed (15/58) or benchmark-like (21/58) situations. Nor are constrained-open (26) or evolving (17) situations numerically rare. The gap is *usable cases*: 24 of the 26 constrained-open entries remain only potentially social, and 11 of the 17 evolving entries are operationally awkward. A high closure-risk count of 23 also argues against equating task difficulty or source breadth with persistent social opportunity.

Only `M4-LANG-02` needed a prior answerability-note correction: license terms constrain the inquiry, but an Agent cannot supply a canonical legal reuse decision. No earlier social-affordance category was changed. The other conditional `answerability_notes` already used `bounded`, `open` or `insufficient` without requiring a standard answer.

## Source-quality and reuse concerns

- Most entries are **source-portal situations**, not record-level case dossiers. Human review must choose a real accession, event, document, version, or table before judging answerability or adaptation. The pool should not be converted mechanically into 58 prompts.
- [Universal Dependencies licenses](https://universaldependencies.org/contributing/licensing.html) vary by treebank; [TalkBank/CHILDES](https://talkbank.org/0share/rules.html) has access, citation, and reuse restrictions; [Pew datasets](https://www.pewresearch.org/datasets/) require account-based download; [CDC WONDER](https://wonder.cdc.gov/datause.html) has suppression and data-use constraints. Public visibility is not blanket permission to copy text or redistribute datasets.
- The [OEIS](https://oeis.org/wiki/The_OEIS_End-User_License_Agreement) and [LMFDB](https://www.lmfdb.org/api/options) have attribution/share-alike considerations. [ATLAS Open Data](https://opendata.cern.ch/docs/about-atlas) describes CC0 data but still requests dataset DOI citation. License checks are per actual material, not inferred from the host.
- Historical cutoffs and forecasts require archived source versions, anti-leakage review, and a predeclared outcome/scoring point. Provisional safety, health, and disaster data must not become operational advice or overconfident causal claims.
- Mathematical, software, and scientific data portals can have high exposure or stock-answer risk. The seven one-shot cases are intentionally present, but the final +32 should not become an exact-answer benchmark collection.

## High-level pool review and next gate

The combined 58 + 4 pool is sufficient to proceed toward the final +32. Promising question sources, not selected Challenges, span physical inference (for example gravitational-wave evidence and CODATA disagreements), astronomy (transit interpretation and candidate status), biology (expression claims, structural inference, species ranges, and specimen records), mathematics (competing derivations and incomplete database coverage), and earth, climate, and coastal observations. These can support natural questions about mechanisms, competing explanations, residual uncertainty, or discriminating observations. Their value depends on a real phenomenon and original wording, not on a structurally-social label. Engineering and data-analysis sources can contribute when the inquiry is genuinely scientific and safe.

Obvious poor fits for the main corpus are bare OEIS-prefix, constant, erratum-status, WALS-value, BLS-series, Hansard-date, and bridge-table lookups, plus fixed planner or conformance exercises and historical forecasts whose main point is scoring a correct answer. Such candidates can remain natural comparison conditions; neither existing annotations nor the frozen 18 should be removed or relabeled to make the corpus look more social.

Chemistry and materials science are the clearest major natural-science areas not represented by a named source family. This is a coverage observation, not a quota or reason to restart broad reconnaissance. If final composition exposes a genuine scientific gap, a narrow source check can be made then.

**Recommendation: stop reconnaissance now and move to final Challenge composition.** Choose concrete phenomena or cases from the existing pool where they serve interesting, open, grounded questions; verify exact sources, rights, and practical access; write original baseline wording with social prompting none; then conduct human review. Do not require packets built around complementary roles, multi-source division of labor, stages, replies, citations, or consensus. Do not select or publish the +32 in this documentation pass. No candidate is currently Agent-facing, and the frozen 18 and live data remain unchanged.
