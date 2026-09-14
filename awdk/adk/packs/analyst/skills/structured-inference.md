# Skill: Structured Inference

A procedure for using zero-shot foundation models (TabFM, TimesFM) to make
predictions on tabular data and time series without training.

## When to use

When you have structured data (CSV, table) and need to classify rows, predict
a numeric value (regression), forecast a time series, or spot anomalies.

## Procedure

1. **Understand the problem.** Ask:
   - What is the target variable? (the thing you want to predict)
   - Is it classification (category), regression (number), or forecasting (time)?
   - What features do you have? (columns in the table)
   - Do you have examples of correct answers? (labeled rows for a support set)

2. **Curate a support set.** If the user has 5–20 labeled examples, use them.
   Zero-shot works without examples, but few-shot (with examples) improves accuracy.
   - Pick diverse examples: include edge cases and different categories.
   - For time series, include data from different time periods.
   - Document your support set so the reader knows what grounded the model.

3. **Prepare the data.** Clean the dataset:
   - Check for missing values (NaN, empty cells, "N/A").
   - Check for outliers that might distort inference.
   - Ensure consistent data types (numbers are numbers, dates are dates).
   - Note any data quality issues — they often explain poor predictions.

4. **Run inference.** Call:
   - `structured_inference(model="tabfm", task="classification", data=table, support_set=examples)`
   - `structured_inference(model="timesfm", task="forecast", time_series=data, horizon=12)`

5. **Inspect the results.** Look at:
   - Predicted values and confidence scores. Are scores high (confident) or low (uncertain)?
   - Distribution of predictions. Are they concentrated in one class or spread?
   - Outliers. Rows with unusual predictions might be edge cases or data errors.

6. **Validate against ground truth.** If you have held-out labels:
   - Compute accuracy (classification), R² or RMSE (regression), MAPE (forecasting).
   - Document performance. "Model achieved 85% accuracy on held-out data."

7. **Iterate or deliver.** If performance is poor:
   - Try a different support set.
   - Engineer new features from existing columns.
   - Reformulate the problem (maybe it's not classification but regression?).
   If performance is good, write the report.

## Quality bar

- Never claim certainty from a zero-shot model. Probabilities are estimates.
- Always inspect the raw data before and after inference.
- Document your support set, hyperparameters, and validation metrics.
- State confidence and limitations honestly: "Model achieves 80% accuracy on
  historical data but may drift on new customer segments."
