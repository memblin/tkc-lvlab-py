# Release Process

Make changes, update the version, and PR the code.

When code hits `main` branch create and push a version matching tag and the workflow should do the rest — `.github/workflows/build-release.yml` triggers on tag push, builds a wheel with `uv build`, and uploads the artifact to a GitHub release.

## Version bump

The project uses the `uv_build` PEP 517 backend; there is no `uv version`
sub-command analogous to `poetry version`. Edit the `version` field in
`pyproject.toml` directly:

```toml
[project]
name = "tkc-lvlab"
version = "0.2.5"   # bumped from 0.2.4
```

Semver as a reminder: patch (0.2.4 → 0.2.5), minor (0.2.4 → 0.3.0), major (0.2.4 → 1.0.0).

After the edit, regenerate the lock file so the wheel build picks up the new
version cleanly:

```bash
uv lock
```

PR the version-bump changes in like normal and get merged into the `main` branch.

Once it's merged pull the `main` branch local and create the release tag, then push it. **The tag must match the `version` field in `pyproject.toml` exactly** or the release artifact filename won't line up with what the workflow looks for.

```bash
# Create the v0.2.5 release by adding tag 0.2.5
git tag -m 'v0.2.5' 0.2.5

git push --tags
```
