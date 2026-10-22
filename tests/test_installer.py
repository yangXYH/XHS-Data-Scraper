"""Offline installer tests use synthetic ZIP files, never download or execute binaries."""
import hashlib
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('install_engine', Path(__file__).resolve().parents[1] / 'scripts/install_engine.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='xhs-installer-test-')
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'fixture.zip'
        self.destination = self.root / 'app'
        self.destination.mkdir()
        self.original_hash = installer.SHA256

    def tearDown(self):
        installer.SHA256 = self.original_hash
        self.temporary.cleanup()

    def fixture(self, unsafe=False):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('release/main.exe', b'fixture-do-not-execute')
            archive.writestr('release/_internal/runtime.txt', 'fixture')
            archive.writestr('release/README.txt', 'must not overwrite project docs')
            if unsafe:
                archive.writestr('release/../escape.txt', 'unsafe')
        installer.SHA256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_extract_required_runtime_and_refuse_overwrite(self):
        self.fixture()
        installer.install(self.destination, self.archive)
        self.assertEqual((self.destination / 'main.exe').read_bytes(), b'fixture-do-not-execute')
        self.assertTrue((self.destination / '_internal/runtime.txt').is_file())
        self.assertFalse((self.destination / 'README.txt').exists())
        with self.assertRaises(ValueError):
            installer.install(self.destination, self.archive)

    def test_hash_mismatch_leaves_destination_clean(self):
        self.fixture()
        installer.SHA256 = '0' * 64
        with self.assertRaises(ValueError):
            installer.install(self.destination, self.archive)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_zip_traversal_rejected_without_installation(self):
        self.fixture(unsafe=True)
        with self.assertRaises(ValueError):
            installer.install(self.destination, self.archive)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse((self.root / 'escape.txt').exists())


if __name__ == '__main__':
    unittest.main()
