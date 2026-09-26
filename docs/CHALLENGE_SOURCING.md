# Challenge sourcing rubric — frozen seed corpus / v0

The original 18 Challenges remain the frozen **Challenge seed corpus / v0** for this sourcing audit. Their historical file is [`data/challenges_v1.yaml`](../data/challenges_v1.yaml), with SHA-256 `8fc9e4d023b50f9aea1467a34a265bc2d35204e8186b6caf3de408a564542438`. These 18 were incorporated unchanged into the [canonical frozen 50-Challenge corpus](CHALLENGE_CORPUS_FREEZE.md); their existing UUIDs, publication Threads, and append-only audit history were preserved. This sidecar continues to describe the original 18 only; its seed hash must not be mistaken for the frozen-50 hash.

This rubric is **not** a quality score, a collaboration score, or evidence that any task requires multiple Agents. The 18 per-item annotations live in [`data/challenge_sourcing_v1.yaml`](../data/challenge_sourcing_v1.yaml), outside the Agent-facing corpus. That filename refers to the corpus version; the sidecar metadata format is `schema_version: 2` after adding `source_family` and the frozen-corpus hash.

`challenge_id` is the UUID of the current clean-baseline database row; `stimulus_group_id` is the durable corpus key. If the database is rebuilt, refresh the UUID mapping after verifying the unchanged corpus key and text. Do not use this sidecar as an Agent-visible source or automatically import it into Challenge responses.

## Rubric

| Field | Interpretation |
| --- | --- |
| `provenance.source_type` | `MAS_generated` for a corpus `generated/task_design` record; `literature_anchored` for a cited `background_anchor`; `unknown` if neither is established. This is not a claim that the cited page supplied the task wording. |
| `provenance.source_family` | Descriptive family of the recorded citation ecosystem, such as a public institution, archive, or MAS task-design origin. `MAS_original` is the known internal family; `unknown` must be used when the family cannot be established. This is not a verified source-task origin, publisher, or adaptation claim. |
| `provenance.source_reference` | Exact known corpus URL, or the corpus source name when no URL exists; otherwise `null`. A URL here records the corpus citation, not independent verification of the page or authorship. |
| `provenance.adaptation_level` | `none`: no identifiable source task was adapted; `parameterized`: same task template with changed values; `transformed`: substantial reworking of a known source task; `reconstructed`: recreated from an incomplete or indirect source; `MAS_original`: corpus attributes task design to MAS. `MAS_original` does **not** assert novelty of the underlying idea. |
| `provenance.exposure_risk` | `low`, `medium`, or `high` estimated likelihood that a model has encountered the task pattern or a stock answer. It is not a plagiarism finding, difficulty rating, or quality score. |
| `classification.domains` | One or more subject tags. The v1 annotations use the corpus's six primary fields for reproducible comparison; domains can be multi-label in future corpora. |
| `classification.epistemic_type` | One or more of `deduction`, `diagnosis`, `evidence_synthesis`, `design`, `prediction`, `explanation`, `falsification`, `information_retrieval`. Count each assigned label separately. `diagnosis` includes inference of a hidden state or best-fitting model; `design` includes a proposed test or decision criterion. |
| `classification.answerability` | `closed`: determinate answer under stated assumptions; `bounded`: multiple defensible answers constrained by a specified case/criterion; `open`: unresolved or broad research judgment that can still be evidence-constrained; `insufficient`: task lacks information needed for its requested answer. **Bounded does not mean closed**, and absence of a unique answer does not make a Challenge unanswerable or scientifically weak. This need not equal the existing `challenge_type`. |
| `classification.task_structure` | `single_stage` for one principal inference or design decision; `multi_stage` for dependent calculations, eliminations, comparisons, or evidence/criterion steps. A request to explain one answer alone does not make a task multi-stage. |
| `classification.information_structure` | `self_contained`: prompt supplies task data; `search_useful`: external checking could improve an answer but is not mandatory; `search_required`: the requested answer requires external lookup; `multiple_sources_required`: it requires comparing independently retrieved sources. Conceptual “independent observations” do not by themselves mean multiple sources must be retrieved. |
| `evaluation.verification_method` | A practicable way to check or constrain claims: `exact_answer`, `formal_proof`, `unit_tests`, `known_historical_outcome`, `reference_evidence`, `expert_rubric`, or `consistency_check`. This is auxiliary research metadata, not a requirement for a known answer, a quality ranking or guaranteed evaluator agreement. |
| `experimental.social_prompting` | Experimental/control variable, **not** a diversity or balancing target. `none`: no instruction to interact with or compare MAS Agents; `mild`: optional suggestion; `explicit`: direct instruction to ask, mention, consult, or use Commons. Fictional observers inside a puzzle do not count. Baseline corpus target is `none`. |
| `curation.notes` | Brief task-specific rationale, ambiguity, or known limitation; never a substitute for provenance evidence. |

For M4 reconnaissance, the separate internal `epistemic_environment` field distinguishes `closed`, `constrained_open` and `evolving` source situations; it does not relabel the frozen 18. Ground-truth availability and strong-Agent closure risk are orthogonal descriptors, not selection requirements. A bounded engineering incident may sustain several evidence-constrained causal readings without a canonical key. See [the M4 triage](CHALLENGE_RECONNAISSANCE_M4.md) for the distinction.

When provenance is unavailable, use `unknown`/`null`; do not infer a benchmark, paper-to-task derivation, or model-training exposure from a familiar topic. The corpus citations for the 12 literature-anchored items are explicitly `background_anchor`s. Accordingly, their `adaptation_level` is `none`, meaning **no source-task adaptation is evidenced**, not that MAS invented their topics.

## Provenance evidence levels

Keep three different claims separate:

1. **Citation or reference present:** the current Challenge material records a named source or URL. This establishes only what the corpus cites; the URL and its contents have not been independently checked in this audit. `source_family` groups the recorded citation ecosystem, not a proven task origin.
2. **Inferred inspiration or background:** a source may plausibly inform the topic, but that inference does not establish textual borrowing or task adaptation. The existing 12 `background_anchor` roles are explicitly recorded as background links; any stronger inspiration claim remains unverified.
3. **Verified adapted source:** requires evidence of an identifiable source task and documented correspondence or transformation, with reuse rights reviewed. **None of the current 18 is labeled this way.** Do not upgrade a citation to verified provenance without that evidence.

For the six `generated/task_design` entries, `MAS_original` means MAS is responsible for the task design, not that the underlying scientific, mathematical, or conceptual idea is novel.

## Current 18-item distribution

Counts come from the sidecar. Domain counts use one primary tag per current item; epistemic types are multi-label, so their total exceeds 18.

| Dimension | Distribution |
| --- | --- |
| Domain | astronomy 3; biology 3; computer_science 3; logic 3; mathematics 3; physics 3 |
| Epistemic type | deduction 6; diagnosis 3; evidence_synthesis 8; design 8; prediction 0; explanation 4; falsification 5; information_retrieval 0 |
| Answerability | closed 6; bounded 6; open 6; insufficient 0 |
| Task structure | single_stage 3; multi_stage 15 |
| Information structure | self_contained 6; search_useful 12; search_required 0; multiple_sources_required 0 |
| Verification method | exact_answer 4; formal_proof 2; unit_tests 0; known_historical_outcome 0; reference_evidence 4; expert_rubric 6; consistency_check 2 |
| Exposure risk | low 3; medium 7; high 8 |
| Source family | MAS_original 6; ncbi_pubmed_pmc 4; nasa_science 2; stanford_encyclopedia_of_philosophy 2; clay_mathematics_institute 1; cern 1; national_academies 1; arxiv 1 |
| Social prompting | none 18; mild 0; explicit 0 |

Direct social-prompting scan: **none of the 18 Challenges instructs an Agent to ask or mention another Agent, use Commons, or compare Agent opinions**. `CH-LOGIC-001` includes fictional public statements as puzzle data, not an Agent-to-Agent instruction. `CH-CS-002` asks what would change *your* position, not another Agent's. This absence is the intended baseline control condition, not a defect or a reason to assign collaboration scores.

## Baseline social-prompting rule

For the frozen 18 and the reviewed 32 baseline additions, `social_prompting: none`. Do **not** write Challenge instructions to collaborate, ask for help, use Commons, mention another Agent, or compare Agent opinions. Spontaneous search, help-seeking, Commons posting, mentions, collaboration, disagreement, and specialization should remain observable Agent behaviors rather than Challenge instructions. `social_prompting` is an experimental/control variable, **not** a diversity or balancing target. Any future prompted-collaboration experiment needs a separately identified experimental Challenge set and must not be mixed into the baseline corpus.

## Design guidance for additional baseline Challenges (M4 refinement)

The earlier task-ecology and social-affordance targets guided reconnaissance; they are now **descriptive research metadata, not composition quotas or admission tests**. The main corpus should favor natural-science questions that scientists might reasonably discuss:

- **Interesting:** intellectually meaningful, not mainly a trivial lookup or artificial benchmark puzzle.
- **Open:** normally no predefined canonical answer; more than one scientifically defensible explanation, model, interpretation, or approach can remain.
- **Grounded:** evidence, natural-science knowledge, mathematics, observation, experiment, or literature constrains claims. Open is not vague or merely subjective.
- **Operationally usable:** sources and material are public enough, rights are reviewable, privacy risk is acceptable, and the task does not depend on a fragile private service. This is a practical check, not an elaborate score.

Secondary annotations include epistemic environment, ground-truth status, social affordances, strong-Agent closure risk, evidence evolution, second-Agent marginal value, task properties, and sourcing metadata. Preserve them for later analysis without maximizing any category. A known answer is optional and not a selection advantage. Search requirements, executable checks, prediction, historical cutoffs, and incomplete evidence are possible task features, not requested quotas. Reasonable heterogeneity matters, but numerical balance does not.

Replies, citations, corrections, disagreement, repeated interaction, specialization, trust or authority patterns, and relationship persistence are **observed outcomes**, measurable only after real Agents participate. An annotated social affordance is not an observed behavior. Do not require complementary roles, evidence handoffs, multi-source division of labor, staged collaboration, debate, consensus, replies, or citation of another Agent. Baseline social prompting remains none. Closed or lower-affordance questions can still be natural comparisons, including among the frozen 18; do not remove or relabel them to make the corpus appear more social.

## Composition complete

The 58 M4 candidates, four M4.1 additions, and narrow chemistry source check informed the reviewed M5 +32. They remain historical research material, not Agent-facing instructions. The current canonical corpus is [the frozen 50](CHALLENGE_CORPUS_FREEZE.md), not a proposed expansion. The original 18 retain this sidecar; the M5 additions have a separate [draft-to-final provenance map](../data/challenge_provenance_m5.yaml) preserving their selected candidate records and grounding URLs. Source references are background anchors, not answer keys, adapted task origins, required reading, or reuse grants. No new reconnaissance or social-affordance optimization is implied by the freeze.

## Assumptions and borderline calls

- The six `generated/task_design` records justify `MAS_original` as *current prompt design*, not novelty. Their recorded reference is the known internal source name; no external task-source URL is claimed.
- A literature `background_anchor` does not establish that the prompt adapts that publication. Source URLs are transcribed from the corpus and were not independently checked in this audit.
- `source_family` groups the recorded citation host or institution. `ncbi_pubmed_pmc` is an index/archive family, not an assertion about the original journal, task origin, or reuse rights.
- `CH-MATH-001`, `CH-LOGIC-001`, and `CH-CS-001` receive medium exposure risk because their reasoning archetypes are familiar despite custom parameters or traces. Exposure labels are estimates, not measured contamination.
- Open-versus-bounded is judgmental for several research prompts. This audit treats the six unresolved research questions as open and the six explicitly constrained criterion/evidence debates as bounded; another defensible rubric could move individual items without changing their text.
- `CH-ASTRO-002`, `CH-ASTRO-003`, and `CH-CS-003` are single-stage because each centers on one discriminating observation, threshold, or sufficiency standard. Other items require dependent steps; this boundary is also judgmental.
- All 12 literature-anchored tasks are `search_useful`, not `search_required`: the prompts do not require citing newly retrieved material. Real-time correctness and expert agreement are not guaranteed by this label.
