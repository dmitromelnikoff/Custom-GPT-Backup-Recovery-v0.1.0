"""Offline regression tests. All GPT names, IDs and data are synthetic."""
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import archive_cli


def fixture(backup: Path, rows):
    backup.mkdir(parents=True, exist_ok=True)
    data = {'gpts': []}
    for i, (name, files) in enumerate(rows, start=1):
        data['gpts'].append({'resource': {
            'gizmo': {'id': f'g-EXAMPLE{i:03}', 'display': {'name': name}},
            'files': [{'file_id': f'file-EXAMPLE{i:03}-{j:02}', 'name': fname, 'size': size,
                       'type': 'text/plain', 'created_at': '2026-01-01T00:00:00Z',
                       'last_modified': '2026-01-01T00:00:00Z'}
                      for j, (fname, size) in enumerate(files, start=1)]
        }})
    p = backup / 'gpt_list_snapshot.json'
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return p


class ArchiveCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.backup = self.base / 'Custom_GPT_backup'
        self.report = self.base / 'reports'
        self.source = self.base / 'vault'
        self.source.mkdir()

    def test_audit_distinguishes_receipt_hash_from_size_only(self):
        snap = fixture(self.backup, [('Demo A', [('a.md', 5), ('b.md', 4), ('c.md', 9)])])
        _, targets = archive_cli.read_targets(snap, self.backup)
        a, b, _ = targets
        a['dest'].parent.mkdir(parents=True, exist_ok=True)
        a['dest'].write_bytes(b'hello')
        b['dest'].write_bytes(b'data')
        digest = hashlib.sha256(b'hello').hexdigest()
        with (self.backup / 'direct_gpt_ui_knowledge_inventory.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, delimiter=';', fieldnames=['status', 'gpt_id', 'file_id', 'file_name', 'note'])
            w.writeheader()
            w.writerow({'status': 'SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH', 'gpt_id': a['gpt_id'],
                        'file_id': a['file_id'], 'file_name': a['name'], 'note': 'SHA-256 ' + digest})
        archive_cli.audit(snap, self.backup, self.report)
        summary = json.loads((self.report / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['knowledge_total'], 3)
        self.assertEqual(summary['direct_receipt_hash_verified'], 1)
        self.assertEqual(summary['present_unverified_or_archive_candidate'], 1)
        self.assertEqual(summary['missing'], 1)
        # Tampering of matching size MUST remove direct verification.
        a['dest'].write_bytes(b'HELLO')
        archive_cli.audit(snap, self.backup, self.report)
        summary = json.loads((self.report / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['direct_receipt_hash_verified'], 0)
        self.assertEqual(summary['present_unverified_or_archive_candidate'], 2)

    def test_import_folder_read_only_and_no_overwrite(self):
        snap = fixture(self.backup, [('Demo A', [('document.md', 5)])])
        src = self.source / 'document.md'
        src.write_bytes(b'hello')
        before = hashlib.sha256(src.read_bytes()).hexdigest()
        archive_cli.import_folder(snap, self.backup, self.source, self.report, False, False)
        _, rows = archive_cli.read_targets(snap, self.backup)
        dest = rows[0]['dest']
        self.assertFalse(dest.exists())
        archive_cli.import_folder(snap, self.backup, self.source, self.report, True, False)
        self.assertEqual(dest.read_bytes(), b'hello')
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)
        self.assertTrue((self.backup / 'manual_candidate_import_receipts.jsonl').exists())
        archive_cli.import_folder(snap, self.backup, self.source, self.report, True, False)
        self.assertEqual(dest.read_bytes(), b'hello')

    def test_conflicting_hashes_do_not_autoselect(self):
        snap = fixture(self.backup, [('Demo A', [('a.md', 5)])])
        (self.source / 'a.md').write_bytes(b'hello')
        sub = self.source / 'other'; sub.mkdir()
        (sub / 'a.md').write_bytes(b'HELLO')
        archive_cli.import_folder(snap, self.backup, self.source, self.report, True, False)
        _, rows = archive_cli.read_targets(snap, self.backup)
        self.assertFalse(rows[0]['dest'].exists())
        summ = json.loads((self.report / 'manual_import_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summ['status_counts']['CONFLICTING_SOURCE_HASHES'], 1)

    def test_shared_targets_need_opt_in(self):
        snap = fixture(self.backup, [('Demo A', [('a.md', 5)]), ('Demo B', [('a.md', 5)])])
        (self.source / 'a.md').write_bytes(b'hello')
        archive_cli.import_folder(snap, self.backup, self.source, self.report, True, False)
        _, targets = archive_cli.read_targets(snap, self.backup)
        self.assertTrue(all(not t['dest'].exists() for t in targets))
        archive_cli.import_folder(snap, self.backup, self.source, self.report, True, True)
        self.assertTrue(all(t['dest'].read_bytes() == b'hello' for t in targets))

    def test_refuse_to_write_inside_source(self):
        # Even if user points to the entire backup as source, the destination
        # must not be written underneath the scanned source tree.
        snap = fixture(self.backup, [('Demo A', [('a.md', 5)])])
        (self.backup / 'a.md').write_bytes(b'hello')
        archive_cli.import_folder(snap, self.backup, self.backup, self.report, True, False)
        _, targets = archive_cli.read_targets(snap, self.backup)
        self.assertFalse(targets[0]['dest'].exists())
        summ = json.loads((self.report / 'manual_import_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summ['status_counts']['DESTINATION_INSIDE_SOURCE_REFUSED'], 1)


if __name__ == '__main__':
    unittest.main()
