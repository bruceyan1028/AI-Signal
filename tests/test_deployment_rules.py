import unittest
from pathlib import Path


class DeploymentRulesTest(unittest.TestCase):
    def test_production_public_url_is_the_company_domain(self):
        env_example = Path(".env.example").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("PUBLIC_BASE_URL=https://ai-signal.modelbest.co", env_example)
        self.assertIn("https://ai-signal.modelbest.co/", readme)

    def test_generated_site_workflows_mirror_to_codehub(self):
        for filename in ("daily-brief.yml", "weekly-report.yml"):
            workflow = Path(".github/workflows", filename).read_text(encoding="utf-8")
            self.assertIn("Mirror generated site data to CodeHub", workflow)
            self.assertIn("secrets.CODEHUB_TOKEN", workflow)
            self.assertIn("private-token", workflow)
            self.assertIn("AI-Signal.git", workflow)

    def test_weekly_cards_target_the_production_site(self):
        workflow = Path(".github/workflows/weekly-report.yml").read_text(encoding="utf-8")
        self.assertIn("PUBLIC_BASE_URL: https://ai-signal.modelbest.co", workflow)
        self.assertIn("vars.COMPANY_PLATFORM_AUTO_DEPLOY == 'true'", workflow)


if __name__ == "__main__":
    unittest.main()
