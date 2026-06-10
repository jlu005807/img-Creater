from pathlib import Path
from unittest import TestCase, main


PLAYGROUND = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "Playground" / "index.vue"


class PlaygroundErrorSourceTests(TestCase):
    def test_attempt_error_text_surfaces_gateway_diagnostics(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("details.http_status || details.status_code", source)
        self.assertIn("details.html_title", source)
        self.assertIn("details.cf_ray", source)
        self.assertIn("details.gateway_hint", source)
        self.assertIn("details.cloudflare", source)
        self.assertIn("details.gateway_timeout", source)
        self.assertIn("网关超时", source)
        self.assertIn("!details.is_html_response", source)


if __name__ == "__main__":
    main()
