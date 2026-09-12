import unittest
from types import SimpleNamespace
from ass_utils import AssStyle,format_ass
from srt_utils import SrtBlock
from subtitle_language_projection import ParallelSubtitleError,normalize_parallel_chinese_ass
from subtitle_quality import analyze_subtitle_file
from pathlib import Path
import tempfile
from dataclasses import asdict
from unittest.mock import patch


def configuration():
    return SimpleNamespace(**{'ass_'+key:value for key,value in asdict(AssStyle()).items()})


def bilingual_ass(count=40):
    blocks=[SrtBlock(i+1,f'00:00:{i:02},000 --> 00:00:{i+1:02},000',[f'我們學習這個課程並選擇明天的方向 {i+1}']) for i in range(count)]
    original=format_ass(blocks,AssStyle())
    lines=[]
    for line in original.splitlines():
        if not line.startswith('Dialogue:'):
            lines.append(line);continue
        fields=line.split(':',1)[1].lstrip().split(',',9)
        fields[3]='Text - CN';lines.append('Dialogue: '+','.join(fields))
        fields[3]='Text - JP';fields[9]=f'私たちは明日のために勉強しています {len(lines)}'
        lines.append('Dialogue: '+','.join(fields))
    return '\n'.join(lines)+'\n'


class ParallelChineseProjectionTests(unittest.TestCase):
    def test_paired_chinese_projection_keeps_text_and_same_qc(self):
        source=bilingual_ass();normalized,evidence=normalize_parallel_chinese_ass(source,language='zh-tw',config=configuration())
        self.assertEqual(evidence['removed_paired_japanese_events'],40)
        self.assertEqual(evidence['retained_text_lines'],40)
        self.assertNotIn('私たちは',normalized)
        self.assertIn('我們學習這個課程並選擇明天的方向',normalized)
        self.assertEqual(normalize_parallel_chinese_ass(normalized,language='zh-tw',config=SimpleNamespace()),None)
        self.assertEqual(normalize_parallel_chinese_ass(source,language='zh-tw',config=configuration()),(normalized,evidence))
        with tempfile.TemporaryDirectory() as tmp:
            before=Path(tmp)/'before.ass';after=Path(tmp)/'after.ass'
            before.write_text(source,encoding='utf-8');after.write_text(normalized,encoding='utf-8')
            self.assertTrue(analyze_subtitle_file(before,SimpleNamespace(),role='unknown').has_failures)
            self.assertFalse(analyze_subtitle_file(after,SimpleNamespace(),role='unknown').has_failures)

    def test_unpaired_foreign_dialogue_is_not_removed(self):
        source=bilingual_ass().replace('Text - JP,,0,0,0,,','Unpaired - JP,,0,0,0,,',1)
        with self.assertRaises(ParallelSubtitleError):
            normalize_parallel_chinese_ass(source,language='zh-tw',config=SimpleNamespace())

    def test_name_labels_without_language_evidence_are_not_authority(self):
        source=bilingual_ass().replace('私たちは明日のために勉強しています','我們學習這個課程並選擇明天的方向')
        with self.assertRaisesRegex(ParallelSubtitleError,'content_language_unproven'):
            normalize_parallel_chinese_ass(source,language='zh-tw',config=SimpleNamespace())

    def test_small_signs_only_pairing_is_not_accepted(self):
        with self.assertRaisesRegex(ParallelSubtitleError,'insufficient_language_events'):
            normalize_parallel_chinese_ass(bilingual_ass(3),language='zh-tw',config=SimpleNamespace())

    def test_foreign_language_and_nonparallel_sources_are_unchanged(self):
        self.assertIsNone(normalize_parallel_chinese_ass(bilingual_ass(),language='en',config=SimpleNamespace()))
        self.assertIsNone(normalize_parallel_chinese_ass(bilingual_ass().replace('Text - JP','Other'),language='zh-tw',config=SimpleNamespace()))

    def test_inconsistent_event_format_refuses_normalization(self):
        source=bilingual_ass().replace('Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text','Format: Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text, Layer')
        with self.assertRaisesRegex(ParallelSubtitleError,'unsupported_event_format'):
            normalize_parallel_chinese_ass(source,language='zh-tw',config=SimpleNamespace())

    def test_normalizer_cannot_drop_retained_dialogue_to_make_qc_pass(self):
        from ass_utils import ass_dialogue_style_to_srt_blocks
        def drop_one(content,style):
            return ass_dialogue_style_to_srt_blocks(content,style)[1:]
        with patch('subtitle_language_projection.ass_dialogue_style_to_srt_blocks',side_effect=drop_one):
            with self.assertRaisesRegex(ParallelSubtitleError,'retained_text_not_preserved'):
                normalize_parallel_chinese_ass(bilingual_ass(),language='zh-tw',config=configuration())

    def test_wrong_requested_chinese_script_is_not_assumed_safe(self):
        with self.assertRaisesRegex(ParallelSubtitleError,'content_language_unproven'):
            normalize_parallel_chinese_ass(bilingual_ass(),language='zh-cn',config=configuration())


if __name__=='__main__':unittest.main()
