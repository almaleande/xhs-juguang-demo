"""演示版隐私边界回归测试；仅连接临时本地服务。"""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import accounts_meta, authflow, diagapi, jgapi, tokenhub


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrivacyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('privacy_board', ROOT / '看板服务.py')
        cls.board = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'argv', ['看板服务.py']):
            spec.loader.exec_module(cls.board)
        cls.board.Handler.log_message = lambda *args: None
        cls.board.build_all()
        cls.server = cls.board.ThreadingHTTPServer(('127.0.0.1', 0), cls.board.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, method='GET', body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        conn.request(method, path, body, {'Content-Type': 'application/json'})
        res = conn.getresponse()
        result = res.status, res.read()
        conn.close()
        return result

    def test_no_network_or_credential_write(self):
        with patch.object(jgapi._NO_PROXY_OPENER, 'open', side_effect=AssertionError('network')), \
             patch.object(tokenhub, 'save_accounts', side_effect=AssertionError('credential write')):
            self.assertEqual(authflow.auth_url(), '#')
            self.assertEqual(authflow.authorize('not-a-demo-credential')['code'], 'DEMO_DISABLED')
            self.assertEqual(jgapi.post('/test', {}, token='not-a-demo-credential')['code'], 'DEMO_DISABLED')
        with patch('builtins.open', side_effect=AssertionError('login file read')):
            self.assertEqual(accounts_meta.load_login_accounts(), ([], None))
        self.assertTrue(diagapi._is_demo_mode())
        before = digest(ROOT / 'juguang_accounts.json')
        with self.assertRaises(ValueError):
            tokenhub.save_accounts({'sample': {'access_token': 'not-a-demo-credential'}})
        self.assertEqual(digest(ROOT / 'juguang_accounts.json'), before)

    def test_blocked_routes_preserve_data(self):
        paths = [ROOT / 'juguang_accounts.json', ROOT / 'data/balances.json']
        before = [digest(p) for p in paths]
        for path, method in [('/go', 'GET'), ('/auth', 'GET'),
                             ('/api/auth', 'POST'), ('/api/balances/refresh', 'POST')]:
            with self.subTest(path=path):
                status, body = self.request(path, method, '{invalid-json-body')
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)['code'], 'DEMO_DISABLED')
        self.assertEqual([digest(p) for p in paths], before)

    def test_pages_and_api_do_not_expose_credentials(self):
        forbidden = ('夏天', '金夫人', 'shuli', 'ngrok-free.dev', 'token_preview',
                     'access_token', 'refresh_token', 'app_secret', 'xiaohongshu.com/explore/',
                     'xiaohongshu.com/oauth')
        for path in ('/', '/?range=3d', '/?range=7d', '/?range=month', '/?range=year',
                     '/keyword', '/keyword?range=latest', '/keyword?range=3d',
                     '/keyword?range=7d', '/keyword?range=month', '/keyword?range=lastmonth',
                     '/keyword?range=quarter', '/keyword?range=halfyear', '/keyword?range=year',
                     '/diag', '/diag?range=7d',
                     '/api/accounts', '/api/pending', '/api/status', '/api/dashboard'):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                text = body.decode()
                for word in forbidden:
                    self.assertNotIn(word, text)
                if path.startswith('/api/'):
                    json.loads(text)
                else:
                    self.assertIn('</html>', text)
                    self.assertIn('<title>小红书聚光投放工作台-AI自动化分析决策系统</title>', text)
                    self.assertNotIn('class="logo"', text)
                    self.assertIn('AI 驱动 · 自动分析与投放决策</span>', text)
                    self.assertNotIn('>-AI自动化分析决策系统</span>', text)
                    if path.startswith('/keyword'):
                        snap = text.split('var KWSNAP = ', 1)[1].split(';', 1)[0]
                        self.assertTrue(json.loads(snap)['keywords'])
        status, body = self.request('/assets/covers/cover_0001.jpg')
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'\xff\xd8'))
        self.assertIn('../assets/covers/', (ROOT / 'reports/小红书投放看板.html').read_text())


if __name__ == '__main__':
    unittest.main()
