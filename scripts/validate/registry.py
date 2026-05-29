"""The declarative scenario catalog — the single source of truth for a run.

The report (JSON, human summary, and the GitHub issue write-up) is a direct
projection of this list. To add coverage, add a scenario here; nothing else in
the harness needs to change.

Static addresses are chosen to sit outside this project's documented default
network layout (v4 DHCP ``…122.200-254``; v6 DHCP ``fdfa:cade::150-254``), so
``.50/.51/.52`` and ``fdfa:cade::51`` are safe static picks. A run validates
the live network first (``--check-network``) before trusting them.
"""

from __future__ import annotations

from validate import predicates as P
from validate.scenarios import CheapScenario, CreateVmScenario

# Published docs URL the no-manifest landing must point at (issue #149).
_DOCS_URL = "memblin.github.io/tkc-lvlab-py"

# --- Cheap lane: CLI contracts, no VM -------------------------------------

CHEAP_SCENARIOS: list[CheapScenario] = [
    CheapScenario(
        name="lvlab-version",
        binary="lvlab",
        args=["--version"],
        asserts=[P.ExitCode(0), P.OutputMatches(r"\d+\.\d+")],
        tags=["#138"],
    ),
    CheapScenario(
        name="lvlab-help",
        binary="lvlab",
        args=["--help"],
        asserts=[P.ExitCode(0), P.OutputContains("Usage")],
    ),
    CheapScenario(
        name="createvm-version",
        binary="createvm",
        args=["--version"],
        asserts=[P.ExitCode(0), P.OutputMatches(r"\d+\.\d+")],
    ),
    CheapScenario(
        name="deletevm-help",
        binary="deletevm",
        args=["--help"],
        asserts=[P.ExitCode(0), P.OutputContains("Usage")],
    ),
    CheapScenario(
        name="status-no-manifest-landing",
        binary="lvlab",
        args=["status"],
        cwd_kind="empty",
        asserts=[
            P.ExitCode(0),
            P.OutputContains(_DOCS_URL),
            P.OutputContains("createvm"),
        ],
        tags=["#149"],
    ),
    CheapScenario(
        name="status-bad-manifest-strict",
        binary="lvlab",
        args=["status"],
        cwd_kind="bad-manifest",
        asserts=[P.ExitCode(1)],
        tags=["#149"],
    ),
    CheapScenario(
        name="createvm-noargs-panel",
        binary="createvm",
        args=[],
        asserts=[P.ExitCode(2), P.OutputContains("Error")],
        tags=["#147"],
    ),
    CheapScenario(
        name="deletevm-noargs-panel",
        binary="deletevm",
        args=[],
        asserts=[P.ExitCode(2), P.OutputContains("Missing")],
    ),
    CheapScenario(
        name="createvm-bad-ip4-clean-error",
        binary="createvm",
        # IP-ish numeric typo stays on the static path -> clean "invalid IPv4"
        # error before any VM is provisioned (issue #105).
        args=[
            "createvm-bad-ip4-should-not-exist",
            "debian13",
            "--ip4",
            "192.168.122.999",
        ],
        asserts=[
            P.ExitCode(1),
            P.OutputContains("not a valid IPv4 address"),
            P.OutputNotContains("Traceback"),
        ],
        tags=["#105"],
    ),
]

# --- Stateful lane: real createvm guests on the NAT default network --------

CREATEVM_SCENARIOS: list[CreateVmScenario] = [
    CreateVmScenario(
        name="cvm-deb13-dhcp",
        image="debian13",
        user="debian",
        ip_mode="dhcp",
        memory_mib=1024,
        tags=["createvm", "dhcp"],
    ),
    CreateVmScenario(
        name="cvm-fedora44-dhcp",
        image="fedora44",
        user="fedora",
        ip_mode="dhcp",
        memory_mib=1536,
        tags=["createvm", "dhcp"],
    ),
    CreateVmScenario(
        name="cvm-deb13-static-ip4",
        image="debian13",
        user="debian",
        ip_mode="static",
        ip4="default,192.168.122.50",
        memory_mib=1024,
        tags=["createvm", "static", "#136"],
    ),
    CreateVmScenario(
        name="cvm-deb13-dualstack",
        image="debian13",
        user="debian",
        ip_mode="dualstack",
        ip4="default,192.168.122.51",
        ip6="default,fdfa:cade::51",
        memory_mib=1024,
        observe_v6=True,
        tags=["createvm", "dualstack", "#137", "#148"],
    ),
    CreateVmScenario(
        name="cvm-deb13-nat-flags-noop",
        image="debian13",
        user="debian",
        ip_mode="nat-flags",
        ip4="default,192.168.122.52",
        extra_args=[
            "--gateway",
            "192.168.122.1",
            "--dns",
            "192.168.122.1",
            "--search-domain",
            "lvlab.validate",
        ],
        memory_mib=1024,
        tags=["createvm", "nat-flags", "#136"],
    ),
]


def all_scenarios() -> list:
    """Return every scenario (cheap first, then stateful), in report order."""
    return [*CHEAP_SCENARIOS, *CREATEVM_SCENARIOS]
