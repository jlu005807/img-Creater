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

    def test_readme_documents_optional_detection_and_port_cleanup(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("AI 生成检测（Beta）是可选功能", readme)
        self.assertIn("detection/requirements.txt", readme)
        self.assertIn("运行脚本会先释放", readme)


if __name__ == "__main__":
    main()
