import importlib.util
import tempfile
import unittest
from pathlib import Path


RUN_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run.py"
SPEC = importlib.util.spec_from_file_location("scrape_cascade_run", RUN_PATH)
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


class ReadDomainsTests(unittest.TestCase):
    def write_temp(self, text):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_detects_domain_column_not_first(self):
        path = self.write_temp(
            "company_id,name,domain,website\n"
            "1,Acme,https://www.acme.com,https://www.acme.com\n"
            "2,Other,other.example,\n"
        )
        self.assertEqual(run.read_domains(path), ["www.acme.com", "other.example"])

    def test_domain_column_override_by_name(self):
        path = self.write_temp(
            "company_id,name,homepage\n"
            "1,Acme,https://acme.com/about\n"
        )
        self.assertEqual(run.read_domains(path, domain_column="homepage"), ["acme.com"])

    def test_domain_column_override_by_index(self):
        path = self.write_temp(
            "1,https://acme.com\n"
            "2,https://other.example\n"
        )
        self.assertEqual(run.read_domains(path, domain_column="1"), ["acme.com", "other.example"])


if __name__ == "__main__":
    unittest.main()
