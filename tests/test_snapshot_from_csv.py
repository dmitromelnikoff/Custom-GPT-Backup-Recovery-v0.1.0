import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from snapshot_from_csv import convert

class ConvertTest(unittest.TestCase):
    def test_conversion_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);csvp=base/'inventory.csv';dst=base/'backup'/'gpt_list_snapshot.json'
            with csvp.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['gpt_id','gpt_name','file_name','file_id','expected_bytes','ordinal'],delimiter=';')
                w.writeheader();w.writerow(dict(gpt_id='g-DEMO',gpt_name='Demo',file_name='hi.txt',file_id='file-DEMO',expected_bytes='3',ordinal='1'))
            self.assertEqual(convert(csvp,dst),(1,1))
            j=json.loads(dst.read_text('utf-8'))
            self.assertEqual(j['gpts'][0]['resource']['files'][0]['name'],'hi.txt')
            with self.assertRaises(FileExistsError):convert(csvp,dst)

    def test_reject_missing_ordinals(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);csvp=base/'inventory.csv'
            csvp.write_text('gpt_id;gpt_name;file_name;file_id;expected_bytes;ordinal\ng-DEMO;Demo;a;file-A;3;2\n',encoding='utf-8')
            with self.assertRaises(ValueError):convert(csvp,base/'snap.json')

if __name__=='__main__':unittest.main()
