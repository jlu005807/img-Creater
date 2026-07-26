from pathlib import Path
from unittest import TestCase, main


ROOT = Path(__file__).resolve().parents[1]


class InstallationScriptSourceTests(TestCase):
    def test_install_scripts_enforce_minimum_python_and_node_versions(self):
        ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
        sh = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn("Assert-VersionAtLeast $pyVersionNumber 3 10 'Python'", ps1)
        self.assertIn("Assert-VersionAtLeast $nodeVersionNumber 18 0 'Node.js'", ps1)
        self.assertIn('assert_version_at_least "$py_version" 3 10 "Python"', sh)
        self.assertIn('assert_version_at_least "$node_version" 18 0 "Node.js"', sh)

    def test_install_ps1_pip_self_upgrade_cannot_abort_install(self):
        # On Windows PowerShell 5.1 native stderr + 2>$null under EAP=Stop can
        # raise NativeCommandError; the decorative pip upgrade must be guarded.
        ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("try { & $venvPython -m pip install --upgrade pip", ps1)

    def test_run_sh_health_probe_does_not_require_curl(self):
        sh = (ROOT / "run.sh").read_text(encoding="utf-8")

        self.assertIn("fetch_url", sh)
        self.assertIn("command -v curl", sh)
        self.assertIn("urllib.request", sh)

    def test_requirements_pin_upper_version_bounds(self):
        for req in ("backend/requirements.txt", "detection/requirements.txt"):
            for line in (ROOT / req).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self.assertIn(",<", line, f"{req}: '{line}' must carry an upper bound")

    def test_readme_documents_optional_detection_and_port_cleanup(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("AI 生成检测（Beta）是可选功能", readme)
        self.assertIn("detection/requirements.txt", readme)
        self.assertIn("运行脚本会先释放", readme)

    def test_readme_documents_bounded_async_polling(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("持续轮询直到完成、失败或手动停止", readme)
        self.assertIn("async_max_wait", readme)


if __name__ == "__main__":
    main()
