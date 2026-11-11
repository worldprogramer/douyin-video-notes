"""Reader-first publishing and provenance regression checks without media services."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import douyin_notes as notes


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / 'job'
        self.analysis = self.job / 'analysis'
        self.analysis.mkdir(parents=True)
        self.source = self.job / 'source.mp4'
        self.source.write_bytes(b'local synthetic fixture; no user media')
        sha = notes.digest(self.source)
        notes.dump(self.analysis / 'source.json', {
            'path': str(self.source), 'sha256': sha, 'title': '测试视频',
            'source_url': 'https://www.douyin.com/video/1234567890123456789',
            'video': {'duration': 10}})
        notes.dump(self.analysis / 'transcript.json', {'source_sha256': sha, 'segments': []})
        (self.analysis / 'transcript.md').write_text('# 完整转录\n', encoding='utf-8')
        (self.analysis / 'transcript.srt').write_text('', encoding='utf-8')
        (self.analysis / 'frames').mkdir()
        (self.analysis / 'frames/one.png').write_bytes(b'registered-image-fixture')
        notes.dump(self.analysis / 'frames.json', {
            'source_sha256': sha, 'frames': [{'image': 'frames/one.png', 'time': 3}]})
        self.image = {'file': 'frames/one.png', 'caption': '关键设置', 'reason': '画面补充关键参数。'}
        self.plan = {
            'title': '详细资料', 'summary': '完整的讲解和依据。',
            'review': {'full_transcript_read': True, 'all_contact_sheets_viewed': True},
            'sections': [{'heading': '操作证据', 'start': 0, 'end': 10,
                          'body': '逐项解释设置。', 'images': [self.image]}],
            'uncertainties': ['具体版本未确认。'],
            'reader': {'title': '先定内容，再细化', 'summary': '先确认内容，再提高画质。',
                       'sections': [{'heading': '怎么用', 'body': '先生成并保存，再调整画质。'}]}}
        self.plan_path = self.job / 'plan.json'
        self.output = self.root / 'output'

    def publish(self):
        notes.dump(self.plan_path, self.plan)
        args = argparse.Namespace(job=str(self.job), plan=str(self.plan_path),
                                  output=str(self.output), overwrite=True)
        with contextlib.redirect_stdout(io.StringIO()):
            notes.publish(args)

    def test_reader_and_details_are_distinct(self):
        self.publish()
        reader = (self.output / '笔记.md').read_text(encoding='utf-8')
        details = (self.output / '详细资料.md').read_text(encoding='utf-8')
        self.assertIn('先生成并保存', reader)
        self.assertNotIn('逐项解释设置', reader)
        self.assertNotIn('00:00:', reader)
        self.assertNotIn('具体版本未确认', reader)
        self.assertIn('逐项解释设置', details)
        self.assertIn('00:00:03.000', details)
        self.assertIn('具体版本未确认', details)
        self.assertIn('(详细资料.md)', reader)
        self.assertTrue((self.output / 'assets/画面-01.png').is_file())

    def test_same_image_is_shared_between_documents(self):
        self.plan['reader']['sections'][0]['images'] = [self.image]
        self.publish()
        self.assertEqual(len(list((self.output / 'assets').iterdir())), 1)
        for name in ('笔记.md', '详细资料.md'):
            self.assertIn('assets/画面-01.png', (self.output / name).read_text(encoding='utf-8'))

    def test_missing_reader_is_rejected(self):
        del self.plan['reader']
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse((self.output / '笔记.md').exists())

    def test_wrong_source_images_are_rejected(self):
        data = notes.read(self.analysis / 'frames.json')
        data['source_sha256'] = 'different-source'
        notes.dump(self.analysis / 'frames.json', data)
        with self.assertRaises(ValueError):
            self.publish()

    def test_detailed_timeline_still_validated(self):
        self.plan['sections'][0]['end'] = 99
        with self.assertRaises(ValueError):
            self.publish()

    def test_reader_cannot_embed_unregistered_image(self):
        self.plan['reader']['sections'][0]['images'] = [dict(self.image, file='../source.mp4')]
        with self.assertRaises(ValueError):
            self.publish()


if __name__ == '__main__':
    unittest.main()
