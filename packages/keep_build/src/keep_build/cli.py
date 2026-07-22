"""keep-build CLI — `keep-build validate <spec.yaml>` and `keep-build build <spec.yaml>`.

The Foundry's `foundry validate`/`foundry build` surface, carried only as far
as Agent Keep needs: bake ONE spec into one image. No interview, no fleet, no
templates.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from agent_runtime.wiring import ComponentNotImplementedError, EgressCrossValidationError
from keep_build.composer import emit_build_context, image_tag
from keep_build.egress_proxy import PROXY_IMAGE, emit_proxy_build_context
from keep_spec import AgentSpec, load_spec


def _load(spec_path: str) -> AgentSpec:
    try:
        return load_spec(spec_path)
    except FileNotFoundError:
        print(f"error: spec file not found: {spec_path}", file=sys.stderr)
        raise SystemExit(2) from None
    except ValidationError as exc:
        print(f"error: spec failed keep/v1 validation:\n{exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_validate(args: argparse.Namespace) -> int:
    spec = _load(args.spec)
    print(
        f"valid keep/v1 AgentSpec: {spec.metadata.slug} (specVersion {spec.metadata.specVersion})"
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    spec = _load(args.spec)
    tag = args.tag or image_tag(spec)

    def _build_from(context_dir: Path) -> int:
        try:
            emit_build_context(spec, Path(args.spec), context_dir)
        except (ComponentNotImplementedError, EgressCrossValidationError) as exc:
            # The same loud gate the runner applies at boot: unbuildable
            # selections and egress cross-validation failures never compose.
            print(f"error: {exc}", file=sys.stderr)
            return 3
        print(f"build context: {context_dir}")
        if args.context_only:
            return 0
        result = subprocess.run(["docker", "build", "-t", tag, str(context_dir)])
        if result.returncode != 0:
            print("error: docker build failed", file=sys.stderr)
            return result.returncode
        print(f"built {tag}")
        return 0

    if args.context_dir:
        return _build_from(Path(args.context_dir))
    with tempfile.TemporaryDirectory(prefix="keep-build-") as tmp:
        return _build_from(Path(tmp))


def cmd_build_proxy(args: argparse.Namespace) -> int:
    """Bake the spec-INDEPENDENT egress-proxy image (keep_build.egress_proxy):
    one generic image, allowlist mounted at run time from the same spec.yaml
    the agent was baked from."""
    tag = args.tag or PROXY_IMAGE

    def _build_from(context_dir: Path) -> int:
        emit_proxy_build_context(context_dir)
        print(f"build context: {context_dir}")
        if args.context_only:
            return 0
        result = subprocess.run(["docker", "build", "-t", tag, str(context_dir)])
        if result.returncode != 0:
            print("error: docker build failed", file=sys.stderr)
            return result.returncode
        print(f"built {tag}")
        return 0

    if args.context_dir:
        return _build_from(Path(args.context_dir))
    with tempfile.TemporaryDirectory(prefix="keep-build-proxy-") as tmp:
        return _build_from(Path(tmp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="keep-build", description="Agent Keep composer/builder — bake ONE spec into an image"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="strictly validate a keep/v1 spec")
    p_validate.add_argument("spec", help="path to the agent spec YAML")
    p_validate.set_defaults(func=cmd_validate)

    p_build = sub.add_parser(
        "build", help="validate, compose selected components, and docker-build the image"
    )
    p_build.add_argument("spec", help="path to the agent spec YAML")
    p_build.add_argument("--tag", help="override the image tag", default=None)
    p_build.add_argument(
        "--context-dir",
        help="write the build context here (kept) instead of a temp dir",
        default=None,
    )
    p_build.add_argument(
        "--context-only",
        action="store_true",
        help="emit the build context but skip `docker build`",
    )
    p_build.set_defaults(func=cmd_build)

    p_proxy = sub.add_parser(
        "build-proxy",
        help="docker-build the spec-independent egress observation proxy image",
    )
    p_proxy.add_argument("--tag", help="override the image tag", default=None)
    p_proxy.add_argument(
        "--context-dir",
        help="write the build context here (kept) instead of a temp dir",
        default=None,
    )
    p_proxy.add_argument(
        "--context-only",
        action="store_true",
        help="emit the build context but skip `docker build`",
    )
    p_proxy.set_defaults(func=cmd_build_proxy)

    args = parser.parse_args(argv)
    rc: int = args.func(args)
    return rc


def entrypoint() -> None:
    raise SystemExit(main())
