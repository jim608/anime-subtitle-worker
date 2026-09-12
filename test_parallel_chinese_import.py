"""Guarded import/publication tests; real-media evidence is a separate fixture."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_subtitle_language_projection import bilingual_ass,configuration
from subtitle_extract import (
    SubtitleExtractError,_SubtitleCandidate,_publish_official_subtitle_set,
    _validated_import_candidates,classify_subtitle_content_file,extract_available_subtitles,
)
from subtitle_language_projection import normalize_parallel_chinese_ass


class ParallelChineseImportTests(unittest.TestCase):
    def setup_case(self,root):
        config=configuration();config.work_path=root/'work';config.mikan_remove_ai_after_extract=False
        target=root/'Show - S01E01.mkv';target.write_bytes(b'original source unchanged')
        source=root/'download.mkv';source.write_bytes(b'complete downloaded source')
        return config,target,source

    def extract(self,source,target,config,content,diagnostics):
        def extract_stream(_video,_stream,temp,*args,**kwargs):
            temp.write_bytes(content.encode('utf-8'))
        with patch('subtitle_extract._probe_subtitle_streams',return_value=[{
            'index':3,'codec_name':'ass','tags':{'language':'chi','title':'Traditional Chinese'}}]),patch(
            'subtitle_extract._extract_subtitle_stream',side_effect=extract_stream),patch(
            'source_inventory._probe_media',return_value={'format':{'duration':'40'}}):
            return extract_available_subtitles(source,config,output_video_path=target,
                                              validate_for_import=True,diagnostics=diagnostics)

    def test_complete_manifest_original_snapshot_and_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,source=self.setup_case(root);content=bilingual_ass()
            for _ in range(2):
                diagnostics=[];result=self.extract(source,target,config,content,diagnostics)
                self.assertEqual(len(result),1)
                self.assertTrue(any(d.get('source')=='parallel_chinese_normalization' and d.get('status')=='candidate_normalized' for d in diagnostics))
                self.assertTrue(any(d.get('source')=='import_validation' and d.get('output_parse')=='PASS' and d.get('hard_qc')=='PASS' for d in diagnostics))
            manifests=list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json'))
            self.assertEqual(len(manifests),1)
            manifest=json.loads(manifests[0].read_text(encoding='utf-8'));self.assertEqual(manifest['status'],'completed')
            publication=manifest['publications'][0];lineage=publication['source_transformation']
            snapshot=Path(lineage['original_snapshot'])
            self.assertEqual(snapshot.read_bytes(),content.encode())
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),lineage['original_sha256'])
            self.assertEqual(lineage['normalized_sha256'],publication['sha256'])
            self.assertEqual(lineage['target_validation']['hard_qc'],'PASS')
            self.assertTrue(lineage['target_validation']['source_analysis']['eligible'])
            self.assertEqual(source.read_bytes(),b'complete downloaded source')
            self.assertEqual(target.read_bytes(),b'original source unchanged')

    def test_unpaired_source_never_publishes_or_retires_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,source=self.setup_case(root)
            content=bilingual_ass().replace('Text - JP','Unmatched - JP')
            diagnostics=[]
            with patch('subtitle_extract.remove_ai_subtitle_outputs') as retire:
                self.assertEqual(self.extract(source,target,config,content,diagnostics),[])
            retire.assert_not_called()
            self.assertTrue(any(d.get('source')=='parallel_chinese_normalization' and d.get('status')=='refused' for d in diagnostics))
            self.assertFalse(list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json')))
            self.assertFalse(target.with_suffix('.zh-TW.ass').exists())

    def test_different_valid_subtitle_is_preserved_without_new_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,source=self.setup_case(root)
            other=bilingual_ass().replace('我們學習這個課程並選擇明天的方向','這裡的學校正在準備這項重要的選擇')
            valid,_=normalize_parallel_chinese_ass(other,language='zh-tw',config=config)
            output=target.with_suffix('.zh-TW.ass');output.write_text(valid,encoding='utf-8')
            before=(output.read_bytes(),output.stat().st_mtime_ns)
            self.extract(source,target,config,bilingual_ass(),[])
            self.assertEqual((output.read_bytes(),output.stat().st_mtime_ns),before)
            self.assertFalse(list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json')))

    def test_changed_original_lineage_is_refused_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,_=self.setup_case(root)
            original=root/'staged.ass';original.write_text(bilingual_ass(),encoding='utf-8')
            candidate=_SubtitleCandidate(original,'zh-tw',3,classify_subtitle_content_file(original,metadata_language='zh-tw'),(0,0))
            with patch('source_inventory._probe_media',return_value={'format':{'duration':'40'}}):
                accepted=_validated_import_candidates([candidate],target,config,[],None,normalize_parallel_chinese=True)
            self.assertEqual(len(accepted),1)
            original.write_text('changed original fixture',encoding='utf-8')
            normalized=accepted[0];output=target.with_suffix('.zh-TW.ass')
            with self.assertRaisesRegex(SubtitleExtractError,'lineage changed'):
                _publish_official_subtitle_set(target,[(normalized.source_path,output,'zh-tw')],config,
                    source_transforms={str(normalized.source_path):normalized.source_transform})
            self.assertFalse(output.exists())
            self.assertFalse(list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json')))

    def test_interrupted_original_snapshot_leaves_no_publication_and_can_retry(self):
        from safe_files import verified_copy_replace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,source=self.setup_case(root)
            def interrupted(src,dst,*args,**kwargs):
                if dst.name.startswith('source-original-'):
                    raise OSError('isolated snapshot interruption')
                return verified_copy_replace(src,dst,*args,**kwargs)
            with patch('subtitle_extract.verified_copy_replace',side_effect=interrupted):
                with self.assertRaisesRegex(OSError,'snapshot interruption'):
                    self.extract(source,target,config,bilingual_ass(),[])
            self.assertFalse(target.with_suffix('.zh-TW.ass').exists())
            self.assertFalse(list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json')))
            self.assertEqual(len(self.extract(source,target,config,bilingual_ass(),[])),1)
            self.assertEqual(len(list(config.work_path.glob('official_subtitle_versions/*/*/manifest.json'))),1)

    def test_mikan_result_preserves_original_rejection_and_verified_success(self):
        from mikan_worker import MikanWorker,_apply_completed_extract_result,_pending_is_terminal_success
        from qbit_client import QBitTorrent
        from test_mikan_worker import _logger,_mikan_process_config
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,target,source=self.setup_case(root)
            config=_mikan_process_config(root,root)
            for key,value in vars(configuration()).items():setattr(config,key,value)
            config.mikan_remove_ai_after_extract=False
            worker=MikanWorker(config,_logger())
            torrent=QBitTorrent(hash='a'*40,name='Show - 01 [CHT]',progress=1.0,state='uploading',
                dlspeed=0,downloaded=100,added_on=None,content_path=str(source),save_path=str(root),
                category='llm-sub',tags='mikansub')
            def extract_stream(_video,_stream,temp,*args,**kwargs):temp.write_bytes(bilingual_ass().encode())
            with patch('subtitle_extract._probe_subtitle_streams',return_value=[{
                'index':3,'codec_name':'ass','tags':{'language':'chi','title':'Traditional Chinese'}}]),patch(
                'subtitle_extract._extract_subtitle_stream',side_effect=extract_stream),patch(
                'source_inventory._probe_media',return_value={'format':{'duration':'40'}}):
                result=worker._extract_completed_source_to_target(source,target,torrent,[],root)
            self.assertEqual(result.extracted_count,1)
            self.assertFalse(result.failure_reason)
            statuses=[d.get('status') for d in result.subtitle_diagnostics if d.get('source')=='import_validation']
            self.assertIn('validation_failed',statuses)
            self.assertIn('validated',statuses)
            entry={'bangumi_id':123,'episode':1,'torrent_url':'https://fixture.invalid/a.torrent',
                'queued_at':'2026-09-12T00:00:00Z','info_hash':torrent.hash}
            pending={'items':{'123:1':entry}}
            changed,replacements=_apply_completed_extract_result(pending,torrent,[],result)
            self.assertTrue(changed);self.assertFalse(replacements)
            self.assertTrue(_pending_is_terminal_success(entry))

    def test_publication_guard_refusal_rolls_back_prepared_manifest_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config,target,_=self.setup_case(root)
            normalized,_=normalize_parallel_chinese_ass(bilingual_ass(),language='zh-tw',config=config)
            staged=root/'staged.ass';staged.write_bytes(normalized.encode())
            guard=Mock(side_effect=[{'contract':'fixture-reviewed-recovery'},RuntimeError('source changed before publish')])
            with self.assertRaisesRegex(RuntimeError,'source changed before publish'):
                _publish_official_subtitle_set(target,[(staged,target.with_suffix('.zh-TW.ass'),'zh-tw')],config,
                                              publication_guard=guard)
            self.assertFalse(target.with_suffix('.zh-TW.ass').exists())
            manifest=json.loads(next(config.work_path.glob('official_subtitle_versions/*/*/manifest.json')).read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'],'rolled_back')
            self.assertEqual(manifest['reviewed_recovery']['contract'],'fixture-reviewed-recovery')


if __name__=='__main__':unittest.main()
