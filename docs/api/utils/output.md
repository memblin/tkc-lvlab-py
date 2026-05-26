# tkc_lvlab.utils.output

Shared CLI output helpers: one table style plus the TTY-vs-pipe gate
that human-facing commands (`status`, `init`, `smoke`,
`global show instances`) render through, so the whole CLI reads
consistently. Machine-facing output (`ssh-config`, `hosts`,
`cloudinit`, any `--format json|yaml`) deliberately does **not** route
through here — it stays raw so piping keeps working.

::: tkc_lvlab.utils.output
