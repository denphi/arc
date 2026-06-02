# Run Co-Scientist

## Description

Run the optional Co-Scientist tournament workflow for a research goal. The
ARC package keeps the upstream `Co-Scientist` clone untouched and calls it
through a lazy wrapper when full execution is explicitly requested.

## Inputs

- `goal`: Natural-language research goal.
- `n_initial`: Number of initial hypotheses to seed.
- `wall_clock_seconds`: Optional wall-clock cap.
- `execute`: Must be true to start the upstream supervisor.

## Output

A session summary containing the Co-Scientist session id, artifact paths, and
the final overview location when a full supervisor run completes.

