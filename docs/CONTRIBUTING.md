# Contributing to the project

We have a GitHub project to track planned work for this repo:

- [TKC Labs : Libvirt Labs](https://github.com/users/memblin/projects/3)

Pull Requests and reports of issues welcome.

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
