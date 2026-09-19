"""Hard-QC source rejection must reach existing safe alternative routes."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from source_analyzer import (analyze_sources, ASR_JA_AUDIO, CONVERT_ZH_CN,
    TRANSLATE_JA_SUBTITLE, USE_EXISTING_ZH_TW)
from source_inventory import build_source_input_identity, inventory_sources
from test_m2_review_source_regressions import srt_text, JA, CN, TW


class SourceHardQcFallbackTest(unittest.TestCase):
    def test_temporary_qc_read_error_propagates_to_retry_not_review(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('subtitle_quality.analyze_subtitle_file',side_effect=OSError(11,'temporary resource unavailable')):
                with self.assertRaises(OSError):
                    self.inventory(Path(temp),[('zh-CN',CN,False)])

    def test_invalid_hard_qc_evidence_is_not_silently_accepted(self):
        from source_analyzer import SubtitleCandidateInput
        for value in (True,'PASS',[None],['']):
            with self.subTest(value=value),self.assertRaises(ValueError):
                SubtitleCandidateInput(track_index=1,codec='srt',hard_qc_failures=value)

    def inventory(self, root, specifications, *, audio_language='jpn', audio=True):
        video=root/'episode.mkv';video.write_bytes(b'immutable-source')
        config=SimpleNamespace(work_path=root/'work',subtitle_quality_check_enabled=True)
        paths=[]
        for label,text,bad in specifications:
            p=root/f'episode.{label}.srt';raw=srt_text(text)
            if bad == 'parse':
                raw += '\n\n327\n00:00:00,000 --> 00:00:05,000\n'
            elif bad:raw=raw.replace('00:00:01,900','00:00:04,000',1)
            p.write_text(raw,encoding='utf-8');paths.append(p)
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (video,*paths)}
        job=build_source_input_identity(video,'test-job',config=config).media_job_identity
        streams=[{'index':1,'codec_type':'audio','codec_name':'aac','duration':'48',
            'channels':2,'sample_rate':'48000','tags':{'language':audio_language},'disposition':{'default':1}}] if audio else []
        with patch('source_inventory._probe_media',return_value={'format':{'duration':'48'},'streams':streams}):
            inventory=inventory_sources(video,job,config=config,sidecar_paths=paths)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})
        return inventory,config

    def test_invalid_source_uses_trusted_japanese_audio_and_persists_qc_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            inventory,_=self.inventory(Path(temp),[('zh-CN',CN,True)])
            decision=analyze_sources(**inventory.analyzer_arguments())
            self.assertEqual(ASR_JA_AUDIO,decision.strategy)
            source=next(c for c in decision.candidates if c.kind=='subtitle')
            self.assertFalse(source.eligible)
            self.assertIn('source_hard_qc_failed',source.rejection_reasons)
            self.assertIn('timing_overlap',source.evidence['hard_qc']['failure_codes'])

    def test_valid_alternatives_keep_existing_priority(self):
        cases=[([('zh-TW',TW,True),('zh-CN',CN,False)],CONVERT_ZH_CN),
            ([('zh-CN',CN,True),('ja',JA,False)],TRANSLATE_JA_SUBTITLE),
            ([('zh-TW',TW,False),('zh-CN',CN,True),('ja',JA,False)],USE_EXISTING_ZH_TW)]
        for sources,expected in cases:
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as temp:
                inventory,_=self.inventory(Path(temp),sources)
                self.assertEqual(expected,analyze_sources(**inventory.analyzer_arguments()).strategy)

    def test_no_trusted_audio_does_not_force_asr(self):
        for language in ('eng','und'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temp:
                inventory,_=self.inventory(Path(temp),[('zh-CN',CN,True)],audio_language=language)
                self.assertEqual('NEEDS_REVIEW',analyze_sources(**inventory.analyzer_arguments()).strategy)

    def test_incomplete_inventory_cannot_use_qc_failure_as_fallback_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            inventory,_=self.inventory(Path(temp),[('zh-CN',CN,True)])
            args=inventory.analyzer_arguments();args['subtitle_inventory_complete']=False
            self.assertEqual('NEEDS_REVIEW',analyze_sources(**args).strategy)

    def test_qc_policy_change_invalidates_cached_source_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);video=root/'episode.mkv';video.write_bytes(b'immutable-source')
            a=SimpleNamespace(subtitle_quality_max_overlap_seconds=0.10)
            b=SimpleNamespace(subtitle_quality_max_overlap_seconds=0.05)
            self.assertNotEqual(build_source_input_identity(video,'job',config=a).fingerprint,
                build_source_input_identity(video,'job',config=b).fingerprint)

    def test_malformed_srt_is_unusable_not_a_worker_crash(self):
        cases = [
            ([('zh-TW',TW,'parse'),('zh-CN',CN,False)],'jpn',CONVERT_ZH_CN),
            ([('zh-CN',CN,'parse'),('ja',JA,False)],'jpn',TRANSLATE_JA_SUBTITLE),
            ([('zh-CN',CN,'parse')],'jpn',ASR_JA_AUDIO),
            ([('zh-CN',CN,'parse')],'und','NEEDS_REVIEW'),
        ]
        for sources,language,expected in cases:
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as temp:
                inventory,_=self.inventory(Path(temp),sources,audio_language=language)
                decision=analyze_sources(**inventory.analyzer_arguments())
                self.assertEqual(expected,decision.strategy)
                rejected=[c for c in decision.candidates if c.kind=='subtitle' and not c.eligible]
                self.assertEqual(1,len(rejected))
                self.assertIn('source_hard_qc_failed',rejected[0].rejection_reasons)
                self.assertIn('subtitle_parse_failed',rejected[0].evidence['hard_qc']['failure_codes'])

    def test_parse_rejection_is_replay_stable_and_keeps_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first,config=self.inventory(root,[('zh-CN',CN,'parse')])
            video=root/'episode.mkv'
            paths=[video,root/'episode.zh-CN.srt']
            before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths}
            job=build_source_input_identity(video,'test-job',config=config).media_job_identity
            with patch('source_inventory._probe_media',return_value={'format':{'duration':'48'},'streams':[]}):
                replay=inventory_sources(video,job,config=config,sidecar_paths=paths[1:])
            self.assertEqual(first.candidate_fingerprint,replay.candidate_fingerprint)
            self.assertEqual(first.subtitle_candidates,replay.subtitle_candidates)
            self.assertEqual('NEEDS_REVIEW',analyze_sources(**replay.analyzer_arguments()).strategy)
            self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths})

    def test_unrelated_quality_implementation_fault_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('subtitle_quality.analyze_subtitle_file',side_effect=ValueError('unexpected QC defect')):
                with self.assertRaisesRegex(ValueError,'unexpected QC defect'):
                    self.inventory(Path(temp),[('zh-CN',CN,False)])


if __name__=='__main__':
    unittest.main()
