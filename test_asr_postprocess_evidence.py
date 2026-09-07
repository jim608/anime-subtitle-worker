import hashlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_worker import _config
from worker import VideoWorker
from srt_utils import SrtBlock,write_srt
from transcriber import asr_diagnostics_path,read_asr_diagnostics
from transcriber import asr_transcription_hold_path,TranscriptionError
from asr_postprocess import recover_asr_postprocess
from safe_files import atomic_write_text as real_atomic_write_text


class AsrPostprocessEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name)
        self.config=_config(root,asr_diagnostics_enabled=True,filter_repeated_vocalizations=False,m2_server_canary_observer_enabled=True)
        self.source=root/'ja.srt';self.audio=root/'source.wav';self.audio.write_bytes(b'isolated-audio')
        write_srt(self.source,[SrtBlock(1,'00:00:01,000 --> 00:00:03,000',['そうです']),SrtBlock(2,'00:00:03,000 --> 00:00:04,000',['っ'])])
        self.original=self.source.read_bytes()
        self.diagnostic=asr_diagnostics_path(self.source,self.config);self.diagnostic.parent.mkdir(parents=True,exist_ok=True)
        self.diagnostic.write_text(json.dumps({'status':'accepted','srt_path':str(self.source),'audio_path':str(self.audio),'srt_sha256':hashlib.sha256(self.original).hexdigest(),'confidence_segments':[]}),encoding='utf-8')
        self.original_diagnostics=self.diagnostic.read_bytes()
        self.worker=VideoWorker(self.config,logging.getLogger('postprocess-test'))

    def assert_original_pair(self):
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(self.diagnostic.read_bytes(),self.original_diagnostics)

    def test_quality_failure_preserves_original_pair(self):
        with patch('transcriber.validate_transcription_srt_quality',side_effect=TranscriptionError('existing quality rejected')):
            with self.assertRaises(TranscriptionError):self.worker._postprocess_ja_srt(self.source)
        self.assert_original_pair()
        self.assertFalse(asr_transcription_hold_path(self.source,self.config).exists())

    def test_missing_acceptance_is_not_fabricated(self):
        self.diagnostic.unlink()
        with self.assertRaises(TranscriptionError):self.worker._postprocess_ja_srt(self.source)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertFalse(self.diagnostic.exists())

    def test_mismatched_acceptance_is_not_rebound(self):
        self.source.write_bytes(self.original+b'\n')
        with self.assertRaises(TranscriptionError):self.worker._postprocess_ja_srt(self.source)
        self.assertEqual(self.diagnostic.read_bytes(),self.original_diagnostics)

    def test_other_path_acceptance_is_not_rebound(self):
        record=json.loads(self.original_diagnostics)
        record['srt_path']=str(self.source.with_name('other.srt'))
        self.diagnostic.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaises(TranscriptionError):self.worker._postprocess_ja_srt(self.source)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_context_rebinds_only_cache_and_preserves_source_evidence(self):
        from transcriber import asr_file_fingerprint,_canonical_sha256
        record=json.loads(self.original_diagnostics)
        record['audio_fingerprint']=asr_file_fingerprint(self.audio)
        record['media_fingerprint']={'fingerprint':'immutable-source-evidence'}
        record['repair_basis']={'cache_fingerprint':'old-cache','media_fingerprint':'immutable-source-evidence'}
        self.diagnostic.write_text(json.dumps(record),encoding='utf-8')
        audio_before=self.audio.read_bytes()
        with patch('transcriber.validate_transcription_srt_quality'):
            self.worker._postprocess_ja_srt(self.source)
        updated=read_asr_diagnostics(self.source,self.config)
        self.assertEqual(updated['cache_fingerprint'],asr_file_fingerprint(self.source,full_hash=True))
        self.assertEqual(updated['repair_basis']['cache_fingerprint'],updated['cache_fingerprint']['fingerprint'])
        self.assertEqual(updated['repair_fingerprint'],_canonical_sha256(updated['repair_basis']))
        self.assertEqual(updated['media_fingerprint'],record['media_fingerprint'])
        self.assertEqual(updated['audio_fingerprint'],record['audio_fingerprint'])
        self.assertEqual(self.audio.read_bytes(),audio_before)

    def test_diagnostics_write_failure_rolls_back_pair(self):
        def write(path,*args,**kwargs):
            if Path(path)==self.diagnostic:raise OSError('injected diagnostics write failure')
            return real_atomic_write_text(path,*args,**kwargs)
        with patch('transcriber.validate_transcription_srt_quality'),patch('asr_postprocess.atomic_write_text',side_effect=write):
            with self.assertRaises(OSError):self.worker._postprocess_ja_srt(self.source)
        self.assert_original_pair()
        self.assertFalse(asr_transcription_hold_path(self.source,self.config).exists())

    def interrupt_pair_commit(self):
        def write(path,*args,**kwargs):
            if Path(path)==self.diagnostic:raise KeyboardInterrupt('simulated process loss')
            return real_atomic_write_text(path,*args,**kwargs)
        with patch('transcriber.validate_transcription_srt_quality'),patch('asr_postprocess.atomic_write_text',side_effect=write):
            with self.assertRaises(KeyboardInterrupt):self.worker._postprocess_ja_srt(self.source)

    def test_restart_restores_checkpoint_without_asr(self):
        self.interrupt_pair_commit()
        self.assertTrue(asr_transcription_hold_path(self.source,self.config).exists())
        restarted=VideoWorker(self.config,logging.getLogger('restart'))
        self.assertTrue(restarted._recover_pending_asr_output(Path('unused-video'),self.source,label='Japanese'))
        self.assert_original_pair()
        with patch('transcriber.validate_transcription_srt_quality') as validate:
            restarted._postprocess_ja_srt(self.source)
            self.assertEqual(validate.call_count,1)
        self.assertFalse(asr_transcription_hold_path(self.source,self.config).exists())

    def test_corrupt_checkpoint_keeps_hold(self):
        self.interrupt_pair_commit()
        backup=next((self.diagnostic.parent/'postprocess_checkpoints').glob('*/source.srt'))
        backup.write_bytes(b'tampered isolated checkpoint')
        with self.assertRaises(TranscriptionError):recover_asr_postprocess(self.source,self.config)
        self.assertTrue(asr_transcription_hold_path(self.source,self.config).exists())

    def test_repeat_is_idempotent_and_does_not_revalidate_or_redecode(self):
        with patch('transcriber.validate_transcription_srt_quality') as validate:
            self.worker._postprocess_ja_srt(self.source)
            current=(self.source.read_bytes(),self.diagnostic.read_bytes())
            self.worker._postprocess_ja_srt(self.source)
            self.assertEqual(validate.call_count,1)
            self.assertEqual((self.source.read_bytes(),self.diagnostic.read_bytes()),current)

    def test_fragment_merge_preserves_hash_bound_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=_config(root,asr_diagnostics_enabled=True,filter_repeated_vocalizations=False)
            source=root/'ja.srt';audio=root/'source.wav';audio.write_bytes(b'isolated-audio')
            write_srt(source,[SrtBlock(1,'00:00:01,000 --> 00:00:03,000',['そうです']),SrtBlock(2,'00:00:03,000 --> 00:00:04,000',['っ'])])
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            diagnostic=asr_diagnostics_path(source,config);diagnostic.parent.mkdir(parents=True,exist_ok=True)
            diagnostic.write_text(json.dumps({'status':'accepted','srt_path':str(source),'audio_path':str(audio),'srt_sha256':before,'confidence_segments':[]}),encoding='utf-8')
            worker=VideoWorker(config,logging.getLogger('postprocess-test'))
            with patch('transcriber.validate_transcription_srt_quality'):
                blocks=worker._postprocess_ja_srt(source)
            self.assertEqual(len(blocks),1)
            self.assertTrue(diagnostic.is_file(),'accepted ASR evidence must not disappear during cleanup')
            record=read_asr_diagnostics(source,config)
            self.assertEqual(record['srt_sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(record['postprocess']['input_srt_sha256'],before)


if __name__=='__main__':unittest.main()
