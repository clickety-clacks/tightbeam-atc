import sys
from pathlib import Path
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from atc_upstream import Read


class DetailPublicationTest(unittest.TestCase):
    def test_blocked_detail_read_allows_publication_and_rejects_old_pin(self):
        entered, release, published = (threading.Event() for _ in range(3))
        records = [dict(id='attest', assignmentId='assignment', ts=1, kind='progress')]

        class Gateway:
            read_count = 0

            def read(self, resource, scope):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release detail read')
                return Read(records, True, 1, 'test-read')

        service = LiveService(Gateway())
        service.model.accept('work-items', None, Read([
            dict(id='item', title='Before', state='open')], True, 1, 'test-read'))
        service.model.accept('assignments', None, Read([
            dict(id='assignment', workItemId='item', state='open')], True, 1, 'test-read'))
        service.model.accept('attests', 'assignment', Read(records, True, 1, 'test-read'))
        service._publish()
        response = []
        reader = threading.Thread(target=lambda: response.append(service.evidence_page('item')))

        def publish():
            with service.model_lock:
                service.model.accept('work-items', None, Read([
                    dict(id='item', title='After', state='open')], True, 2, 'test-read'))
                service._dirty = True
                service._publish()
                published.set()

        publisher = threading.Thread(target=publish)
        try:
            reader.start()
            self.assertTrue(entered.wait(2), 'detail read never reached upstream')
            publisher.start()
            self.assertTrue(published.wait(1), 'upstream detail I/O blocked publication')
        finally:
            release.set()
            reader.join(5)
            if publisher.ident is not None:
                publisher.join(5)
            service.stop()
        self.assertFalse(reader.is_alive())
        self.assertFalse(publisher.is_alive())
        self.assertEqual(409, response[0][0])
        self.assertEqual('revision-mismatch', response[0][1]['error'])
