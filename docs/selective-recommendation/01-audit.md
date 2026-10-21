# Recommender audit (October 2026)

## Scope

- **Code audited:** branch `audit/reliability-followup` at `3f69ca4`, which includes tranches 2a–4. `main` is still the initial version.
- **Method:** every statement below comes from reading the code at that commit, not from the tranche checklist.

## How a Discover recommendation is produced today

| Step | Where | What it does |
|---|---|---|
| **Candidates** | `recommender.feed_candidates`, `api/products.feed` | Active listings. Excludes: anything you decided on, anything skipped in the last 10 days, other listings of a garment you decided on or skipped (except after a dislike given only for the price), seed items. Browse filters then apply. |
| **Photos and attributes** | `ingestion/pipeline`, `services/image_select` | One useful garment photo per product by default. Size charts, text graphics and repeated banners are excluded from the product vector. Attributes are parsed from titles and descriptions: colour, category, fibre, fit, logo level, brand, price, sizes. |
| **Representation** | `preference_model.build_feature_space` | One feature space. It holds: a written-profile score, used as a prior with a learned trust weight; one-hot colour, category, fibre, fit, logo level and brand; 16 whitened principal components of the image embedding (lite 192-d colour/texture/shape, or CLIP); and a bias. |
| **Your preferences** | `preference_model.fit` | Bayesian logistic regression with a *diagonal* Laplace posterior, refitted on every feed request. Only the feature space is cached. |
| **Feedback → training data** | `preference_model.observations` | The latest decision per product. Weights: love 2, like 1, purchased 1.5, maybe 0.3 (positives); dislike 1. A rejection reason restricts learning to the matching attributes; non-taste reasons teach no taste. Listings of one garment share one decision's weight. Inspiration photos and rated wardrobe pieces count too. A background sample of undecided items acts as weak "not chosen" observations (total weight up to 2, scaled by positive evidence). |
| **Similarity** | `services/dedup`, `recommender.rank_products` | Perceptual hash plus embedding cosine for photo matching and families. A cosine of 0.96 or more to a rejected item suppresses near-duplicates. |
| **Quality and seller** | `services/quality.assess`, `rank_products` | Garment quality uses only independent evidence on construction and material. Seller reliability is separate (reputation status plus priority). Value compares price with the category median and flags cheaper identical listings. |
| **Families** | `dedup.recompute_groups` | Union-find over strong relations that respects "not the same" decisions → `Product.group_id`. |
| **Feed** | `rank_products`, `collapse_families`, `build_feed` | A score combines taste (weight 3) with quality, value, wardrobe and seller components (formula below). One slot per garment family. The top ~82% are taken as ranked; ~18% are Thompson-sampling exploration picks, interleaved. Re-ranked after every decision. |
| **Logging** | `FeedRequest`, `Impression`, `Feedback.context` | Every feed served is logged with engine version, candidate count, and each item's score, position, exploration flag and family size. Displayed cards are logged at the server's position. Each decision stores its feed id and position. Undo deletes the decision row. |
| **Evaluation** | `eval/simulate.py`, `eval/real.py` | Synthetic personas give precision@10. `eval/real.py` uses uniformly sampled, blinded taste grades from the Evaluate page and reports NDCG, P@10 and bootstrap intervals. It refuses to report below 40 grades. |

Feed score: `0.5 + Σ_c w_c·g_c·(component_c − 0.5) / Σ_c |w_c|·g_c`.

## Do any current scores mean "probability you'll approve it"? No.

- **Taste component.** `σ(mean logit)` comes from a weighted pseudo-likelihood that mixes several things:
  - real decisions, at weights 2, 1, 1.5 and 0.3;
  - background items that were never shown, which are not rejections;
  - inspiration and wardrobe pseudo-observations.

  So it measures how much an item resembles what you chose rather than typical items, under those weights. It is not `P(Y = 1 | shown and rated)`, and it has never been checked against outcomes.
- **Uncertainty.** `taste_sd` comes from a diagonal posterior. It ignores correlations between features: colour, category and image components are strongly correlated. So it is not a reliable item-level interval.
- **Final feed score.** A weighted average of components on different scales. It orders items; it is not a probability.
- **Interface.** Two displays read like probabilities but are not:
  - the product card shows "match 87" (100 × the taste component);
  - the quality panel shows "Design compatibility" on a 0–100 scale.

## Data available for probabilistic learning

| Source | Labels | Bias | In this environment |
|---|---|---|---|
| Discover decisions on displayed items | approve (love/like/purchased) or reject (dislike, with optional reasons); "maybe" is ambiguous | Shown by an earlier ranking: selection bias. Exposure is logged only since 3b; older decisions have no impression link. | **None.** No real user database here. |
| Evaluate-page grades | taste 0–3, plus flags for owning something similar, price, unclear quality | Uniform random sample of readable, undecided pieces: representative of the catalogue | **None.** |
| Inspiration photos and wardrobe ratings | approve/reject of a look, not of a listing | Chosen by you | **None.** |

The app is single-user. So "shrink toward broader patterns" can only mean two things:

- shrinking toward your written profile;
- shrinking toward effects shared across categories.

There is no population of other users to pool from.

## Retain, extend, replace, repair

| Component | Decision | Why |
|---|---|---|
| Ingestion, photo selection, embeddings | **Retain** | Reliable, efficient, measured. The approval model reuses stored embeddings and needs no new downloads. |
| Bayesian logistic taste model (features, reason masks, family weighting, deleted-product snapshots) | **Retain as the raw compatibility score** | Proportionate to the data a single user produces. Fixes R1–R3 are in place. |
| Diagonal Laplace posterior | **Replace with full covariance** | Item-level uncertainty must account for correlated features. Cost is a d×d inverse with d ≈ 50–400. |
| Logo rejection also teaching brand (`REASON_GROUPS["logo"] = {logo, brand}`) | **Repair** | Rejecting a loud logo should not reject the brand. |
| Scores shown as percentages ("match 87") | **Replace** | Show an approval estimate only when calibrated, rounded, with its basis. Otherwise show none. |
| Feed policy "top-k plus exploration, always full" | **Extend with a selective gate** | Add per-item eligibility from a calibrated, uncertainty-aware approval estimate. Permit an empty feed. |
| Exploration mixed into the main feed | **Extend** | Keep it clearly labelled in Balanced mode. Move it to a separate Learning section, with logged inclusion probabilities, in the strict modes. |
| Insights "top picks" ranking every product | **Repair** | It is a second recommendation surface, so it must use the same gate. |
| Impression and feed logging | **Extend** | Record the policy, mode, calibration version and each item's gate decision and estimates. |
| `eval/real.py` | **Extend** | Calibration, precision–coverage, learning progression and ablations, on real labels only. |
| Garment quality and seller reliability | **Retain separate; use as eligibility only** | Known poor quality, sellers with concerns, and predictable price or redundancy objections become gate constraints. Unknown quality stays unknown. |
