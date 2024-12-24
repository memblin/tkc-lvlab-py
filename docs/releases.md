# Release Process

Make changes, update the version, and PR the code.

When code hits `main` branch create and push a version matching tag and the workflow should do the rest.

## Version bump with poetry

```bash
# Patch; This would take version 0.1.0 and make it 0.1.1
poetry version patch

# Minor; This would take version 0.1.1 and make it 0.2.0
poetry version minor

# Major; This would take version 0.2.0 and make it 1.0.0
poetry version major
```

PR the changes in like normal and get merged into the `main` branch.

Once it's merged pull the `main` branch local and create the release tag, then push it.

```bash
# Ceate the v0.2.0 release by adding tag 0.2.0
git tag -m 'v0.2.0' 0.2.0

git push --tags
```
