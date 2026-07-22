"""Stage-5 CI-side gate #2: render the systemd unit template and assert the
paired-topology deploy contract WITHOUT a tailnet, a host, or docker.

CI cannot reach the NSAF dev server's tailnet (ADR 0004), so it cannot run the
live smoke. What it CAN do, purely, is prove that the artifact the Operator
installs — deploy/systemd/agent-keep@.service — renders (with a per-chassis env
file, the way systemd expands `%i` and `${VAR}`) into the exact hardened,
paired, no-route-out topology Stages 3-4 froze. This test is that proof: it is
the machine-checked spec of the unit file, so a hand-edit that drops
`--cap-drop ALL`, opens a proxy host port, or gives the worker a route out
fails CI here — not silently in production.

Pure Python / pytest, no docker (default marker — runs in the `test` job).
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
UNIT_TEMPLATE = REPO_ROOT / "deploy" / "systemd" / "agent-keep@.service"

SLUG = "default-chatbot"

#: A representative per-chassis env file (deploy.sh writes exactly these NAMES;
#: the values here are stand-ins — digests/ports the render must carry through).
SAMPLE_ENV = {
    "WORKER_IMAGE_REF": "ghcr.io/seanerama/agent-keep-default-chatbot@sha256:1111",
    "PROXY_IMAGE_REF": "ghcr.io/seanerama/agent-keep-egress-proxy@sha256:2222",
    "MECHANIC_IMAGE_REF": "ghcr.io/seanerama/agent-keep-mechanic@sha256:3333",
    "WORKER_IMAGE_DIGEST": "sha256:1111",
    "MECHANIC_IMAGE_DIGEST": "sha256:3333",
    "BIND_HOST": "127.0.0.1",
    "WORKER_BIND_PORT": "8377",
    "MECHANIC_BIND_PORT": "8477",
    "SQLITE_PATH": "/tmp/agent-keep-sessions.sqlite3",
    "KEEP_EGRESS_HOST": "",
    "KEEP_EGRESS_PORT": "",
}


def _render(text: str, slug: str, env: dict[str, str]) -> str:
    """Approximate systemd rendering: expand the `%i` specifier and `${VAR}` /
    bare `$VAR` references from the env file (unset -> empty, as systemd does),
    and join `\\`-continued lines into flat command strings."""
    # systemd instance specifier (%% -> %, %i -> slug); no other %-specifiers used.
    text = text.replace("%%", "\x00").replace("%i", slug).replace("\x00", "%")
    # Join line continuations so each ExecStart*/ExecStop is one line.
    text = re.sub(r"\\\n\s*", " ", text)

    def sub_var(match: re.Match[str]) -> str:
        return env.get(match.group(1) or match.group(2) or "", "")

    text = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)", sub_var, text)
    return text


@pytest.fixture(scope="module")
def rendered() -> str:
    return _render(UNIT_TEMPLATE.read_text(encoding="utf-8"), SLUG, SAMPLE_ENV)


def _exec_lines(rendered: str, prefix: str) -> list[str]:
    return [
        ln.strip() for ln in rendered.splitlines() if ln.strip().startswith(prefix) and "=" in ln
    ]


def test_env_file_is_the_per_chassis_path(rendered: str) -> None:
    """The unit sources exactly /etc/agent-keep/<slug>.env (ADR 0004 env-file
    pattern; secret VALUES host-only, root:0600 — agent-spec rule 3)."""
    assert f"EnvironmentFile=/etc/agent-keep/{SLUG}.env" in rendered


def test_all_container_runs_carry_ops01_hardening_verbatim(rendered: str) -> None:
    """OPS-01 verbatim on EVERY `docker run` — worker (ExecStart) + proxy +
    mechanic + ingress (ExecStartPre). A dropped flag on any fails here."""
    runs = [
        ln.strip()
        for ln in rendered.splitlines()
        # the container launches (ExecStart*/docker run) — not comments,
        # not network/pull/rm lines
        if re.match(r"ExecStart(Pre)?=/usr/bin/docker run ", ln.strip())
    ]
    assert len(runs) == 4, (
        f"expected 4 `docker run` lines (worker/proxy/mechanic/ingress), got {len(runs)}"
    )
    for run in runs:
        assert "--security-opt no-new-privileges" in run
        assert "--cap-drop ALL" in run
        assert "--read-only" in run
        assert "--pids-limit" in run
        assert "--memory" in run
        assert "PYTHONDONTWRITEBYTECODE=1" in run
        assert "--tmpfs /tmp:mode=1777" in run  # load-bearing mode=1777 (conftest note)


def test_digest_pinned_image_refs_are_carried_through(rendered: str) -> None:
    """The three pinned refs the env file supplies land on their runs — the
    deploy runs immutable digests, never floating tags."""
    assert SAMPLE_ENV["WORKER_IMAGE_REF"] in rendered
    assert SAMPLE_ENV["PROXY_IMAGE_REF"] in rendered
    assert SAMPLE_ENV["MECHANIC_IMAGE_REF"] in rendered


def test_worker_has_no_route_out_except_the_proxy(rendered: str) -> None:
    """issue #11 / contract egress-observation: the worker joins the --internal
    net ONLY (no default route) and is pointed at the paired proxy via
    HTTP(S)_PROXY — belt AND suspenders."""
    execstart = " ".join(_exec_lines(rendered, "ExecStart=/usr/bin/docker run"))
    assert f"--network agent-keep-{SLUG}-net" in execstart
    # the worker is NOT on the egress-capable network
    assert f"agent-keep-{SLUG}-egress" not in execstart
    assert "HTTP_PROXY=http://egress-proxy:3128" in execstart
    assert "HTTPS_PROXY=http://egress-proxy:3128" in execstart


def test_internal_network_is_created_internal(rendered: str) -> None:
    """The worker/mechanic net is created with --internal: docker installs NO
    default route out of it (the suspenders half of the boundary)."""
    assert f"docker network create --internal agent-keep-{SLUG}-net" in rendered


def test_proxy_is_dual_homed_and_publishes_no_host_port(rendered: str) -> None:
    """The proxy sits on the internal net (alias egress-proxy) AND the egress
    net (its only route out), mounts the spec read-only (KEEP_SPEC_PATH), and
    publishes NO `-p` host port (contract egress-observation: reachable only on
    the paired private net)."""
    proxy_run = next(
        ln
        for ln in rendered.splitlines()
        if f"agent-keep-{SLUG}-proxy" in ln and "docker run" in ln
    )
    assert f"--network agent-keep-{SLUG}-net" in proxy_run
    assert "--network-alias egress-proxy" in proxy_run
    assert f"/etc/agent-keep/{SLUG}/spec.yaml:/etc/agent-keep/spec.yaml:ro" in proxy_run
    assert " -p " not in proxy_run, "the proxy must NOT publish a host port"
    # the egress leg is added right after, as a network connect
    assert f"docker network connect agent-keep-{SLUG}-egress agent-keep-{SLUG}-proxy" in rendered


def test_only_the_proxy_can_resolve_the_docker_host_gateway(rendered: str) -> None:
    """ADR 0006 (ollama provider): the egress-proxy — and ONLY the proxy — gets
    `--add-host=host.docker.internal:host-gateway`, so it can resolve the host's
    local-inference endpoint from its egress leg. The worker gets NO such route
    (it reaches a local model ONLY through the proxy — the ADR 0002 boundary
    stays intact); neither do the mechanic or the ingress forwarder."""
    add_host = "--add-host=host.docker.internal:host-gateway"
    proxy_run = next(
        ln
        for ln in rendered.splitlines()
        if f"agent-keep-{SLUG}-proxy" in ln and "docker run" in ln
    )
    assert add_host in proxy_run, "the proxy must resolve host.docker.internal via the gateway"
    # every OTHER container run must NOT carry the add-host — the worker above all
    for name in ("", "-mechanic", "-ingress"):  # "" == the worker (ExecStart)
        run = next(
            ln
            for ln in rendered.splitlines()
            if "docker run" in ln and f"--name agent-keep-{SLUG}{name} " in ln + " "
        )
        assert add_host not in run, (
            f"agent-keep-{SLUG}{name} must NOT get a route to the docker host"
        )


def test_worker_audit_is_the_single_file_bind_into_the_bundle(rendered: str) -> None:
    """Stage-4 arrangement: the worker's spec audit.path is bound to
    <slug>.audit.jsonl INSIDE the host bundle dir (persisted), and the base
    hardened set's /var/lib/agent-keep tmpfs is DROPPED for the worker (the bind
    persists it instead — drop_keep_tmpfs)."""
    execstart = " ".join(_exec_lines(rendered, "ExecStart=/usr/bin/docker run"))
    assert (
        f"/var/lib/agent-keep/{SLUG}/bundle/{SLUG}.audit.jsonl:/var/lib/agent-keep/audit.jsonl"
        in execstart
    )
    assert "--tmpfs /var/lib/agent-keep" not in execstart  # dropped for the worker


def test_mechanic_reads_the_bundle_read_only(rendered: str) -> None:
    """Stage-4 / ADR 0011: the mechanic mounts the bundle dir READ-ONLY at
    MECHANIC_WORKER_DIR, and its OWN audit is a separate volume (never the
    bundle)."""
    mech_run = next(
        ln
        for ln in rendered.splitlines()
        if f"agent-keep-{SLUG}-mechanic" in ln and "docker run" in ln
    )
    assert f"/var/lib/agent-keep/{SLUG}/bundle:/srv/worker-bundle:ro" in mech_run
    assert "MECHANIC_WORKER_DIR=/srv/worker-bundle" in mech_run
    assert f"agent-keep-{SLUG}-mechanic-audit:/var/lib/agent-keep" in mech_run


def test_ingress_forwarder_publishes_both_surfaces_with_fixed_targets(rendered: str) -> None:
    """issue #11: dev-http reaches the host ONLY through the ingress forwarder
    (worker/mechanic are --internal, un-publishable). The forwarder publishes
    both host ports, mounts the relay read-only, and relays to FIXED targets
    (worker:8000 / mechanic:8000) — no egress pivot for the worker."""
    ingress_run = next(
        ln
        for ln in rendered.splitlines()
        if f"agent-keep-{SLUG}-ingress" in ln and "docker run" in ln
    )
    assert f"-p {SAMPLE_ENV['BIND_HOST']}:{SAMPLE_ENV['WORKER_BIND_PORT']}:8000" in ingress_run
    assert f"-p {SAMPLE_ENV['BIND_HOST']}:{SAMPLE_ENV['MECHANIC_BIND_PORT']}:8001" in ingress_run
    assert "/ingress-forward.py:/ingress-forward.py:ro" in ingress_run
    # FIXED targets — the worker cannot redirect the relay elsewhere.
    assert "python /ingress-forward.py 8000:worker:8000 8001:mechanic:8000" in ingress_run
    # The forwarder joins the internal net (to reach the aliases) AFTER publishing.
    assert f"docker network connect agent-keep-{SLUG}-net agent-keep-{SLUG}-ingress" in rendered


def test_only_the_forwarder_publishes_host_ports(rendered: str) -> None:
    """The worker, mechanic and proxy publish NO host port — the forwarder is the
    single ingress seam (and the proxy is never host-reachable at all)."""
    for name in ("", "-mechanic", "-proxy"):  # "" == the worker (ExecStart)
        run = next(
            ln
            for ln in rendered.splitlines()
            if "docker run" in ln and f"--name agent-keep-{SLUG}{name} " in ln + " "
        )
        assert " -p " not in run, f"agent-keep-{SLUG}{name} must not publish a host port"


def test_anthropic_key_is_passthrough_not_a_baked_value(rendered: str) -> None:
    """The secret is passed with the `-e VAR` (no value) form: docker hands it
    in ONLY when the host env file sets it — never a literal in the unit."""
    execstart = " ".join(_exec_lines(rendered, "ExecStart=/usr/bin/docker run"))
    assert "-e ANTHROPIC_API_KEY " in execstart + " "
    assert "ANTHROPIC_API_KEY=" not in execstart  # no value ever in the unit


def test_stop_tears_down_the_whole_trio_and_both_networks(rendered: str) -> None:
    stop = " ".join(_exec_lines(rendered, "ExecStop="))
    assert f"docker stop agent-keep-{SLUG}" in stop
    assert f"docker rm -f agent-keep-{SLUG}-mechanic" in stop
    assert f"docker rm -f agent-keep-{SLUG}-proxy" in stop
    assert f"docker network rm agent-keep-{SLUG}-net" in stop
    assert f"docker network rm agent-keep-{SLUG}-egress" in stop
