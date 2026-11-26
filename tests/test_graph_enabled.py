"""Проверка включения Graph DB при обычном запуске и через Диспетчерскую."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MVP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MVP / 'dashboard'), str(MVP)]
with patch.dict(sys.modules, {'dotenv': types.SimpleNamespace(load_dotenv=lambda: None)}):
    from config import Config
import settings_store
import supervisor


class GraphEnabledTests(unittest.TestCase):
    def test_default_launch_uploads_companies_products_and_distributors(self):
        with patch.dict(os.environ, {}, clear=True):
            for config in [Config(), Config.load_from_env()]:
                self.assertTrue(config.graph_db_enable)
                self.assertTrue(config.graph_db_upload_company)
                self.assertTrue(config.graph_db_upload_products)
                self.assertTrue(config.graph_db_upload_distributors)

    def test_explicit_isolated_test_setting_still_overrides_default(self):
        with patch.dict(os.environ, GRAPH_DB_ENABLE='false'):
            self.assertFalse(Config.load_from_env().graph_db_enable)

    def test_panel_passes_enabled_graph_to_the_actual_pipeline_launch(self):
        data = Path.cwd() / 'test_data'
        data.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='graph_', dir=data))
        overrides = root / 'settings_overrides.env'
        overrides.write_text('GRAPH_DB_ENABLE=true\n', encoding='utf-8')
        with patch.dict(os.environ, GRAPH_DB_ENABLE='false', LOGS_DIR=str(root / 'logs'),
                        RUNS_DIR=str(root / 'logs/runs')), \
                patch.object(settings_store, 'OVERRIDES_PATH', str(overrides)), \
                patch.object(supervisor, 'process_status', return_value={'running': False}), \
                patch.object(supervisor, '_acquire_lock'), \
                patch.object(supervisor, '_clear_control'), \
                patch.object(supervisor, '_write_lock_pid'), \
                patch.dict(supervisor._PIPELINE, supervisor._PIPELINE.copy()), \
                patch.dict(supervisor._WATCH, supervisor._WATCH.copy()), \
                patch.object(supervisor.subprocess, 'Popen') as start:
            start.return_value.pid = 12345
            supervisor.start(company_ids=['test-company'])
            env = start.call_args.kwargs['env']
            self.assertEqual(env['GRAPH_DB_ENABLE'], 'true')
            self.assertIn('main.py', start.call_args.args[0])
            with patch.dict(os.environ, env, clear=True):
                self.assertTrue(Config.load_from_env().graph_db_enable)


if __name__ == '__main__':
    unittest.main(verbosity=2)
