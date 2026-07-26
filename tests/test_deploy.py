from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentArtifactTests(unittest.TestCase):
    def test_deploy_compose_uses_only_the_public_image(self):
        compose = (PROJECT_ROOT / "deploy" / "compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker.io/shaundcn/vislex:", compose)
        self.assertIn("pull_policy: always", compose)
        self.assertIn("${HOST_BIND_IP:?", compose)
        self.assertIn("${TRUSTED_HOSTS:?", compose)
        self.assertNotIn("\n    build:", compose)
        self.assertNotIn("0.0.0.0:", compose)

    def test_installer_is_idempotent_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_commands(fake_bin)

            install_dir = root / "vislex"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "HOME": str(root),
                    "VISLEX_DIR": str(install_dir),
                    "FAKE_COMPOSE": str(
                        PROJECT_ROOT / "deploy" / "compose.yaml"
                    ),
                }
            )

            first = self._run_installer(environment)
            self.assertIn("http://192.168.50.10:8080/", first.stdout)
            self.assertEqual(
                stat.S_IMODE((install_dir / ".env").stat().st_mode), 0o600
            )
            self.assertTrue((install_dir / "compose.yaml").is_file())
            for name in ("input", "output", "data"):
                self.assertTrue((install_dir / name).is_dir())

            sentinel = install_dir / "input" / "keep-me.mp4"
            sentinel.write_bytes(b"keep")
            with (install_dir / ".env").open("a", encoding="utf-8") as handle:
                handle.write("CUSTOM_VALUE=preserved\n")

            override = dict(environment)
            override["HOST_PORT"] = "9090"
            second = self._run_installer(override)
            self.assertIn("http://192.168.50.10:9090/", second.stdout)
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertIn(
                "CUSTOM_VALUE=preserved",
                (install_dir / ".env").read_text(encoding="utf-8"),
            )

    def test_installer_rejects_a_wildcard_address(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_commands(fake_bin)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "HOME": str(root),
                    "VISLEX_DIR": str(root / "vislex"),
                    "HOST_BIND_IP": "0.0.0.0",
                    "FAKE_COMPOSE": str(
                        PROJECT_ROOT / "deploy" / "compose.yaml"
                    ),
                }
            )
            result = subprocess.run(
                [str(PROJECT_ROOT / "deploy" / "install.sh")],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须是 10/8", result.stderr)

    def _run_installer(self, environment: dict[str, str]):
        result = subprocess.run(
            [str(PROJECT_ROOT / "deploy" / "install.sh")],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def _write_fake_commands(self, fake_bin: Path) -> None:
        commands = {
            "docker": """#!/bin/sh
if [ "${1:-}" = "info" ]; then
    exit 0
fi
if [ "${1:-}" = "compose" ]; then
    case " $* " in
        *" version "*)
            exit 0
            ;;
        *" config "*)
            printf 'services:\\n  vislex:\\n    image: docker.io/shaundcn/vislex:%s\\n' "${VISLEX_TAG:-latest}"
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi
exit 1
""",
            "ip": """#!/bin/sh
if [ "${1:-}" = "-4" ] && [ "${2:-}" = "route" ]; then
    printf '1.1.1.1 via 192.168.50.1 dev eth0 src 192.168.50.10 uid 1000\\n'
    exit 0
fi
if [ "${1:-}" = "-o" ] && [ "${2:-}" = "-4" ]; then
    printf '2: eth0 inet 192.168.50.10/24 brd 192.168.50.255 scope global eth0\\n'
    exit 0
fi
exit 1
""",
            "curl": """#!/bin/sh
case " $* " in
    *raw.githubusercontent.com/*)
        /bin/cat "$FAKE_COMPOSE"
        ;;
    *)
        exit 0
        ;;
esac
""",
        }
        for name, content in commands.items():
            path = fake_bin / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
