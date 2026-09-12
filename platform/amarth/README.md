# Amarth

> "Behind that, there was something else at work, beyond any design of the ring
> maker. I can put it no plainer than by saying that Bilbo was meant to find the
> ring, and not by its maker. In which case you also were meant to have it. And
> that may be an encouraging thought."

The mountain represents the ultimate destination of a causal chain that began
thousands of years prior. Sauron chose to forge the Ring there because of the
mountain's innate power; therefore, by the laws of Middle-earth's spiritual
physics, the Ring was bound to that specific fire for its end.

## Time-windowed ESKG inference

Amarth accepts an immutable `ObservationWindow` containing scalar state values,
multiple dense embedding modalities, and existing ESKG relationships. All
evidence is aligned to the window's fixed bucket cadence. Dense vectors remain
packed during ingestion and are projected to a bounded number of PCA components
only when the numerical discovery matrix is assembled.

Temporal discovery delegates to Tigramite PCMCI. Amarth applies
Benjamini-Hochberg false-discovery-rate correction and emits one `CausalLink`
per directional dependency with:

- `confidence_score`, derived from corrected statistical confidence and
  cross-window stability;
- `lag_steps` and `time_lag_seconds`;
- effect size, p-value, q-value, discovery method, and ESKG node provenance;
- `supports_counterfactual`, indicating that the resulting graph is a DAG that
can be fitted as a DoWhy structural causal model.

PCMCI is the only observational discovery algorithm in Amarth. The former
NOTEARS, DirectLiNGAM, and Peter-Clark adapters were removed because ESKG
inference is explicitly time-ordered. Static input must provide an existing
ontology or analytical `prior_graph`; Amarth will not invent directionality
when temporal evidence is absent.

Heterogeneous estimation uses EconML `LinearDML` with analytic inference,
bounded cross-fitting, single-threaded LightGBM learners, and at most 16 PCA
embedding components by default. DoWhy subset refutation is limited to five
refits instead of its default 100. PCMCI evaluates at most 32 variables, 16
windows, and five conditioning dimensions by default. These limits are
configurable when a larger offline budget is desired.

DoWhy confidence-interval bootstrapping is disabled by default because its
implicit simulation count is unsuitable for the online path. Call
`DowhyEstimator(confidence_interval_simulations=N)` to opt into an explicitly
bounded bootstrap.

## Example

```python
from datetime import UTC, datetime, timedelta

from amarth import AmarthRouter, Observation, ObservationWindow

start = datetime.now(UTC) - timedelta(minutes=10)
window = ObservationWindow(
    start=start,
    end=start + timedelta(minutes=10),
    bucket=timedelta(seconds=1),
    observations=(
        Observation(
            observation_id="vision-1",
            graph_node_id="event-face-1",
            observed_at=start,
            observation_type="FacialExpressionShift",
            scalar_values={"confidence": 0.94},
            embeddings={"facial_embedding": (0.1, 0.2, 0.3)},
        ),
    ),
)
result = AmarthRouter().analyze_observation_window(
    window,
    target_outcome="TextSentimentChange.sentiment",
)
```
