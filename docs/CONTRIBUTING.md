# Contributing to the project

We have a GitHub project to track planned work for this repo:

- [TKC Labs : Libvirt Labs](https://github.com/users/memblin/projects/3)

Pull Requests and reports of issues welcome.

## Branch naming

The branch naming approach I use when working on this repo is for reference. I won't enforce this same pattern for others interested in helping at this time. I make it available to help others who may be new to coding and perhaps a bit timid about jumping into an open project.

```bash
# Pull most recent on main branch
user@example01:~/repos/github/memblin/tkc-lvlab-py$ git pull main
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean

# Create new development branch for Issue #37
user@example01:~/repos/github/memblin/tkc-lvlab-py$ git checkout -b main.issue37
Switched to a new branch 'main.issue37'

# Do work and stage changes for commit
git add .

# Commit with conventional commit message
# https://www.conventionalcommits.org/en/v1.0.0/#summary
git commit -m 'docs: more contributor help info'

# Push to my repo
git push -u origin main.issue37

# I usually let commit trigger pre-commit but to do-so
# manually now and then I use
pre-commit run --all-files
```

## Tools

The project uses..

- [Poetry](https://python-poetry.org/docs/) for dependency management and
  packaging. Poetry install directions are in the docs linked here.

These are some common poetry commands:

```bash
# By default, Poetry creates a virtual environment in {cache-dir}/virtualenvs.
# Activate the poetry virtual env
poetry shell

# Add dependency; when needing a new library added to the project
poetry add

# To install the defined dependencies for your project, just run the install command.
poetry install

# Buiding; artifacts show up in ./dist
poetry build
```

- [pre-commit](https://pre-commit.com/)
  - hooks
      - id: check-yaml
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: black

```bash
# Install our pre-commit hooks in the repo after cloning
pre-commit install
```

- [Black - The uncompromising formatter](https://black.readthedocs.io/en/stable/)

```bash
# Run black on the whole project
black ./tkc_lvlab
```

## End-to-End Testing

Manual for now, make sure all operations still function after changes

```bash
# Capabilities command
lvlab capabilities

# Environment initialization
lvlab init

# Verify /etc/hosts file content rendering
lvlab hosts
lvlab hosts --heredoc

# Verify /etc/hosts file content update
# This will write to /etc/hosts so only run on ephemeral test machine
sudo lvlab hosts --append

# VM Operations
#
# Check the status
lvlab status
# Bring up salt.local
lvlab up salt.local
# Check that status agrees
lvlab status
# Verify cloud-init re-render works
lvlab cloudinit salt.local
# List snapshots when we know there aren' tany
lvlab snapshot list salt.local
# Create a snapshot
lvlab snapshot create salt.local Base
# List snapshots now that we know there is one
lvlab snapshot list salt.local
# Delete the snapshot
lvlab snapshot delete salt.local Base
# Shutdown salt.local
lvlab down salt.local
# Check that status agrees (may need to wait for shutdown)
lvlab status
# Bring salt.local up from down state
lvlab up salt.local
# Check that status agrees
lvlab status
# Destroy the running VM
lvlab destroy salt.local
# Check that status agrees
lvlab status
```
