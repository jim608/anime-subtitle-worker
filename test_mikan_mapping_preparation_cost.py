import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mikan_matcher as matcher


class MappingPreparationCostTests(unittest.TestCase):
    def test_path_identity_refresh_and_oserror_fallback_remain_scoped(self):
        mapping={'path':'/isolated/link'}
        first={}
        with patch.object(Path,'resolve',autospec=True,return_value=Path('/isolated/target-a')) as resolve:
            self.assertEqual(matcher._mapping_path_key(mapping,resolved_paths=first),str(Path('/isolated/target-a')).casefold())
            self.assertEqual(matcher._mapping_path_key(mapping,resolved_paths=first),str(Path('/isolated/target-a')).casefold())
            self.assertEqual(resolve.call_count,1)
        with patch.object(Path,'resolve',autospec=True,return_value=Path('/isolated/target-b')):
            self.assertEqual(matcher._mapping_path_key(mapping,resolved_paths={}),str(Path('/isolated/target-b')).casefold())
        with patch.object(Path,'resolve',side_effect=OSError('isolated unavailable path')):
            self.assertEqual(matcher._mapping_path_key(mapping,resolved_paths={}),str(Path('/isolated/link')).casefold())

    def test_cached_miss_suppression_resolves_each_path_once_per_operation(self):
        mappings=[{'path':f'/isolated/series-{i}','bangumi_id':i+1,'identity_source':'series_metadata'}
                  for i in range(80)]
        mappings.append({'path':'/isolated/series-0','bangumi_id':999,'identity_source':'manual','locked':True})
        cache={f'/isolated/series-{i}':{'matcher_version':2,'status':'miss','reason':'ambiguous_candidates'}
               for i in range(40)}
        config=SimpleNamespace(mikan_series_path_mappings=[],mikan_auto_match_enabled=True)
        with patch.object(matcher,'_series_metadata_mappings',return_value=mappings), \
             patch.object(matcher,'_resolve_cache_path',return_value=Path('/isolated/cache')), \
             patch.object(matcher,'_load_cache',return_value=cache), \
             patch.object(matcher,'_season_scoped_cached_mappings',return_value=[]), \
             patch.object(matcher,'_valid_cached_miss',return_value=True), \
             patch.object(matcher,'_valid_cached_mapping',return_value=False), \
             patch.object(Path,'resolve',autospec=True,side_effect=lambda path:path) as resolve:
            result=matcher.resolve_mikan_series_mappings(config,logging.getLogger('mapping-cost'),cached_only=True)
            self.assertLessEqual(resolve.call_count,160,'cached metadata preparation must not resolve every path for every miss')
            self.assertEqual(len(result),41)
            self.assertTrue(any(m['bangumi_id']==999 for m in result))
            before=resolve.call_count
            matcher.resolve_mikan_series_mappings(config,logging.getLogger('mapping-cost'),cached_only=True)
            self.assertGreater(resolve.call_count,before,'path identities must be refreshed next operation')


if __name__=='__main__':unittest.main()
