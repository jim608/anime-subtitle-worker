"""Completion may account for one proven own output, never arbitrary drift."""
import hashlib,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import m2_strict_runtime_evidence as strict

class OwnedPublicationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.video=self.root/'episode.mkv';self.video.write_bytes(b'source-media')
        self.config=SimpleNamespace(work_path=self.root/'work')
        self.output=self.root/'episode.zh-TW.ass';self.output.write_bytes(b'validated-new-output')
        self.old={'relative_path':self.output.name,'size':len(b'old-rejected-caption'),'mtime_ns':11,
            'sha256':hashlib.sha256(b'old-rejected-caption').hexdigest()}
        self.selected={'relative_path':'episode.zh-CN.srt','size':30,'mtime_ns':12,'sha256':'a'*64}
        base={'schema_version':'source-input-identity-v3','media_job_identity':{'job_id':'job','media_revision':'b'*64}}
        self.before={**base,'sidecars':[self.old,self.selected]};self.current={**base,'sidecars':[self.selected]}
        payload={'strategy':'CONVERT_ZH_CN','selected_subtitle_track':{'source_reference':self.selected['relative_path']},
            'candidates':[{'source_reference':self.old['relative_path'],'source_sha256':self.old['sha256'],
                'eligible':False,'rejection_reasons':['source_hard_qc_failed']}]}
        self.row={'input_identity_json':json.dumps(self.before),'decision_json':json.dumps(payload),'created_at':20,
            'candidate_fingerprint':'c'*64}
        digest=hashlib.sha1(str(self.video.resolve()).encode()).hexdigest()[:16]
        self.versions=self.config.work_path/'ai_output_versions'/digest
        version=self.versions/'fixture';version.mkdir(parents=True)
        self.backup=version/'previous.ass';self.backup.write_bytes(b'old-rejected-caption')
        self.journal=version/'manifest.json';self.publication={'video':str(self.video),'status':'completed','created_at':21,'completed_at':22,
            'destinations':[str(self.output)],'backups':[{'path':str(self.output),'backup':str(self.backup),'sha256':self.old['sha256']}],
            'published':[{'path':str(self.output),'sha256':hashlib.sha256(self.output.read_bytes()).hexdigest()}]}
        self.save();self.provenance={'run_started_at':19,'finished_at':23}
    def save(self):self.journal.write_text(json.dumps(self.publication),encoding='utf-8')
    def check(self):
        with patch('source_inventory._is_verified_generated_publication_sidecar',return_value=True):
            return strict._completion_input_after_owned_publication(self.video,self.config,self.row,self.current,self.provenance)
    def test_exact_rejected_output_replacement_can_validate_original_decision(self):
        self.assertEqual(self.before,self.check())
        self.assertEqual(b'old-rejected-caption',self.backup.read_bytes())
        self.assertEqual(b'source-media',self.video.read_bytes())
        self.assertEqual(self.check(),self.check())
    def test_missing_or_corrupt_backup_stays_unproven(self):
        self.backup.write_bytes(b'unproven')
        self.assertIsNone(self.check())
    def test_selected_source_can_never_be_excused(self):
        payload=json.loads(self.row['decision_json']);payload['selected_subtitle_track']['source_reference']=self.old['relative_path']
        self.row['decision_json']=json.dumps(payload);self.assertIsNone(self.check())
    def test_other_source_change_or_addition_is_not_absorbed(self):
        self.current['sidecars'][0]={**self.selected,'sha256':'d'*64}
        self.assertIsNone(self.check())
    def test_healthy_old_caption_cannot_be_overwritten(self):
        payload=json.loads(self.row['decision_json']);payload['candidates'][0]['eligible']=True
        self.row['decision_json']=json.dumps(payload);self.assertIsNone(self.check())
    def test_unbound_or_incomplete_publication_is_rejected(self):
        for field,value in [('status','prepared'),('created_at',10),('completed_at',24)]:
            with self.subTest(field=field):
                old=self.publication[field];self.publication[field]=value;self.save()
                self.assertIsNone(self.check());self.publication[field]=old
        self.save()
    def test_current_output_tamper_is_rejected(self):
        self.output.write_bytes(b'tampered');self.assertIsNone(self.check())
    def test_restart_reuses_proof_without_changing_history(self):
        before={p:p.read_bytes() for p in (self.journal,self.backup,self.output,self.video)}
        self.assertIsNotNone(self.check())
        self.row=json.loads(json.dumps(self.row));self.current=json.loads(json.dumps(self.current))
        self.assertIsNotNone(self.check());self.assertEqual(before,{p:p.read_bytes() for p in before})

if __name__=='__main__':unittest.main()
