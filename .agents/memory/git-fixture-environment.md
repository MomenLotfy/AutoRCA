---
name: Git fixture environment
description: Environment-specific behavior affecting tests that commit .env files.
---

Tests that create real Git repositories and commit `.env` files can fail in this
environment because the system Git configuration applies `/etc/.gitignore`,
which excludes `.env` globally.

**Why:** A real-Git fixture may report “nothing to commit” even when the test
created and staged `.env`; the repository's own `.gitignore` is not the cause.

**How to apply:** Run the suite with `GIT_CONFIG_NOSYSTEM=1` when validating
real Git fixtures, or explicitly force-add `.env` in a fixture that must be
independent of host Git configuration.