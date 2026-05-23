## Summary

<!-- 1-3 bullets describing the change. Be specific about files/areas touched. -->

## Why

<!-- Motivation, linked issue, or the user-facing problem this solves.
     If it's a refactor, what was wrong with the prior shape? -->

## What changed

<!-- Optional: list of notable code-level changes if the diff isn't self-evident.
     Skip for small, obvious PRs. -->

## Test plan

<!-- How to verify locally. Check what you actually ran. -->
- [ ] `uv sync --group dev`
- [ ] `uv run pytest` (or note why a test isn't applicable)
- [ ] `pre-commit run --all-files`
- [ ] Manual smoke against a real libvirt host if VM/cloud-init paths changed

## Risk

<!-- Anything that could break in unexpected ways. Especially:
     - destructive paths (`destroy`, `down`, snapshot delete)
     - libvirt domain naming / multi-environment coexistence
     - cloud-init template changes that ship to real VMs
     - release/version bumps (tags on main cut a GitHub release)
     - changes to checksum/GPG verification logic -->
