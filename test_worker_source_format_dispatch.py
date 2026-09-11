"""Real parser/OpenCC/QC/publication regression for selected Chinese SRT/ASS."""
import logging
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ass_utils import AssStyle, AssExportError, convert_srt_file_to_ass
from opencc_convert import convert_ass_to_zh_tw
from output_manifest import output_manifest_path, output_publication_marker_path, validate_output_manifest
from safe_files import sha256_file
from source_decision import SubtitleSourceDecision, CONVERT_ZH_CN
from source_integrity import SourceIntegrityError
from subtitle_extract import classify_subtitle_content_file
from subtitle_quality import analyze_subtitle_file, SubtitleQualityError
from test_worker import _config
from worker import VideoWorker


class SourceFormatDispatchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=getattr(self, 'fixture_parent', None))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / 'episode.mkv'
        self.video.write_bytes(b'fixture-source-video')
        self.source = self.root / 'episode.zh.srt'
        self.source.write_text('\n\n'.join(
            f'{i}\n00:00:{i*5:02d},000 --> 00:00:{i*5+4:02d},000\n{line}'
            for i, line in enumerate([
                '我们准备开始今天的练习。', '请认真检查这些资料。',
                '这个问题应该如何解决？', '大家已经到达图书馆了。',
                '让我们一起确认详细内容。', '我会继续完成后面的工作。',
            ], 1)) + '\n', encoding='utf-8')
        self.config = _config(self.root / 'work', source_integrity_sha256_enabled=True)
        self.config.work_path.mkdir()
        self.worker = self.new_worker()
        self.output = self.video.with_name('episode.zh-TW.ass')

    def new_worker(self):
        worker = VideoWorker.__new__(VideoWorker)
        worker.config = self.config
        worker._ass_style = AssStyle()
        worker.logger = logging.getLogger('test-source-format')
        worker._source_snapshot = None
        worker._provenance = None
        worker._set_stage = Mock()
        worker._close_stage_state = Mock()
        return worker

    def decision(self):
        stat = self.source.stat()
        return SubtitleSourceDecision(
            CONVERT_ZH_CN, self.source, 'zh-cn', 'sidecar_or_extracted', -1,
            classify_subtitle_content_file(self.source).as_dict(),
            analyze_subtitle_file(self.source, self.config, role='unknown').to_dict(),
            sha256_file(self.source), stat.st_size, stat.st_mtime_ns,
        )

    def convert(self, worker=None, decision=None):
        return (worker or self.worker)._convert_simplified_chinese_source(
            self.video, decision or self.decision())

    def signature(self, path):
        stat = path.stat()
        return sha256_file(path), stat.st_size, stat.st_mtime_ns

    def test_selected_srt_is_converted_without_ass_parser_misdispatch(self):
        before = self.signature(self.source), self.signature(self.video)
        self.convert()
        self.assertIn('Dialogue:', self.output.read_text(encoding='utf-8-sig'))
        self.assertIn('我們', self.output.read_text(encoding='utf-8-sig'))
        self.assertFalse(analyze_subtitle_file(self.output, self.config).has_failures)
        self.assertTrue(validate_output_manifest(self.video, self.config, verify_hashes=True))
        self.assertFalse(output_publication_marker_path(self.video, self.config).exists())
        self.assertEqual(before, (self.signature(self.source), self.signature(self.video)))

    def test_ass_route_preserves_styles_and_timeline(self):
        ass = self.source.with_suffix('.ass')
        convert_srt_file_to_ass(self.source, ass, self.worker._ass_style)
        self.source = ass
        before = self.signature(ass)
        self.convert()
        originals = [line.split(',', 9)[:9] for line in ass.read_text(encoding='utf-8-sig').splitlines()
                     if line.startswith('Dialogue:')]
        outputs = [line.split(',', 9)[:9] for line in self.output.read_text(encoding='utf-8-sig').splitlines()
                   if line.startswith('Dialogue:')]
        self.assertEqual(originals, outputs)
        self.assertEqual(before, self.signature(ass))

    def test_hant_srt_uses_same_format_safe_boundary(self):
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('我们', '我們'), encoding='utf-8')
        before = self.signature(self.source)
        self.worker._normalize_traditional_chinese_source(self.video, self.decision())
        self.assertEqual(before, self.signature(self.source))
        self.assertTrue(validate_output_manifest(self.video, self.config, verify_hashes=True))

    def test_malformed_srt_never_reaches_formal_destination(self):
        decision = self.decision()
        self.source.write_text('1\nnot a timestamp\n我們', encoding='utf-8')
        stat = self.source.stat()
        from dataclasses import replace
        decision = replace(decision, source_sha256=sha256_file(self.source),
                           source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
        with self.assertRaises(AssExportError):
            self.convert(decision=decision)
        self.assertFalse(self.output.exists())
        self.assertFalse(output_manifest_path(self.video, self.config).exists())

    def test_qc_rejection_does_not_publish_or_overwrite(self):
        self.config.subtitle_quality_hard_max_primary_chars = 2
        existing = b'previous invalid file retained on failed conversion'
        self.output.write_bytes(existing)
        with self.assertRaises(SubtitleQualityError):
            self.convert()
        self.assertEqual(existing, self.output.read_bytes())
        self.assertFalse(output_manifest_path(self.video, self.config).exists())

    def test_valid_existing_output_is_not_overwritten(self):
        self.convert()
        before = self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('练习', '活动'), encoding='utf-8')
        with self.assertRaisesRegex(SubtitleQualityError, 'Refusing to overwrite'):
            self.convert()
        self.assertEqual(before, (self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))))

    def test_second_worker_replays_without_new_file_or_manifest(self):
        self.convert()
        before = self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))
        with patch('worker.write_output_manifest', side_effect=AssertionError('duplicate manifest')):
            self.convert(worker=self.new_worker())
        self.assertEqual(before, (self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))))

    def test_manifest_commit_interruption_resumes_without_duplicate_publish(self):
        with patch.object(self.worker, '_commit_output_publication', side_effect=RuntimeError('interrupt')):
            with self.assertRaisesRegex(RuntimeError, 'interrupt'):
                self.convert()
        before = self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))
        self.assertTrue(output_publication_marker_path(self.video, self.config).exists())
        self.convert(worker=self.new_worker())
        self.assertFalse(output_publication_marker_path(self.video, self.config).exists())
        self.assertEqual(before, (self.signature(self.output), self.signature(output_manifest_path(self.video, self.config))))

    def test_manifest_write_interruption_reuses_verified_file(self):
        with patch('worker.write_output_manifest', side_effect=RuntimeError('interrupt')):
            with self.assertRaisesRegex(RuntimeError, 'interrupt'):
                self.convert()
        before = self.signature(self.output)
        self.convert(worker=self.new_worker())
        self.assertEqual(before, self.signature(self.output))
        self.assertTrue(validate_output_manifest(self.video, self.config, verify_hashes=True))

    def test_source_changed_after_selection_is_refused(self):
        decision = self.decision()
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('练习', '活动'), encoding='utf-8')
        with self.assertRaisesRegex(SubtitleQualityError, 'identity changed'):
            self.convert(decision=decision)
        self.assertFalse(self.output.exists())

    def test_source_changed_during_conversion_is_refused(self):
        def mutate(*args, **kwargs):
            result = convert_ass_to_zh_tw(*args, **kwargs)
            self.source.write_bytes(self.source.read_bytes() + b'\n')
            return result
        with patch('worker.convert_ass_to_zh_tw', side_effect=mutate):
            with self.assertRaises(SourceIntegrityError):
                self.convert()
        self.assertFalse(self.output.exists())

    def test_video_changed_during_conversion_is_refused(self):
        def mutate(*args, **kwargs):
            result = convert_ass_to_zh_tw(*args, **kwargs)
            self.video.write_bytes(b'changed fixture only')
            return result
        with patch('worker.convert_ass_to_zh_tw', side_effect=mutate):
            with self.assertRaises(SourceIntegrityError):
                self.convert()
        self.assertFalse(self.output.exists())

    def test_held_source_refuses_before_conversion(self):
        with patch('m2_production_recovery.require_source_not_held', side_effect=RuntimeError('held')):
            with self.assertRaisesRegex(RuntimeError, 'held'):
                self.convert()
        self.assertFalse(self.output.exists())

    def test_hold_arriving_during_conversion_refuses_publication(self):
        with patch('m2_production_recovery.require_source_not_held', side_effect=[None, RuntimeError('held')]):
            with self.assertRaisesRegex(RuntimeError, 'held'):
                self.convert()
        self.assertFalse(self.output.exists())

    def test_existing_invalid_output_has_recoverable_backup(self):
        self.output.write_bytes(b'old invalid subtitle')
        original_hash = sha256_file(self.output)
        self.convert()
        versions = list((self.config.work_path / 'ai_output_versions').glob('*/*/manifest.json'))
        self.assertEqual(len(versions), 1)
        import json
        version = json.loads(versions[0].read_text())
        self.assertEqual(version['status'], 'completed')
        self.assertEqual(version['backups'][0]['sha256'], original_hash)
        self.assertEqual(sha256_file(Path(version['backups'][0]['backup'])), original_hash)

    def test_conversion_crash_can_restart_without_partial_formal_output(self):
        with patch('worker.convert_ass_to_zh_tw', side_effect=RuntimeError('converter crash')):
            with self.assertRaisesRegex(RuntimeError, 'converter crash'):
                self.convert()
        self.assertFalse(self.output.exists())
        self.assertFalse(output_manifest_path(self.video, self.config).exists())
        self.convert(worker=self.new_worker())
        self.assertTrue(validate_output_manifest(self.video, self.config, verify_hashes=True))


if __name__ == '__main__':
    unittest.main()
