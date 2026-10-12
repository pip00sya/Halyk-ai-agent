# Halyk: inspect the plan, calculation and supporting transaction

The challenge required reasoning across synthetic banking documents and a
transaction ledger. Halyk's [official leaderboard record](official-result.json)
is separate from the known-public-data calibration. The root README links both
the bank's event page and original final spreadsheet.

## The stages answer different failure modes

1. **Inventory and preflight:** identify required files, hash PDF inputs,
   extract pages and detect insufficient text before using a model.
2. **Version resolution:** distinguish applicable final documents from drafts,
   rejected versions and superseded material.
3. **Semantic planning:** express source facts, references, formulas and
   candidate transaction evidence in a constrained structured result.
4. **Independent review:** check the plan against the selected evidence and
   material exceptions rather than accepting the first answer automatically.
5. **Deterministic calculation:** use Decimal and a limited expression language,
   with rounding at the output boundary.
6. **Counterfactual support:** check whether removing the candidate transaction
   changes the relevant verdict.
7. **Output validation:** match the required submission keys, types and allowed
   transaction references before writing the result.

The local Command Center provides overview, configuration, results and project
documents. It is an operator/inspection surface; the evaluated competition
artifact is the agent's JSON, not an attractive dashboard screenshot.

## Read the artifacts in order

| Artifact | Purpose |
| --- | --- |
| Preflight report | Establish input readiness and required-file conditions |
| Document manifest | Identify the exact hashed input archive |
| Semantic plans | Inspect model-selected facts, source references and formulas |
| Decision trace | Follow the result through calculation and evidence support |
| Quality report | Inspect validation gates and the output-file hash |
| `submission.json` | The challenge's final output contract |

The public source includes the initial implementation and known-public fact
package. That package is enabled only for an exact dataset fingerprint. Its
36/36 calibration does not prove performance on unseen private-stage documents.
The bank's independent final result is **26/160 ranked teams, 81.6688%**.

## Reproduction has a data prerequisite

The [public source](https://github.com/pip00sya/Halyk-ai-agent) includes the agent,
dashboard, scorer, validator and operator documentation. Its
[development guide](https://github.com/pip00sya/Halyk-ai-agent/blob/main/DEVELOPMENT.md)
preserves the full Russian setup, preflight, mode and artifact instructions.
This initial checkout does not include the challenge PDF archives. The
[public Eye172 full-history copy](https://github.com/Eye172/halyk-hackathon-1)
contains the later code, 200 public-stage PDFs, 302 private-stage PDFs and retained
outputs, as checked on 12 October 2026. “Private-stage” identifies the competition
stage, not the current visibility of that repository.

Its migration test/CI record applies to that updated version, not automatically
to the earlier source snapshot. This case-study update adds presentation material;
it does not duplicate the dataset archives or claim a fresh evaluation run.

## What the new recording establishes

The 30-second tour was captured on 12 October 2026 from an isolated local copy
of the initial public source. It shows real navigation and configuration views,
an empty result matrix and the available documents. Captured browser frames were
assembled into a silent walkthrough, with each view held long enough to read.
It neither invokes a provider nor claims a fresh calibration/leaderboard score.
