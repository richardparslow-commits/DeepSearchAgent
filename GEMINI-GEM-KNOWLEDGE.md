# DeepSearchAgent — Gem Knowledge Pack

This file is the reference library for the DeepSearchAgent Gemini Gems. It holds all the reference data the Gems rely on: the claim element library, governing statute table, case catalog, coverage-score formula, outcome classification rules, and the output template. Upload it to a Gem's **Knowledge** section and pair it with one of the prompt variants below.

## SETUP — how to upload and pair this knowledge pack

The Gems' prompts (`GEMINI-GEM.md` for the one-shot Gem, `PROGRESSIVE-GEM.md` for the step-by-step Gem) contain only methodology, step workflow, rules, and output-format instructions. Every reference data set they use lives in **this file** — the prompts cite it by section number (1–7), so keep the section headers below unchanged.

**Step 1 — Create or edit the Gem.** In Gemini, open Gem manager → **New Gem** (or edit an existing Gem).

**Step 2 — Upload this file as the Gem's knowledge.**
- In the Gem editor, find the **Knowledge** section.
- Click **Add / Upload files** and select `GEMINI-GEM-KNOWLEDGE.md`. (Alternatively, paste this file's full contents into a Knowledge text entry.)
- Save the Gem. The element library, statute table, case catalog, coverage formula, outcome rules, and output template are now available to every turn.

**Step 3 — Pair it with a prompt variant.** This knowledge file is shared by both prompts; you only upload it once per Gem.
- **One-shot Gem** (full report in a single response): paste the entire contents of `GEMINI-GEM.md` into the Gem's **Instructions** (system prompt) field.
- **Progressive Gem** (8 steps with a **proceed** checkpoint after each): create a second Gem (or edit the same one) and paste the entire contents of `PROGRESSIVE-GEM.md` into **Instructions**, leaving the same knowledge file uploaded.
- Switching between one-shot and progressive behavior means swapping the Instructions text — the knowledge file stays put.

**Step 4 — Verify the pairing.** Ask the Gem: "Do you have the knowledge pack loaded?" It should name the six reference sections (element library, statute table, case catalog, coverage formula, outcome rules, output template). If the Gem says the reference data is missing, confirm the file is uploaded to the Gem's **Knowledge** section (not just saved in a chat), or paste the file contents into the Knowledge text entry. The prompt will ask for the knowledge file if it is not present rather than running without it.

---

## 1. CLAIM ELEMENT LIBRARY

Every VA claim issue maps to some subset of these 9 elements. The Gem detects elements by scanning the issue for detection phrases, then uses the weight, description, guidance, and step fields in the analysis.

### service connection
- weight: 0.30
- phrases: "service connection", "service-connected"
- description: "The Caluza elements: a current diagnosed disability, an in-service event or aggravation, and a nexus linking the two."
- guidance: "Assemble (1) a current diagnosis, (2) evidence of the in-service event, and (3) a medical nexus opinion connecting the two."
- step: "Map the record to the three Caluza elements (diagnosis, in-service event, nexus) and identify which element is contested."

### nexus
- weight: 0.40
- phrases: "nexus", "medical opinion", "caused by service", "related to service"
- description: "A medical or factual link between the current disability and military service."
- guidance: "Obtain a supporting medical nexus opinion that addresses the 'at least as likely as not' standard."
- step: "Secure a nexus opinion that explicitly applies the 'at least as likely as not' standard."

### benefit of the doubt
- weight: 0.10
- phrases: "benefit of the doubt", "reasonable doubt", "equipoise", "approximate balance"
- description: "When the positive and negative evidence is in approximate balance (nearly equal, including but not limited to equipoise), the benefit of the doubt is given to the veteran (38 U.S.C. § 5107(b); Lynch v. McDonough, 21 F.4th 776, 781 (Fed. Cir. 2021) (en banc))."
- guidance: "Highlight issues where favorable and unfavorable evidence is nearly equal (an approximate balance), and request that the benefit of the doubt be applied; do not require exact equipoise."
- step: "Identify issues where the evidence is in approximate balance (nearly equal) and argue the benefit of the doubt."

### reasons and bases
- weight: 0.05
- phrases: "reasons and bases", "adequate explanation", "statement of reasons"
- description: "The Board must provide an adequate statement of the reasons and bases for its decision (38 U.S.C. § 7104(d)(1))."
- guidance: "Point out any findings the Board failed to explain or evidence it failed to address."
- step: "Check the decision for findings that lack reasons-and-bases support."

### evidence evaluation
- weight: 0.05
- phrases: "evidence", "competent evidence", "lay evidence", "medical evidence"
- description: "Evidence must be competent, credible, and properly weighed under VA standards."
- guidance: "Gather competent evidence supporting the claim, and challenge any improper weighing of lay or medical evidence."
- step: "Audit whether VA weighed the competent and lay evidence properly."

### presumption
- weight: 0.03
- phrases: "presumption", "presumptive"
- description: "Presumptive service connection may apply to certain conditions for qualifying veterans."
- guidance: "Confirm whether the condition and service history qualify for any presumptions."
- step: "Check whether presumptive service connection applies."

### rating
- weight: 0.02
- phrases: "rating", "evaluation", "schedular"
- description: "Disability evaluation applies the rating schedule to the symptoms and severity shown in the record."
- guidance: "Compare the veteran's symptoms against the rating criteria to argue for the appropriate evaluation."
- step: "Compare the symptoms against the rating schedule criteria."

### aggravation
- weight: 0.02
- phrases: "aggravation", "aggravated"
- description: "A pre-existing condition aggravated by service may be service-connected."
- guidance: "Document the pre-service baseline and the worsening during service."
- step: "Document the pre-service baseline and the in-service worsening."

### duty to assist
- weight: 0.03
- phrases: "duty to assist"
- description: "VA has a duty to assist the claimant in developing evidence relevant to the claim."
- guidance: "Identify records or exams that VA failed to obtain or provide."
- step: "Identify any evidence that VA failed to help develop."

## 2. GOVERNING STATUTE TABLE

### 38 U.S.C. § 1110
- element: service connection
- description: "Basic wartime service connection: a current disability resulting from personal injury or disease incurred or aggravated in active military service."
- outcomes from case law: mostly favorable

### 38 U.S.C. § 1131
- element: service connection
- description: "Peacetime service connection — mirrors § 1110 for non-wartime service periods."
- outcomes from case law: mostly favorable

### 38 U.S.C. § 5107(b)
- element: benefit of the doubt
- description: "When the positive and negative evidence is in approximate balance (nearly equal, including but not limited to equipoise), the benefit of the doubt is given to the claimant; the rule does not apply when the evidence persuasively favors one side (Lynch v. McDonough, 21 F.4th 776, 781 (Fed. Cir. 2021) (en banc))."
- outcomes from case law: uniformly favorable (this statute structurally benefits the claimant)

### 38 U.S.C. § 7104(d)(1)
- element: reasons and bases
- description: "The Board's decision must include a written statement of the reasons or bases for the findings and conclusions."
- outcomes from case law: mixed (remanded when inadequate, affirmed when adequate)

### 38 U.S.C. § 5103A
- element: duty to assist
- description: "VA shall make reasonable efforts to assist a claimant in obtaining evidence necessary to substantiate the claim."
- outcomes from case law: mixed

### 38 U.S.C. § 1153
- element: aggravation
- description: "A pre-existing injury or disease aggravated by active service is service-connected for the degree of aggravation."
- outcomes from case law: case-specific

### 38 C.F.R. § 3.303
- element: service connection, evidence evaluation
- description: "Principles relating to service connection: chronicity, continuity of symptomatology, and the types of evidence that establish a claim."
- outcomes from case law: mostly favorable on the framework; individual cases vary

### 38 C.F.R. § 3.304(f)
- element: service connection
- description: "Tinnitus and hearing loss: when associated with acoustic trauma or noise exposure in service, a current diagnosis plus a history of in-service noise exposure can establish service connection."
- outcomes from case law: favorable when noise exposure is documented

### 38 C.F.R. § 3.159
- element: duty to assist, evidence evaluation
- description: "Duty to assist regulation: what VA must do to help the claimant develop evidence."
- outcomes from case law: mixed

### 38 C.F.R. § 3.306
- element: aggravation
- description: "Aggravation of a pre-service disability: the baseline and the worsening must both be documented."
- outcomes from case law: case-specific

### 38 C.F.R. Part 4
- element: rating
- description: "Schedule for Rating Disabilities — the criteria for assigning disability percentages."
- outcomes from case law: case-specific (depends on symptoms matching criteria)

### 38 C.F.R. § 3.350
- element: service connection, rating
- description: "Special monthly compensation (SMC) for aid and attendance, housebound status, and loss of use."
- outcomes from case law: case-specific; highly fact-dependent

## 3. CASE CATALOG

Each case listed below is a real decision in your training data; check its `precedential` field before treating it as binding. Cite only cases you are confident about.

### Federal Circuit (precedential decisions bind all veterans courts)

#### Shedden v. Principi, 381 F.3d 1163 (Fed. Cir. 2004)
- elements: service connection, presumption
- statutes: 38 U.S.C. § 105(a), 38 U.S.C. § 1110
- holding: Section 105(a) creates a presumption that a disability incurred or aggravated during active duty is service-connected. The Veterans Court's contrary statement was erroneous, but the error was harmless because the CUE claim failed on an independent ground.
- outcome: unfavorable (affirmed)
- appellant: veteran
- posture: affirmed
- precedential: yes

#### Lynch v. McDonough, 21 F.4th 776 (Fed. Cir. 2021) (en banc)
- elements: benefit of the doubt
- statutes: 38 U.S.C. § 5107(b)
- holding: The benefit-of-the-doubt rule applies when the positive and negative evidence is in "approximate balance" — "nearly equal," which includes but is not limited to equipoise. It does not apply when the evidence persuasively favors one side.
- outcome: unfavorable (affirmed)
- appellant: veteran
- posture: affirmed
- precedential: yes

#### Buchanan v. Nicholson, 451 F.3d 1331 (Fed. Cir. 2006)
- elements: nexus, duty to assist, reasons and bases
- statutes: 38 U.S.C. § 7104(d)(1), 38 U.S.C. § 5103A
- holding: The Board must explain why it rejected favorable evidence; failure to do so violates § 7104(d)(1). VA's duty to assist includes obtaining examinations when warranted.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Walker v. Shinseki, 708 F.3d 1331 (Fed. Cir. 2013)
- elements: service connection, evidence evaluation
- statutes: 38 C.F.R. § 3.303(b), 38 C.F.R. § 3.309(a)
- holding: Continuity of symptomatology under § 3.303(b) is available only for chronic diseases enumerated in § 3.309(a); other chronic conditions must be evaluated under the standard three-element test in § 3.303(a). The court abrogated CAVC decisions extending § 3.303(b) more broadly.
- outcome: unfavorable (affirmed)
- appellant: veteran
- posture: affirmed
- precedential: yes

#### Davidson v. Shinseki, 581 F.3d 1313 (Fed. Cir. 2009)
- elements: reasons and bases
- statutes: 38 U.S.C. § 7104(d)(1)
- holding: The Board's statement of reasons and bases must be adequate to enable meaningful judicial review; conclusory statements are insufficient.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Hensley v. West, 212 F.3d 1255 (Fed. Cir. 2000)
- elements: evidence evaluation, benefit of the doubt
- statutes: 38 U.S.C. § 5107(b)
- holding: Lay evidence is competent to establish symptoms observable by a layperson; the Board cannot dismiss lay evidence without explanation.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Jandreau v. Nicholson, 492 F.3d 1372 (Fed. Cir. 2007)
- elements: evidence evaluation, service connection
- statutes: 38 U.S.C. § 1110, 38 C.F.R. § 3.303
- holding: Lay evidence may be competent and sufficient to establish a diagnosis when (1) a layperson is competent to identify the condition, (2) the layperson is reporting a contemporaneous medical diagnosis, or (3) lay testimony describing symptoms at the time supports a later diagnosis by a medical professional. Whether lay evidence is competent and sufficient is a fact issue for the Board. Lay evidence does not substitute for medical evidence where diagnosis or causation is beyond lay competence.
- outcome: favorable (reversed and remanded)
- appellant: veteran
- posture: reversed and remanded
- precedential: yes

#### Charles v. Principi, 93 F. App'x 217 (Fed. Cir. 2004)
- elements: service connection
- statutes: 38 U.S.C. § 1110, 38 C.F.R. § 3.304(f)
- holding: Tinnitus can be service-connected when there is evidence of in-service noise exposure and a current diagnosis.
- outcome: favorable
- appellant: veteran
- posture: vacated and remanded
- precedential: NO (non-precedential memorandum — note this when citing)

### CAVC (precedential decisions bind the Board; persuasive on the Federal Circuit)

Precedential CAVC decisions (three-judge panel or en banc) bind the Board and VA in future cases. Non-precedential memorandum decisions and single-judge actions are persuasive only and may not be cited as precedent under U.S. Vet. App. R. 30(a).

#### Gilbert v. Derwinski, 1 Vet. App. 49 (1990)
- elements: reasons and bases
- statutes: 38 U.S.C. § 7104(d)(1)
- holding: Established the standard for adequate reasons and bases — the Board must account for all evidence and explain why it reached its conclusions.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Caluza v. Brown, 7 Vet. App. 498 (1995)
- elements: service connection
- statutes: 38 U.S.C. § 1110
- holding: Established the three-element Caluza framework: (1) current disability, (2) in-service event or aggravation, (3) nexus linking them.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Stefl v. Nicholson, 21 Vet. App. 120 (2007)
- elements: evidence evaluation, duty to assist
- statutes: 38 U.S.C. § 5103A, 38 C.F.R. § 3.159
- holding: A medical nexus opinion that denies service connection solely because the condition is not presumptively service-connected, without considering direct service connection, is inadequate. An adequate opinion must address the relevant facts and medical science and provide a reasoned analysis the Board can weigh against contrary opinions.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Barr v. Nicholson, 21 Vet. App. 303 (2007)
- elements: duty to assist
- statutes: 38 U.S.C. § 5103A
- holding: VA's duty to assist is not satisfied by merely notifying the veteran — VA must actually help obtain records and may need to provide examinations.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Hickson v. West, 12 Vet. App. 247 (1999)
- elements: evidence evaluation
- statutes: 38 C.F.R. § 3.303
- holding: Competent lay evidence includes statements about observable symptoms and their timing; the Board must provide reasons for discounting such evidence.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

#### Colvin v. Derwinski, 1 Vet. App. 171 (1991)
- elements: evidence evaluation, reasons and bases
- statutes: 38 U.S.C. § 7104(d)(1)
- holding: The Board may not reject independent medical evidence or substitute its own unsubstantiated medical judgment for it; where a determination requires medical expertise, the Board must identify competent medical evidence supporting its findings.
- outcome: favorable (reversed and remanded)
- appellant: veteran
- posture: reversed and remanded
- precedential: yes

#### Washington v. Nicholson, 21 Vet. App. 191 (2007)
- elements: service connection, evidence evaluation
- statutes: 38 C.F.R. § 3.303(b)
- holding: Continuity of symptomatology under § 3.303(b) requires evidence that symptoms have persisted from service to the present; lay statements alone may suffice.
- outcome: favorable (vacated and remanded)
- appellant: veteran
- posture: vacated and remanded
- precedential: yes

### Known VA-Law Principles (extracted from case holdings)

These are principles the case catalog above establishes. Cite them in the "Applicable principles" section when relevant cases are invoked:

- "Service connection requires a current disability, an in-service event, and a nexus linking the two — the Caluza framework." (Caluza v. Brown, 7 Vet. App. 498)
- "When the positive and negative evidence is in approximate balance (nearly equal, including but not limited to equipoise), the benefit of the doubt is given to the veteran under 38 U.S.C. § 5107(b); the rule does not apply when the evidence persuasively favors one side." (Lynch v. McDonough, 21 F.4th 776, 781 (Fed. Cir. 2021) (en banc))
- "The Board must provide an adequate statement of reasons and bases for its decision under 38 U.S.C. § 7104(d)(1)." (Gilbert v. Derwinski, 1 Vet. App. 49; Buchanan v. Nicholson, 451 F.3d 1331)
- "Lay evidence is competent to establish symptoms observable by a layperson; the Board cannot dismiss lay evidence without explanation." (Hensley v. West, 212 F.3d 1255)
- "Lay evidence may also be competent and sufficient to establish a diagnosis when the layperson can identify the condition, reports a contemporaneous medical diagnosis, or describes symptoms supporting a later medical diagnosis; sufficiency is a fact issue for the Board." (Jandreau v. Nicholson, 492 F.3d 1372, 1377)
- "The Board may not substitute its own medical judgment for independent medical evidence; where a determination requires medical expertise, competent medical evidence is required." (Colvin v. Derwinski, 1 Vet. App. 171, 175)
- "VA has a duty to assist the claimant in developing evidence relevant to the claim under 38 U.S.C. § 5103A." (Barr v. Nicholson, 21 Vet. App. 303)
- "Presumptive service connection may apply for qualifying conditions and service histories." (38 U.S.C. §§ 1112–1118; 38 C.F.R. §§ 3.307–3.309)
- "Tinnitus may be service-connected on evidence of in-service noise exposure and current diagnosis under 38 C.F.R. § 3.304(f)." (Charles v. Principi, 93 F. App'x 217)

## 4. COVERAGE SCORE FORMULA

coverage_score = sum of weights of elements supported by at least one cited case holding / sum of weights of all detected elements

Confidence heuristic:
- high: 10+ cases, coverage score at least 0.60, some with source holdings, at least one binding Federal Circuit or precedential CAVC decision, and at least one element supported by multiple cases
- medium: 5+ cases with coverage at least 0.30, or 3–4 cases including binding precedent with coverage at least 0.30
- low: fewer than 5 cases, all cases from a non-binding (BVA or non-precedential) source, or coverage below 0.30

## 5. OUTCOME CLASSIFICATION RULES

### Appellant-role requirement
The favorable and unfavorable lists below require an explicit appellant role. If the appellant is unknown or was not extracted, classify the outcome as unknown (0); do not assume the veteran appealed. "Affirmed" means opposite things depending on which party appealed.

### Favorable outcomes (count as +1)
- granted: the claim was awarded
- vacated: a denial was set aside
- remanded: the case was sent back for further proceedings (presumptively favorable — the Board must reconsider)

### Unfavorable outcomes (count as -1)
- denied: the claim was rejected
- affirmed WHEN the veteran appealed: the denial was upheld
- dismissed WHEN the veteran appealed: the appeal was rejected

### Secretary-appellant flip rule
When an explicitly identified Secretary is the appellant, read every disposition from the Secretary's point of view and invert it:
- **affirmed**: the Board's grant to the veteran was upheld on appeal = favorable
- **dismissed**: the Secretary's appeal was dismissed, leaving the veteran's grant in place = favorable
- **denied**: the Secretary's appeal was denied, leaving the veteran's grant in place = favorable
- **granted**: the Secretary's appeal was granted and the veteran's grant was reversed = unfavorable
- **vacated**: the Secretary's appeal vacated the veteran's grant = unfavorable
- **remanded**: the Secretary's appeal remanded the veteran's grant, so it is not final = unfavorable

### Unknown (count as 0)
- an empty or unknown appellant role
- mixed outcomes (e.g., "granted in part and denied in part")
- partial dispositions that cannot be resolved to one direction
- unrecognized outcome language
- enriched vs. text-scanned discrepancy

## 6. OUTPUT TEMPLATE

The final report (Step 8 in the progressive Gem, or the only output in the one-shot Gem) must use this exact structure:

```
Issue: [the issue]

Run id: simulated

Top cases:
- [case], [citation] ([court])

Summary:
[3–5 sentences with inline case citations]

How this affects VA claims:
[4–8 sentences explaining claim strategy, evidentiary pressure points, what the Board will look for]

Applicable principles:
- [principle] ([case])

Contradictions:
- [statement] ([Case A] vs [Case B])
(or "Contradictions: (none)")

Next steps:
- [one per element]
- Compare the facts of the current claim to the cited authorities and identify the closest analog.
- This output is research support derived from public decisions, not legal advice.

Strengths:
- Retrieved authority addresses '[element]': [cases].

Gaps:
- No retrieved authority directly addresses '[element]'; consider a targeted search.

Statute-outcome matrix:
  Statute                                                     Court          Fav  Unf  Unk
  [60-char left-aligned]                                       [15-char]       [N]   [N]   [N]

Coverage score: [0.00–1.00] (confidence: [low/medium/high])

Interpretation source: training-knowledge

Note: This is a simulated analysis. The real DeepSearchAgent app performs live searches across CourtListener, search.usa.gov, and the BVA decision corpus. This analysis draws on training knowledge of published veterans-law decisions and may miss recent rulings or case-specific holdings. Always verify with live legal research.
```

**Progressive variant (PROGRESSIVE-GEM.md, Step 8):** apply these overrides to the template above:
- `Run id: progressive-simulated` instead of `simulated`
- Append the line `Steps completed: 8/8` after `Interpretation source: training-knowledge`
- Replace the closing Note with: `Note: This is a simulated analysis produced through an 8-step progressive reasoning process. The real DeepSearchAgent app performs live searches across CourtListener, search.usa.gov, and the BVA decision corpus. This analysis draws on training knowledge of published veterans-law decisions and may miss recent rulings or case-specific holdings. Always verify with live legal research.`

## 7. GENERIC NEXT STEPS (always appended at end)

Regardless of the claim issue, always end the Next steps section with these two items:

1. "Compare the facts of the current claim to the cited authorities and identify the closest analog."
2. "This output is research support derived from public decisions, not legal advice."