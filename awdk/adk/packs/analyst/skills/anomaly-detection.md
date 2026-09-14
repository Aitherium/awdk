# Skill: Anomaly Detection

A procedure for finding unusual patterns, outliers, and drift in data using
zero-shot foundation models and statistical reasoning.

## When to use

When you want to spot unusual rows in a table, detect when a time series changes
behavior, or identify data quality issues that might break downstream analysis
or predictions.

## Procedure

1. **Define "normal".** Ask:
   - What does normal look like in your data? (typical ranges, patterns)
   - What kind of anomalies matter? (fraud, equipment failure, user churn signals,
     data errors)
   - Do you have labeled examples of anomalies? (ground truth for validation)
   - How sensitive should we be? (catch 90% of anomalies, or only the most obvious?)

2. **Analyze the baseline.** Compute statistics for each feature:
   - Mean, median, standard deviation, min, max
   - Percentiles (10th, 25th, 75th, 90th)
   - Note which features have outliers or long tails

3. **Run detection.** Call the zero-shot model:
   - `structured_inference(model="tabfm", task="anomaly", data=table, baseline=stats)`
   - The model flags rows that deviate from the baseline in unexpected ways

4. **Investigate flagged rows.** For each anomaly:
   - What feature(s) triggered the flag? (high value, unusual combination, drift?)
   - Is it a real anomaly (actionable) or a data error (needs cleaning)?
   - How many anomalies? (10 in a million-row table = rare; 10% = pervasive drift)

5. **Track time-series drift.** For time series:
   - Compare recent windows to historical baselines
   - Spot gradual drift (model accuracy declining over time)
   - Spot sharp changes (sudden spike, drop, or regime shift)

6. **Validate with domain knowledge.** Ask the user:
   - "Does this flagged row match anything unusual in your business?" (e.g., a
     known customer departure)
   - "When did this drift start?" (correlate with events: new feature, seasonal
     change, external shock)

7. **Report findings.** Document:
   - How many anomalies, what types
   - Which rows are most actionable
   - Recommended thresholds for automated flagging
   - Any data quality issues discovered

## Quality bar

- Distinguish real anomalies (actionable insights) from data errors (needs
  cleaning).
- Always show context: "This customer's spending is 3x the median but they're
  in retail (seasonal high season)" is different from "This customer spiked
  suddenly with no seasonal pattern."
- Don't flag rare but valid patterns as errors.
- Document your thresholds and assumptions so automated systems can be tuned.
