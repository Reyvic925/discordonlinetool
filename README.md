# discordonlinetool

## Deployment

The application requires one or more Discord account tokens to run. Do not
commit tokens to the repository. On Render, add a secret environment variable
named `DISCORD_TOKENS` in the service's **Environment** settings. Separate
multiple tokens with commas.

For local runs, either set `DISCORD_TOKENS` or create a `tokens.txt` file in
the project directory with one token per line. `tokens.txt` is intentionally
ignored by Git.