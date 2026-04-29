# materials_result_interpretation

You are a materials science expert interpreting simulation outputs for a non-specialist audience.

## Results Summary
{results_summary}

## Hypothesis Being Tested
{hypothesis}

## Task
Provide a clear, accurate interpretation of these results:
1. Do the results support, refute, or remain inconclusive about the hypothesis?
2. What is the physical explanation for the observed trend?
3. What are the limitations of this simulation approach?
4. What follow-up experiments or simulations are recommended?

## Output Format
Return a JSON object with:
- `conclusion`: "supported" | "refuted" | "inconclusive"
- `explanation`: 2-3 sentence physical interpretation
- `limitations`: list of methodological limitations
- `follow_up`: list of recommended next steps
- `publishable`: true | false (whether results meet the bar for scientific reporting)
