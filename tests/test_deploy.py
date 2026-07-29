from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoint import (
    DEFAULT_PGID,
    DEFAULT_PUID,
    EntrypointError,
    configured_identity,
    prepare_mounts,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentArtifactTests(unittest.TestCase):
    def test_deploy_compose_is_the_minimal_nas_variant(self):
        compose = (PROJECT_ROOT / "deploy" / "compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker.io/shaundcn/vislex:", compose)
        self.assertIn(
            '${HOST_BIND_IP:-127.0.0.1}:${HOST_PORT:-8080}:8000',
            compose,
        )
        self.assertIn(
            'TRUSTED_HOSTS: "${TRUSTED_HOSTS:-127.0.0.1,localhost}"',
            compose,
        )
        self.assertIn('PUID: "${PUID:-1000}"', compose)
        self.assertIn('PGID: "${PGID:-1000}"', compose)
        self.assertIn("${VISLEX_INPUT_DIR:-./input}", compose)
        self.assertIn("${VISLEX_OUTPUT_DIR:-./output}", compose)
        self.assertIn("${VISLEX_DATA_DIR:-./data}", compose)
        self.assertIn("init: true", compose)
        self.assertNotIn("\n    build:", compose)
        for removed_option in (
            "pull_policy:",
            "restart:",
            "\n    user:",
            "read_only:",
            "security_opt:",
            "cap_drop:",
            "tmpfs:",
            "healthcheck:",
        ):
            self.assertNotIn(removed_option, compose)

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
                    "VISLEX_INPUT_DIR": str(root / "media input"),
                    "VISLEX_OUTPUT_DIR": str(root / "knowledge output"),
                    "VISLEX_DATA_DIR": str(root / "application data"),
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
            environment_text = (install_dir / ".env").read_text(
                encoding="utf-8"
            )
            self.assertIn("PUID=", environment_text)
            self.assertIn("PGID=", environment_text)
            for name, variable in (
                ("media input", "VISLEX_INPUT_DIR"),
                ("knowledge output", "VISLEX_OUTPUT_DIR"),
                ("application data", "VISLEX_DATA_DIR"),
            ):
                expected = root / name
                self.assertTrue(expected.is_dir())
                self.assertIn(f"{variable}={expected}\n", environment_text)

            sentinel = root / "media input" / "keep-me.mp4"
            sentinel.write_bytes(b"keep")
            with (install_dir / ".env").open("a", encoding="utf-8") as handle:
                handle.write("CUSTOM_VALUE=preserved\n")

            override = dict(environment)
            override["HOST_PORT"] = "9090"
            for name in (
                "VISLEX_INPUT_DIR",
                "VISLEX_OUTPUT_DIR",
                "VISLEX_DATA_DIR",
            ):
                override.pop(name)
            second = self._run_installer(override)
            self.assertIn("http://192.168.50.10:9090/", second.stdout)
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertIn(
                "CUSTOM_VALUE=preserved",
                (install_dir / ".env").read_text(encoding="utf-8"),
            )

    def test_ci_cleans_identity_fixture_with_a_root_container(self):
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "docker-publish.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn('rm -rf "$identity_dir"', workflow)
        self.assertIn(':/cleanup"', workflow)
        self.assertIn("--entrypoint python", workflow)
        self.assertIn("--user 0:0", workflow)

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

    def test_installer_warns_about_root_container_identity(self):
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
                    "PUID": "0",
                    "PGID": "0",
                    "FAKE_COMPOSE": str(
                        PROJECT_ROOT / "deploy" / "compose.yaml"
                    ),
                }
            )
            result = self._run_installer(environment)
            self.assertIn("持续以 root 身份运行", result.stderr)

    def test_entrypoint_identity_defaults_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                configured_identity(),
                (DEFAULT_PUID, DEFAULT_PGID),
            )
        with patch.dict(
            os.environ,
            {"PUID": "1000", "PGID": "100"},
            clear=True,
        ):
            self.assertEqual(configured_identity(), (1000, 100))

    def test_entrypoint_rejects_invalid_identity(self):
        for name, value in (
            ("PUID", "-1"),
            ("PUID", "root"),
            ("PGID", "2147483648"),
        ):
            environment = {"PUID": "1000", "PGID": "1000", name: value}
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(EntrypointError):
                        configured_identity()

    def test_entrypoint_repairs_api_key_permissions_without_recursing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            output_dir = root / "output"
            data_dir = root / "data"
            for directory in (input_dir, output_dir, data_dir):
                directory.mkdir()
            api_key = data_dir / "ark_api_key"
            unrelated = data_dir / "user-file"
            api_key.write_text("secret\n", encoding="utf-8")
            unrelated.write_text("keep\n", encoding="utf-8")
            api_key.chmod(0o644)
            unrelated.chmod(0o644)

            with patch(
                "app.entrypoint.MOUNT_DIRECTORIES",
                (input_dir, output_dir, data_dir),
            ), patch("app.entrypoint.DATA_DIRECTORY", data_dir):
                prepare_mounts(os.geteuid(), os.getegid())

            self.assertEqual(stat.S_IMODE(api_key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o644)

    def test_dockerfile_uses_privilege_dropping_entrypoint(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            'ENTRYPOINT ["python", "-m", "app.entrypoint"]',
            dockerfile,
        )
        self.assertNotIn("\nUSER 10001:10001", dockerfile)

    def test_feature_release_version_is_1_1_0(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("ARG VISLEX_VERSION=1.1.0", dockerfile)
        self.assertIn("当前源码版本：`1.1.0`", readme)
        self.assertIn("VISLEX_TAG=1.0.1", readme)

    def test_source_compose_keeps_only_required_startup_capabilities(self):
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\n    user:", compose)
        self.assertIn("cap_drop:\n      - ALL", compose)
        for capability in (
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "KILL",
            "SETGID",
            "SETUID",
        ):
            self.assertIn(f"      - {capability}\n", compose)

    def test_legacy_identity_variable_names_are_absent(self):
        for relative_path in (
            ".env.example",
            "README.md",
            "docker-compose.yml",
            "deploy/compose.yaml",
            "deploy/install.sh",
        ):
            content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertNotIn("APP" + "_UID", content)
                self.assertNotIn("APP" + "_GID", content)

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
