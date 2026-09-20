"""Source-language ASR must keep real acceptance across formatting and restart."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from test_worker import _config, _logger
from worker import VideoWorker, SourceSelectionReviewError
from subtitle_paths import source_transcript_paths_for_video
from srt_utils import SrtBlock, write_srt, read_srt
from safe_files import sha256_file, atomic_write_text
from transcriber import asr_diagnostics_path, asr_transcription_hold_path, read_asr_diagnostics, TranscriptionError
from output_manifest import _source_transcription_provenance_matches, SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT


class SourceAsrEvidenceContinuityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.video = self.root/'episode.mkv'
        self.video.write_bytes(b'original-media-read-only')
        self.audio = self.root/'audio.wav'
        with wave.open(str(self.audio),'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
            f.writeframes(b'\0\0'*16000*5)
        self.config = _config(self.root, m2_server_canary_observer_enabled=True,
            asr_diagnostics_enabled=True, subtitle_quality_max_primary_chars=24)
        self.paths = source_transcript_paths_for_video(self.video,self.config,'zh')
        self.worker = VideoWorker(self.config,_logger())
        self.before = {p:p.read_bytes() for p in (self.video,self.audio)}

    def accepted(self, target=None, text=None):
        target = target or self.paths.srt
        write_srt(target,[SrtBlock(1,'00:00:00,500 --> 00:00:04,500',
            [text or '這是一段需要換行處理而且必須保留原本驗證證據的完整中文字幕內容'])])
        d=asr_diagnostics_path(target,self.config);d.parent.mkdir(parents=True,exist_ok=True)
        d.write_text(json.dumps({'status':'accepted','srt_path':str(target),
            'audio_path':str(self.audio),'srt_sha256':sha256_file(target),
            'confidence_segments':[],'repair_budget_used':2,'source_evidence':'unchanged'}))
        return d

    def assert_inputs_unchanged(self):
        self.assertEqual(self.before,{p:p.read_bytes() for p in self.before})

    def test_fresh_source_process_preserves_acceptance_and_replay_skips_asr(self):
        for lang in ('zh','en'):
            with self.subTest(language=lang):
                p=source_transcript_paths_for_video(self.video,self.config,lang)
                text=('This is a complete English sentence whose line wrapping must retain its accepted source evidence.'
                    if lang=='en' else None)
                def transcribe(_video,_audio,target,_language,_config): self.accepted(target,text)
                with patch.object(self.worker,'_transcribe_source_with_fallback',side_effect=transcribe) as model:
                    self.worker._process_source_transcription(self.video,self.audio,lang)
                    record=read_asr_diagnostics(p.srt,self.config)
                    self.assertEqual(record.get('srt_sha256'),sha256_file(p.srt))
                    self.assertEqual(record.get('repair_budget_used'),2)
                    self.assertIn('postprocess',record)
                    before=(p.srt.read_bytes(),asr_diagnostics_path(p.srt,self.config).read_bytes())
                    self.worker._process_source_transcription(self.video,self.audio,lang)
                    self.assertEqual(model.call_count,1)
                    self.assertEqual(before,(p.srt.read_bytes(),asr_diagnostics_path(p.srt,self.config).read_bytes()))
                    self.assertFalse(asr_transcription_hold_path(p.srt,self.config).exists())
        self.assert_inputs_unchanged()

    def test_missing_source_evidence_reviews_before_translation_without_fabrication(self):
        d=self.accepted();d.unlink();before=self.paths.srt.read_bytes()
        with patch.object(self.worker,'_get_translator',side_effect=AssertionError('model must not run')):
            with self.assertRaisesRegex(SourceSelectionReviewError,'asr_cache_acceptance_unproven'):
                self.worker._translate_source_transcription(self.video,self.paths)
        self.assertEqual(self.paths.srt.read_bytes(),before);self.assertFalse(d.exists())
        self.assert_inputs_unchanged()

    def test_hallucination_is_stopped_before_translation(self):
        self.accepted(text='ご視聴ありがとうございました')
        with patch.object(self.worker,'_get_translator',side_effect=AssertionError('model must not run')):
            with self.assertRaisesRegex(SourceSelectionReviewError,'asr_cache_acceptance_unproven'):
                self.worker._translate_source_transcription(self.video,self.paths)
        self.assert_inputs_unchanged()

    def test_source_cache_missing_diagnostic_is_not_trusted_in_m2(self):
        d=self.accepted();d.unlink()
        self.assertFalse(self.worker._asr_cache_diagnostics_are_trusted(self.paths.srt))
        self.config.m2_server_canary_observer_enabled=False
        self.assertTrue(self.worker._asr_cache_diagnostics_are_trusted(self.paths.srt))

    def test_source_manifest_missing_diagnostic_is_not_valid_in_m2(self):
        d=self.accepted();d.unlink();st=self.paths.srt.stat()
        payload={'provenance':{'source_transcription':{'contract':SOURCE_TRANSCRIPTION_PROVENANCE_CONTRACT,
            'language':'zh','asr_used':True,'path':str(self.paths.srt),'size':st.st_size,
            'mtime_ns':st.st_mtime_ns,'sha256':sha256_file(self.paths.srt)}}}
        publication={'output_languages':['zh','zh-CN','zh-TW']}
        self.assertFalse(_source_transcription_provenance_matches(self.video,self.config,publication,payload))
        self.config.m2_server_canary_observer_enabled=False
        self.assertTrue(_source_transcription_provenance_matches(self.video,self.config,publication,payload))

    def test_diagnostic_write_failure_restores_pair_and_restart_reuses_checkpoint(self):
        d=self.accepted();original=(self.paths.srt.read_bytes(),d.read_bytes())
        def interrupt(path,*args,**kwargs):
            if Path(path)==d:raise KeyboardInterrupt('isolated process interruption')
            return atomic_write_text(path,*args,**kwargs)
        with patch('asr_postprocess.atomic_write_text',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):self.worker._normalize_source_language_srt_for_readability(self.paths.srt)
        self.assertTrue(asr_transcription_hold_path(self.paths.srt,self.config).exists())
        restarted=VideoWorker(self.config,_logger())
        self.assertTrue(restarted._recover_pending_asr_output(self.video,self.paths.srt,label='source-language:zh'))
        self.assertEqual(original,(self.paths.srt.read_bytes(),d.read_bytes()))
        with patch.object(restarted,'_transcribe_source_with_fallback',side_effect=AssertionError('must resume, not decode again')):
            restarted._process_source_transcription(self.video,self.audio,'zh')
        self.assertEqual(read_asr_diagnostics(self.paths.srt,self.config)['srt_sha256'],sha256_file(self.paths.srt))
        self.assert_inputs_unchanged()

    def test_full_quality_rejection_preserves_accepted_input_pair(self):
        d=self.accepted();original=(self.paths.srt.read_bytes(),d.read_bytes())
        with patch('transcriber.validate_transcription_srt_quality',side_effect=TranscriptionError('existing QC refuses')):
            with self.assertRaises(TranscriptionError):self.worker._normalize_source_language_srt_for_readability(self.paths.srt)
        self.assertEqual(original,(self.paths.srt.read_bytes(),d.read_bytes()))

    def test_unaccepted_hash_or_path_never_gets_normalization_acceptance(self):
        for field,value in (('status','rejected'),('srt_sha256','0'*64),('srt_path','/wrong/source.srt')):
            with self.subTest(field=field):
                d=self.accepted();record=json.loads(d.read_text());record[field]=value
                d.write_text(json.dumps(record));before=(self.paths.srt.read_bytes(),d.read_bytes())
                with self.assertRaises(TranscriptionError):self.worker._normalize_source_language_srt_for_readability(self.paths.srt)
                self.assertEqual(before,(self.paths.srt.read_bytes(),d.read_bytes()))

    def test_legacy_non_m2_without_diagnostic_remains_compatible(self):
        d=self.accepted();d.unlink();self.config.m2_server_canary_observer_enabled=False
        self.assertTrue(self.worker._normalize_source_language_srt_for_readability(self.paths.srt))
        self.assertFalse(d.exists());self.assertGreater(len(read_srt(self.paths.srt)[0].text),1)


if __name__=='__main__':unittest.main()
