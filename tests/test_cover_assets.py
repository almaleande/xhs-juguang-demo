"""封面完整性与缓存版本回归；只使用标准库，不修改数据。"""
import hashlib
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
COVER_REF = re.compile(r'assets/covers/cover_\d{4}\.jpg(?:\?v=[a-f0-9]+)?')


def jpeg_size(payload):
    if not payload.startswith(b'\xff\xd8'):
        raise ValueError('not JPEG')
    offset = 2
    while offset < len(payload):
        if payload[offset] != 0xFF:
            raise ValueError('invalid marker')
        marker = payload[offset + 1]
        length = int.from_bytes(payload[offset + 2:offset + 4], 'big')
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(payload[offset + 5:offset + 7], 'big')
            width = int.from_bytes(payload[offset + 7:offset + 9], 'big')
            return width, height
        if marker == 0xDA or length < 2:
            break
        offset += length + 2
    raise ValueError('missing image dimensions')


class CoverAssetsTest(unittest.TestCase):
    def test_titles_match_cover_and_remain_consistent_across_ranges(self):
        notes = json.loads((ROOT / 'data/note_titles.json').read_text())['notes']
        self.assertEqual(len(notes), 547)
        by_cover = {}
        for row in notes.values():
            title = row['title']
            self.assertTrue(title.strip())
            self.assertNotIn('演示作品', title)
            self.assertNotIn('投放工作台｜', title)
            self.assertEqual(by_cover.setdefault(row['image'], title), title)
        self.assertIn('谁能拒绝如此有故事感的胶片婚纱照📷', by_cover.values())

        def check(value):
            if isinstance(value, dict):
                if 'note_title' in value:
                    self.assertEqual(value['note_title'], notes[value['note_id']]['title'])
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        for folder in ('快照', 'data/range_creative'):
            for path in (ROOT / folder).glob('*.json'):
                check(json.loads(path.read_text()))

    def test_all_covers_are_high_resolution_unique_jpegs(self):
        covers = sorted((ROOT / 'assets/covers').glob('cover_*.jpg'))
        self.assertEqual(len(covers), 190)
        digests = set()
        for p in covers:
            with self.subTest(cover=p.name):
                data = p.read_bytes()
                self.assertGreaterEqual(min(jpeg_size(data)), 1080)
                self.assertTrue(data.endswith(b'\xff\xd9'))
                self.assertNotIn(b'Exif\x00\x00', data)
                self.assertNotIn(b'http://ns.adobe.com/xap/', data)
                digests.add(hashlib.sha256(data).digest())
        self.assertEqual(len(digests), 190)

    def test_all_data_references_resolve_with_current_cache_hash(self):
        referenced = set()
        for folder in ('data', '快照'):
            for p in (ROOT / folder).rglob('*.json'):
                text = p.read_text(encoding='utf-8')
                json.loads(text)
                for ref in COVER_REF.findall(text):
                    with self.subTest(data=p.name, ref=ref):
                        path, _, version = ref.partition('?v=')
                        target = ROOT / path
                        self.assertTrue(target.is_file())
                        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
                        self.assertEqual(version, digest)
                        referenced.add(path)
        expected = {p.relative_to(ROOT).as_posix() for p in (ROOT / 'assets/covers').glob('cover_*.jpg')}
        # Some pre-existing cover slots are currently unused; do not alter
        # business records just to force every stored image into a table.
        self.assertTrue(referenced)
        self.assertTrue(referenced <= expected)


if __name__ == '__main__':
    unittest.main()
