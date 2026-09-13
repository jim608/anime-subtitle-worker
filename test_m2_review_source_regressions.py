"""Regressions for shared causes found in the immutable 20260912 5/15 Gate."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from source_analyzer import analyze_sources, CONVERT_ZH_CN, TRANSLATE_JA_SUBTITLE
from source_decision_adapter import resolve_source_decision, SourceDecisionReviewError
from source_inventory import build_source_input_identity, inventory_sources, _subtitle_metrics
from subtitle_extract import classify_subtitle_content_file


def srt_text(text, *, reverse=False, duplicate=False):
    cues = [f"{i+1}\n00:00:{i*2:02d},000 --> 00:00:{i*2+1:02d},900\n{text}{i}\n" for i in range(24)]
    if reverse:
        cues.reverse()
    if duplicate:
        cues.append(cues[-1])
    return '\n'.join(cues)


JA = '彼らは時々話して書いて門を開いて學んでいます'
CN = '这里会选择打开网络连接并显示完整字幕'
TW = '這裡會選擇開啟網路連線並顯示完整字幕'


class ReviewSourceRegressionTest(unittest.TestCase):
    def test_v1_input_context_cannot_reuse_v2_semantic_decisions(self):
        with tempfile.TemporaryDirectory() as temp:
            video=Path(temp)/'episode.mkv';video.write_bytes(b'immutable-video')
            current=build_source_input_identity(video,'job-test')
            with patch('source_inventory.SOURCE_INPUT_IDENTITY_VERSION','source-input-identity-v1'):
                previous=build_source_input_identity(video,'job-test')
            self.assertNotEqual(previous.candidate_fingerprint,current.candidate_fingerprint)
            self.assertEqual(previous.media_job_identity,current.media_job_identity)

    def test_explicit_japanese_context_kanji_markers_do_not_beat_dominant_kana(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'source.srt';p.write_text(srt_text(JA),encoding='utf-8')
            self.assertEqual('ja',classify_subtitle_content_file(p,metadata_language='ja').language)

    def test_japanese_hint_does_not_override_chinese_or_unknown_content(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'source.srt'
            for text,expected in ((CN,'zh-cn'),(TW,'zh-tw'),('Plain English',None),('世界和平大家一起吃飯',None)):
                with self.subTest(text=text):
                    p.write_text(srt_text(text),encoding='utf-8')
                    self.assertEqual(expected,classify_subtitle_content_file(p,metadata_language='ja').language)

    def test_bilingual_chinese_dialogue_is_not_relabelled_japanese(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'source.srt';p.write_text(srt_text(JA+'\n'+TW),encoding='utf-8')
            self.assertEqual('zh-tw',classify_subtitle_content_file(p,metadata_language='ja').language)

    def test_semantic_digest_is_order_independent_but_preserves_multiplicity_time_and_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'a.srt';b=root/'b.srt'
            a.write_text(srt_text(CN),encoding='utf-8');b.write_text(srt_text(CN,reverse=True),encoding='utf-8')
            digest=_subtitle_metrics(a).content_sha256
            self.assertEqual(digest,_subtitle_metrics(b).content_sha256)
            for altered in (srt_text(CN,duplicate=True),srt_text(CN).replace('900','899',1),srt_text(CN).replace('这里','那里',1)):
                b.write_text(altered,encoding='utf-8')
                self.assertNotEqual(digest,_subtitle_metrics(b).content_sha256)

    def _resolve_fixture(self, root, text, *, language='ja', reverse_duplicate=False, unsafe=False):
        video=root/'episode.mkv';video.write_bytes(b'untouched-media')
        subtitle=root/f'episode.{language}.srt'
        content=srt_text(text)
        if unsafe:
            content=content.replace('00:00:01,900','00:00:00,010',1)
        subtitle.write_text(content,encoding='utf-8')
        sources=[subtitle]
        if reverse_duplicate:
            twin=root/f'episode.alternate.{language}.srt'
            twin.write_text(srt_text(text,reverse=True),encoding='utf-8');sources.append(twin)
        config=SimpleNamespace(work_path=root/'work',subtitle_quality_check_enabled=True)
        identity=build_source_input_identity(video,'job-test',config=config)
        job=dict(identity.media_job_identity)
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (video,*sources)}
        with patch('source_inventory._probe_media',return_value={'format':{'duration':'48'},'streams':[]}):
            inventory=inventory_sources(video,job,config=config,sidecar_paths=sources)
        decision=analyze_sources(**inventory.analyzer_arguments()).to_dict()
        decision['candidate_fingerprint']=inventory.candidate_fingerprint
        return video,job,config,decision,before

    def test_japanese_adapter_continues_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            video,job,config,decision,before=self._resolve_fixture(Path(temp),JA)
            self.assertEqual(TRANSLATE_JA_SUBTITLE,decision['strategy'])
            first=resolve_source_decision(video,{'decision':decision},job,config)
            second=resolve_source_decision(video,{'decision':decision},job,config)
            self.assertEqual('ja',first.subtitle.source_language)
            self.assertEqual(first,second)
            self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})

    def test_equivalent_reordered_candidates_continue_zh_cn_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            video,job,config,decision,before=self._resolve_fixture(Path(temp),CN,language='zh-CN',reverse_duplicate=True)
            self.assertEqual(CONVERT_ZH_CN,decision['strategy'])
            resolved=resolve_source_decision(video,{'decision':decision},job,config)
            self.assertEqual('zh-cn',resolved.subtitle.source_language)
            self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})

    def test_corrected_language_does_not_bypass_hard_qc(self):
        with tempfile.TemporaryDirectory() as temp:
            video,job,config,decision,before=self._resolve_fixture(Path(temp),JA,unsafe=True)
            with self.assertRaisesRegex(SourceDecisionReviewError,'structural QC'):
                resolve_source_decision(video,{'decision':decision},job,config)
            self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before})


if __name__=='__main__':
    unittest.main()
